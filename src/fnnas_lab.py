# -*- coding: utf-8 -*-
"""
飞牛 fnOS 本地测试环境 (FnNAS Lab)
--------------------------------------------------
零外部依赖：仅使用 Python 标准库 + 随程序附带的便携版 QEMU。
适用于 Windows 10 及以上 64 位系统。

功能：
  1. 环境自检（QEMU / 固件 / 硬件虚拟化）
  2. 飞牛 ISO 镜像与系统盘管理（随时切换）
  3. 虚拟机参数配置
  4. 一键启动 / 优雅关机 / 打开飞牛管理界面
  5. FPK 应用安装引导
  6. 在 PC 上打开 FPK 应用界面
"""

import ctypes
import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from ctypes import wintypes
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "飞牛 fnOS 本地测试环境"
APP_VER = "2.4.0"

# 初始状态快照名称
INIT_SNAPSHOT = "fnnas_initial_state"

# ---------------------------------------------------------------------------
# 路径与配置
# ---------------------------------------------------------------------------

def is_frozen():
    return bool(getattr(sys, "frozen", False))


def app_dir():
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _source_qemu_root():
    """程序包内自带的 QEMU 运行时目录。"""
    candidates = [
        app_dir() / "runtime" / "qemu",
        app_dir() / "tools" / "qemu_extracted",
        app_dir() / "tools" / "qemu",
    ]
    for c in candidates:
        if c.exists():
            return c
    # 单文件版：运行时由 exe 末尾的压缩包解压到安全目录
    safe = _safe_runtime_dir()
    if (safe / "qemu-system-x86_64.exe").exists():
        return safe
    return candidates[0]


# QEMU 对中文路径支持不佳（读取固件会失败）。当程序位于含非 ASCII 字符的
# 路径时，把运行时复制到纯英文的安全目录后再启动，确保固件能正常加载。
_SAFE_RUNTIME_MARK = "fnnas-qemu-runtime-v1"


def _is_ascii_path(p):
    try:
        return str(p).encode("ascii")
    except UnicodeEncodeError:
        return False


def _safe_base():
    """返回一个纯英文、可写的基础目录。"""
    candidates = []
    pub = os.environ.get("PUBLIC", r"C:\Users\Public")
    candidates.append(Path(pub) / "Documents")
    candidates.append(Path(os.environ.get("SystemDrive", "C:")) / "ProgramData")
    candidates.append(Path(os.environ.get("TEMP", r"C:\Windows\Temp")))
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            t = base / ".wr"
            t.write_text("1", encoding="ascii")
            t.unlink()
            if _is_ascii_path(base):
                return base
        except Exception:
            continue
    return Path(r"C:\ProgramData")


_SAFE_RUNTIME_CACHE = []


def _safe_runtime_dir():
    """安全目录下的 QEMU 运行时位置（纯英文路径）。结果缓存，避免反复探测。"""
    if not _SAFE_RUNTIME_CACHE:
        _SAFE_RUNTIME_CACHE.append(_safe_base() / _SAFE_RUNTIME_MARK / "qemu")
    return _SAFE_RUNTIME_CACHE[0]


# 单文件版：运行时压缩包以"附加数据"形式拼在 exe 末尾。
# 布局：[exe 主体][zip 数据][8 字节起始偏移][8 字节魔数]
_PAYLOAD_MAGIC = b"FNPLD001"
_PAYLOAD_TRAILER = 16


def _payload_span():
    """返回附加在 exe 末尾的运行时压缩包 (start, end)；没有则返回 None。"""
    if not is_frozen():
        return None
    try:
        exe = Path(sys.executable)
        size = exe.stat().st_size
        if size < _PAYLOAD_TRAILER + 1024:
            return None
        with open(exe, "rb") as f:
            f.seek(size - _PAYLOAD_TRAILER)
            trailer = f.read(_PAYLOAD_TRAILER)
        if not trailer.endswith(_PAYLOAD_MAGIC):
            return None
        start = struct.unpack("<Q", trailer[:8])[0]
        end = size - _PAYLOAD_TRAILER
        if not (0 < start < end):
            return None
        return start, end
    except Exception:
        return None


def _payload_ready():
    try:
        return (_safe_runtime_dir() / "qemu-system-x86_64.exe").exists()
    except Exception:
        return False


def _extract_payload(progress=None):
    """把 exe 末尾的运行时压缩包解压到安全目录。已解压过则直接返回。"""
    span = _payload_span()
    if not span:
        return False, "未找到内嵌运行时"
    dst = _safe_runtime_dir()
    if _payload_ready() and (dst.parent / "copied.ok").exists():
        return True, "运行时已就绪（已解压）"
    start, end = span
    total = end - start
    tmp_zip = Path(tempfile.gettempdir()) / ("fnnas-runtime-%d.zip" % os.getpid())
    try:
        if progress:
            progress("正在准备运行环境（首次启动需要解压，约 1 分钟）…", 0.0)
        # 1) 把附加数据切出来写成临时 zip（zip 的偏移是基于自身起点的）
        chunk = 4 << 20
        done = 0
        with open(sys.executable, "rb") as src, open(tmp_zip, "wb") as out:
            src.seek(start)
            left = total
            while left > 0:
                buf = src.read(min(chunk, left))
                if not buf:
                    break
                out.write(buf)
                left -= len(buf)
                done += len(buf)
                if progress:
                    progress(None, 0.35 * done / total)
        # 2) 解压
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(str(dst), ignore_errors=True)
        with zipfile.ZipFile(tmp_zip) as z:
            members = z.infolist()
            all_bytes = sum(m.file_size for m in members) or 1
            done = 0
            for m in members:
                z.extract(m, str(dst.parent))
                done += m.file_size
                if progress:
                    progress(None, 0.35 + 0.65 * done / all_bytes)
        (dst.parent / "copied.ok").write_text("single-exe", encoding="ascii")
        if progress:
            progress("运行环境准备完成", 1.0)
        if not (dst / "qemu-system-x86_64.exe").exists():
            return False, "解压后未找到 QEMU 主程序"
        return True, "运行时已解压到安全目录"
    except Exception as e:
        return False, "无法解压运行环境：%s" % e
    finally:
        try:
            tmp_zip.unlink()
        except Exception:
            pass


def qemu_root():
    """返回实际用于启动 QEMU 的运行时目录（保证纯英文路径）。"""
    src = _source_qemu_root()
    if _is_ascii_path(src):
        return src
    # 源路径含中文：使用复制出的安全副本
    return _safe_runtime_dir()


def ensure_qemu_runtime(progress=None):
    """准备 QEMU 运行时。

    - 目录版：程序旁边自带 runtime/qemu；若路径含中文则复制到纯英文目录
    - 单文件版：从 exe 末尾解压内嵌运行时到纯英文目录（仅首次）
    """
    src = _source_qemu_root()
    if src.exists() and src.is_dir():
        if _is_ascii_path(src):
            return True, "运行时位于程序目录"
        dst = _safe_runtime_dir()
        marker = dst.parent / "copied.ok"
        version = "qemu-11.1.3"
        try:
            if dst.exists() and marker.exists() and marker.read_text(
                    encoding="ascii", errors="ignore") == version:
                return True, "运行时已就绪（安全目录）"
            if progress:
                progress("正在准备运行环境（首次需要复制文件，请稍候）…", 0.0)
            if dst.exists():
                shutil.rmtree(str(dst), ignore_errors=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(str(src), str(dst))
            marker.write_text(version, encoding="ascii")
            return True, "运行时已复制到安全目录"
        except Exception as e:
            return False, "无法准备运行环境：%s" % e
    # 没有本地 runtime 目录：尝试从 exe 内嵌数据解压
    if _payload_span():
        return _extract_payload(progress)
    return False, "未找到 QEMU 运行时（缺少 runtime 目录，且程序不含内嵌运行时）"


def find_file(root, names):
    """在 root 下递归查找文件名匹配任一关键词的文件。"""
    if not root.exists():
        return None
    for path in root.rglob("*"):
        if path.is_file() and any(k in path.name.lower() for k in names):
            return path
    return None


def qemu_exe():
    p = qemu_root() / "qemu-system-x86_64.exe"
    if p.exists():
        return p
    return find_file(qemu_root(), ["qemu-system-x86_64.exe"])


def qemu_img_exe():
    p = qemu_root() / "qemu-img.exe"
    if p.exists():
        return p
    return find_file(qemu_root(), ["qemu-img.exe"])


def _firmware_files():
    """返回固件目录下所有 .fd 文件，按 x86_64 优先排序。"""
    root = qemu_root()
    files = [p for p in root.rglob("*.fd")] if root.exists() else []
    def score(p):
        n = p.name.lower()
        s = 0
        if "x86_64" in n or "x64" in n:
            s -= 100          # x86_64 最优先
        if "secure" in n:
            s += 50           # secure boot 版排后
        if any(a in n for a in ("aarch64", "arm", "riscv", "loongarch", "i386")):
            s += 200          # 其他架构排最后
        return s
    return sorted(files, key=score)


def ovmf_code():
    for p in _firmware_files():
        n = p.name.lower()
        if ("ovmf" in n or "edk2" in n) and "code" in n and "vars" not in n:
            if "x86_64" in n or "x64" in n or not any(
                    a in n for a in ("aarch64", "arm", "riscv", "loongarch", "i386")):
                return p
    return None


def ovmf_vars():
    for p in _firmware_files():
        n = p.name.lower()
        if "vars" in n and ("ovmf" in n or "edk2" in n):
            if "x86_64" in n or "x64" in n:
                return p
    return None


def data_dir():
    d = app_dir() / "fnnas-data"
    try:
        d.mkdir(parents=True, exist_ok=True)
        t = d / ".write_check"
        t.write_text("ok", encoding="utf-8")
        t.unlink()
        return d
    except Exception:
        d = Path(os.environ.get("APPDATA", str(Path.home()))) / "FnNASLab"
        d.mkdir(parents=True, exist_ok=True)
        return d


CONFIG_PATH = data_dir() / "config.json"
LOG_PATH = data_dir() / "app.log"

# 小于该体积的 qcow2 视为"空盘"（刚创建、还没装系统）。
# 真实装好飞牛的系统盘至少数个 GB，用它来区分空壳盘和有效系统盘。
BLANK_DISK_BYTES = 2 * 1024 * 1024


def disk_volume_bytes(path):
    try:
        return Path(path).stat().st_size
    except Exception:
        return 0


def disk_is_blank(path):
    """qcow2 体积过小 = 尚未安装系统的空盘。"""
    return disk_volume_bytes(path) < BLANK_DISK_BYTES


def disk_search_dirs():
    """可能存放虚拟硬盘的目录，用于找回已有系统盘。"""
    dirs = []
    for d in (data_dir(), app_dir()):
        dirs.append(Path(d))
    # 历史硬链接目录
    pub = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Documents"
    dirs.append(pub / "fnnas-lab-links")
    # 配置里出现过的路径
    try:
        cfg = load_config()
        for k in ("system_disk", "disk_store_dir"):
            v = str(cfg.get(k) or "").strip()
            if v:
                p = Path(v)
                dirs.append(p.parent if p.suffix else p)
        for v in (cfg.get("extra_disks") or []):
            v = str(v).strip()
            if v:
                dirs.append(Path(v).parent)
    except Exception:
        pass
    # 各盘符根目录（用户自定义存储位置常选在根目录）
    for letter in "CDEFGH":
        dirs.append(Path(letter + ":\\"))
    seen, out = set(), []
    for d in dirs:
        key = str(d).lower()
        if key in seen:
            continue
        seen.add(key)
        if d.exists():
            out.append(d)
    return out


def find_existing_system_disks(min_bytes=1024 * 1024 * 1024):
    """扫描已知位置，返回体积超过 min_bytes 的 qcow2 磁盘，按体积降序。
    用来在"用户换了存储位置"时找回真正装过系统的盘，避免误建空盘。"""
    found = []
    for d in disk_search_dirs():
        try:
            for p in list(d.glob("*.qcow2")):
                try:
                    if p.stat().st_size >= min_bytes:
                        found.append(p)
                except Exception:
                    pass
        except Exception:
            pass
    found.sort(key=lambda p: p.stat().st_size, reverse=True)
    return found

# 默认自动转发的应用端口：覆盖飞牛常见应用（下载器、影音库、网盘、Docker 面板等）
DEFAULT_FORWARD_PORTS = [8080, 8081, 8090, 8096, 3000, 5000,
                         5244, 5700, 8000, 8888, 9000, 9090]

DEFAULT_CONFIG = {
    "iso_path": "",
    "system_disk": "",
    "extra_disks": [],
    "install_mode": "fresh",     # fresh | existing
    "ram_mb": 4096,
    "cpu_cores": 4,
    "web_port": 5666,
    "ssh_port": 2222,
    "qmp_port": 45700,
    "boot_mode": "bios",         # bios | uefi  (默认传统引导，兼容性最好)
    "accel": "auto",             # auto | whpx | tcg
    "display": "window",         # window | none
    "disk_bus": "virtio",        # virtio | sata
    "nic_model": "e1000",        # e1000 | virtio
    "new_disk_gb": 64,
    "new_data_gb": 64,
    "disk_store_dir": "",   # 用户自定义的虚拟硬盘存放目录，空表示默认程序目录
    # 需要自动转发到本机的应用端口（虚拟机上装的应用，用 localhost:端口 直接访问）
    "forward_ports": list(DEFAULT_FORWARD_PORTS),
    "fpk_path": "",
    "app_path": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    # 默认系统盘路径
    if not cfg.get("system_disk"):
        cfg["system_disk"] = str(data_dir() / "system.qcow2")
    return cfg


def save_config(cfg):
    try:
        CONFIG_PATH.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        return True, ""
    except Exception as e:
        return False, str(e)


class PathMapper:
    """
    把任意路径下的文件映射为纯英文路径，供 QEMU 使用。
    同盘符用 NTFS 硬链接（瞬时、不占空间），跨盘或失败则用副本。
    """
    def __init__(self, base):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.mapping = {}

    def _link(self, src, dst):
        # 同盘符优先硬链接
        if src.drive.lower() == dst.drive.lower():
            try:
                subprocess.run(["cmd", "/c", "mklink", "/H", str(dst), str(src)],
                               capture_output=True, timeout=30,
                               startupinfo=hidden_startupinfo())
                if dst.exists():
                    return True
            except Exception:
                pass
        # 回退：复制
        try:
            shutil.copy2(str(src), str(dst))
            return dst.exists()
        except Exception:
            return False

    def map(self, src, kind="disk"):
        """返回可用于 QEMU 的纯英文路径；输入已是英文则原样返回。"""
        src = Path(src)
        if not src.exists():
            return src
        if _is_ascii_path(src):
            return src
        key = str(src).lower()
        if key in self.mapping and Path(self.mapping[key]).exists():
            return Path(self.mapping[key])
        ext = src.suffix or ".bin"
        import hashlib
        h = hashlib.md5(str(src).encode("utf-8")).hexdigest()[:12]
        dst = self.base / ("%s_%s%s" % (kind, h, ext))
        if not dst.exists():
            ok = self._link(src, dst)
            if not ok:
                return src
        self.mapping[key] = str(dst)
        return dst


# 全局路径映射器（延迟初始化）
_path_mapper = None


def path_mapper():
    global _path_mapper
    if _path_mapper is None:
        base = _safe_base() / "fnnas-lab-links"
        _path_mapper = PathMapper(base)
    return _path_mapper


def qemu_path(src, kind="disk"):
    """把文件路径转成 QEMU 可访问的纯英文路径。"""
    return path_mapper().map(src, kind)


def find_running_qemu():
    """枚举系统中正在运行的 qemu-system-x86_64 进程，返回 [(pid, 路径)]。
    用于检测"程序重启前已启动、当前程序未接管"的虚拟机。"""
    result = []
    try:
        psapi = ctypes.windll.psapi
        kernel = ctypes.windll.kernel32
        arr = (wintypes.DWORD * 4096)()
        needed = wintypes.DWORD()
        psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(needed))
        count = needed.value // ctypes.sizeof(wintypes.DWORD)
        for i in range(count):
            pid = arr[i]
            h = kernel.OpenProcess(0x1000, False, pid)
            if not h:
                continue
            buf = ctypes.create_unicode_buffer(260)
            n = wintypes.DWORD(260)
            ok = kernel.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n))
            kernel.CloseHandle(h)
            if ok and "qemu-system-x86_64" in buf.value.lower():
                result.append((pid, buf.value))
    except Exception:
        pass
    return result


def hwnd_ptr(v):
    """把窗口句柄包成 c_void_p。
    Windows 句柄是 64 位指针，直接当 Python int 传给未声明 argtypes 的
    user32 函数时会被按 32 位整数处理，句柄值偏大就抛
    OverflowError: int too long to convert，导致嵌入等问题。"""
    return ctypes.c_void_p(int(v))


def pid_alive(pid):
    """判断进程号是否仍在运行（用于接管的"外部虚拟机"）。"""
    try:
        kernel = ctypes.windll.kernel32
        h = kernel.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        code = wintypes.DWORD()
        kernel.GetExitCodeProcess(h, ctypes.byref(code))
        kernel.CloseHandle(h)
        return code.value == 259        # STILL_ACTIVE
    except Exception:
        return False


def open_url_safe(url):
    """用 ShellExecute 打开 URL，避免 webbrowser 模块在 PyInstaller GUI 程序中的崩溃。"""
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "open", url, None, None, 1)
        return True
    except Exception:
        try:
            os.startfile(url)
            return True
        except Exception:
            return False


def port_in_use(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", int(port))) == 0
    finally:
        s.close()


def port_service_alive(port, timeout=1.0):
    """判断本机某端口背后是否真的有服务。
    QEMU 的端口转发对"虚拟机内没有服务"的端口会 accept 后立刻 RST，
    所以能收到数据、或连接能保持住，才算真的有应用在监听。"""
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout)
    except Exception:
        return False
    try:
        s.settimeout(timeout)
        try:
            s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            d = s.recv(1)
            return bool(d)
        except socket.timeout:
            # 没有立刻回数据，但连接没被重置——说明端口确实有服务（如 SSH）
            return True
        except ConnectionResetError:
            return False
        except Exception:
            return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def parse_forward_ports(raw):
    """把配置里的 forward_ports 统一成 [(host_port, guest_port)]。
    既兼容 8080 这种整数（本机与虚拟机同端口），
    也兼容 {"host": 5024, "guest": 5654} 这种端口不同的映射。"""
    out = []
    for item in (raw or []):
        try:
            if isinstance(item, dict):
                h = int(item.get("host"))
                g = int(item.get("guest") or h)
            else:
                h = int(item)
                g = h
            if 1 <= h <= 65535 and 1 <= g <= 65535:
                out.append((h, g))
        except Exception:
            continue
    return out


def whpx_available():
    """粗略判断 Windows 是否已启用 Hyper-V（WHPX 依赖它）。"""
    win = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    return (win / "System32" / "vmms.exe").exists()


def hidden_startupinfo():
    """返回一个隐藏窗口的 STARTUPINFO，安全替代 CREATE_NO_WINDOW。"""
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

class Logger:
    def __init__(self, sink):
        self.sink = sink

    def log(self, msg, level="信息"):
        line = "[%s] [%s] %s" % (time.strftime("%H:%M:%S"), level, msg)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        self.sink("log", level, line)


# ---------------------------------------------------------------------------
# 虚拟机控制器
# ---------------------------------------------------------------------------

class QemuController:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.logger = app.logger
        self.proc = None
        self.reader = None
        self._start_ts = 0
        self._retry_tcg = False
        self.embedded_hwnd = None   # 已嵌入到软件窗口的 SDL 主画面句柄
        self.external_pid = None    # 接管的外部 QEMU 进程号（非本程序启动）

    @property
    def running(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        if self.external_pid and pid_alive(self.external_pid):
            return True
        # 外部进程已退出，清理接管状态
        if self.external_pid:
            self.external_pid = None
        return False

    def attach_external(self, pid):
        """接管一个已在运行、但不是本程序启动的 QEMU 进程。
        接管后"停止虚拟机"、端口转发、画面嵌入等功能才能对外部虚拟机生效。"""
        try:
            pid = int(pid)
        except Exception:
            return False
        if not pid_alive(pid):
            return False
        self.external_pid = pid
        self.proc = None
        try:
            st = (self.qmp_call("query-status", timeout=3)
                  .get("return", {}).get("status"))
        except Exception as e:
            st = "未知"
            self.logger.log("接管外部虚拟机时管理接口不可用：%s" % e, "警告")
        self.logger.log("已接管已运行的虚拟机（进程 %d，状态 %s）" % (pid, st))
        return True

    def effective_accel(self, override=None):
        a = override or self.cfg.get("accel", "auto")
        if a == "auto":
            return "whpx" if whpx_available() else "tcg"
        return a

    def vars_path(self):
        """每个系统盘对应独立的 UEFI 变量文件（放在纯英文工作目录）。"""
        disk = Path(self.cfg["system_disk"])
        stem = re.sub(r"[^A-Za-z0-9_-]", "_", disk.stem)[:40] or "default"
        base = _safe_base() / "fnnas-lab-links"
        base.mkdir(parents=True, exist_ok=True)
        return base / ("ovmf-vars-%s.fd" % stem)

    def prepare_vars(self):
        dst = self.vars_path()
        src = ovmf_vars()
        if src and not dst.exists():
            shutil.copyfile(str(src), str(dst))
        return dst

    def build_command(self, accel_override=None):
        c = self.cfg
        exe = qemu_exe()
        if not exe:
            raise RuntimeError("未找到 QEMU 主程序，请检查 runtime/qemu 目录")

        accel = self.effective_accel(accel_override)
        cmd = [str(exe), "-machine", "q35"]

        if accel == "whpx":
            cmd += ["-accel", "whpx"]
        else:
            cmd += ["-accel", "tcg,thread=multi"]

        cmd += ["-m", str(int(c["ram_mb"]))]
        cmd += ["-smp", "cpus=%d" % int(c["cpu_cores"])]
        cmd += ["-cpu", "qemu64"]

        # 引导方式：默认 SeaBIOS（内置，无需外部固件）；UEFI 需完整固件
        if c.get("boot_mode", "bios") == "uefi":
            code = ovmf_code()
            var = ovmf_vars()
            if not code or not var:
                raise RuntimeError(
                    "当前 QEMU 运行时缺少完整的 x86_64 UEFI 固件（code/vars），"
                    "请在「虚拟机设置」中将「启动方式」改回 bios 后重试。")
            cmd += ["-drive", "if=pflash,format=raw,readonly=on,file=%s" % code]
            vars_p = self.prepare_vars()
            cmd += ["-drive", "if=pflash,format=raw,file=%s" % vars_p]
        # bios：QEMU 使用内置 SeaBIOS，无需额外参数

        # 网络：用户模式 + 端口转发（把飞牛的 5666/22 映射到本机）
        # 应用端口不写在这里：命令行里的 hostfwd 若绑定失败会让 QEMU 直接起不来，
        # 改为启动成功后用 QMP 热添加（见 _auto_forward_ports），个别端口冲突
        # 只影响那一个端口，不会拖垮整个虚拟机。
        fwd = "user,id=net0"
        fwd += ",hostfwd=tcp::%d-:5666" % int(c["web_port"])
        fwd += ",hostfwd=tcp::%d-:22" % int(c["ssh_port"])
        cmd += ["-netdev", fwd]
        nic = "virtio-net-pci" if c.get("nic_model") == "virtio" else "e1000"
        cmd += ["-device", "%s,netdev=net0" % nic]

        # 系统盘
        disk = Path(c["system_disk"])
        if not disk.exists():
            raise RuntimeError("系统盘不存在：%s\n请先在「准备镜像」中创建或选择。" % disk)
        disk = qemu_path(disk, "sysdisk")
        cmd += ["-drive", "file=%s,if=none,id=disk0,format=qcow2" % disk]
        if c.get("disk_bus") == "sata":
            cmd += ["-device", "ide-hd,drive=disk0,bus=ide.0"]
        else:
            cmd += ["-device", "virtio-blk-pci,drive=disk0"]

        # 额外数据盘（飞牛需要至少一个数据盘来创建存储空间）
        for i, extra in enumerate(c.get("extra_disks", [])):
            ep = Path(extra)
            if not ep.exists():
                continue
            ep = qemu_path(ep, "data")
            cmd += ["-drive", "file=%s,if=none,id=extra%d,format=qcow2" % (ep, i)]
            bus = "ide-hd,drive=extra%d,bus=ide.%d" % (i, i + 1) if c.get("disk_bus") == "sata" \
                else "virtio-blk-pci,drive=extra%d" % i
            cmd += ["-device", bus]

        # 安装镜像
        if c.get("install_mode") == "fresh" and c.get("iso_path"):
            iso = Path(c["iso_path"])
            if not iso.exists():
                raise RuntimeError("安装镜像不存在：%s" % iso)
            iso = qemu_path(iso, "iso")
            cmd += ["-cdrom", str(iso)]

        # 显示
        if c.get("display") == "none":
            cmd += ["-display", "none"]
        else:
            cmd += ["-display", "sdl"]
            # 绝对定位鼠标（USB tablet）：画面嵌入到本窗口后，鼠标无需
            # 按 Ctrl+Alt+G 捕获即可直接点击，位置精确对应。
            # 不接这块虚拟鼠标时，guest 只有 PS/2 相对鼠标，嵌入窗口里
            # 点不动（第一次点击只会被 SDL 当作"获取输入焦点"）。
            cmd += ["-usb", "-device", "usb-tablet"]
            cmd += ["-device", "usb-kbd"]

        # QMP 管理接口，用于优雅关机
        cmd += ["-qmp", "tcp:127.0.0.1:%d,server,nowait" % int(c["qmp_port"])]
        return cmd, accel

    def port_check(self):
        c = self.cfg
        used = []
        for name, key in (("Web 管理端口", "web_port"),
                          ("SSH 端口", "ssh_port"),
                          ("管理接口端口", "qmp_port")):
            if port_in_use(c[key]):
                used.append("%s %s" % (name, c[key]))
        return used

    def start(self, accel_override=None):
        if self.running:
            return False, "虚拟机已经在运行中"

        # 关闭上一次启动留下的日志句柄（硬件加速失败重试时会重新进入这里）
        try:
            if getattr(self, "_qemu_log_fh", None):
                self._qemu_log_fh.close()
        except Exception:
            pass
        self._qemu_log_fh = None

        used = self.port_check()
        if used:
            return False, "以下端口已被占用，请在「虚拟机设置」中修改：\n" + "\n".join(used)

        try:
            cmd, accel = self.build_command(accel_override)
        except Exception as e:
            return False, str(e)

        self.logger.log("启动虚拟机（加速模式：%s）" % accel)
        self.logger.log("命令：" + subprocess.list2cmdline(cmd), "命令")

        # 隐藏子进程窗口：不能用 CREATE_NO_WINDOW！
        # 在 PyInstaller 打包的 GUI 程序里，subprocess 加 CREATE_NO_WINDOW 会
        # 与父进程争用运行时 DLL，触发 ucrtbase 的 0xc0000409 fail-fast 崩溃。
        # 改用 STARTUPINFO 的 SW_HIDE，同样能隐藏窗口但不碰 DLL 锁。
        flags = 0
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
        # SDL 相关：允许点击直接落到未聚焦的嵌入窗口上（否则第一次点击
        # 只会让窗口获得焦点，不会传给虚拟机），并禁止失焦时最小化。
        env = os.environ.copy()
        env["SDL_MOUSE_FOCUS_CLICKTHROUGH"] = "1"
        env["SDL_VIDEO_MINIMIZE_ON_FOCUS_LOSS"] = "0"
        env["SDL_VIDEO_ALLOW_SCREENSAVER"] = "1"
        # 用日志文件而不是管道接收 QEMU 输出：
        # 若用 PIPE，本程序退出后管道关闭，QEMU 往已关闭的管道写日志会直接崩掉
        # （表现为"关掉软件 · 虚拟机也跟着消失"）。重定向到文件后，
        # 本程序退出不会影响虚拟机继续运行。
        try:
            log_path = data_dir() / "qemu-output.log"
            self._qemu_log_path = str(log_path)
            self._qemu_log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
        except Exception:
            self._qemu_log_path = None
            self._qemu_log_fh = None
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdout=(self._qemu_log_fh if self._qemu_log_fh else subprocess.DEVNULL),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags, startupinfo=startupinfo,
                cwd=str(qemu_root()), env=env)
        except Exception as e:
            self.proc = None
            return False, "无法启动 QEMU：%s" % e

        self._start_ts = time.time()
        self._retry_tcg = (accel == "whpx" and accel_override is None)
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.reader.start()
        # 后台线程等待 SDL 画面窗口出现，把它嵌入到软件的"虚拟机画面"页面；
        # 同时隐藏所有后台窗口（命令行黑窗、串口窗），对外只保留软件一个窗口。
        if self.cfg.get("display") != "none":
            threading.Thread(target=self._embed_sdl_window, daemon=True).start()
        # 启动成功后自动补上应用端口转发（不影响虚拟机自身启动）
        threading.Thread(target=self._auto_forward_ports, daemon=True).start()
        self.app.post_event("vm_started")
        return True, "虚拟机已启动"

    def _auto_forward_ports(self):
        """等 QMP 就绪后，把配置里的应用端口逐个转发。
        跳过已被本机其它程序占用的端口，避免和系统服务抢端口。"""
        pairs = parse_forward_ports(self.cfg.get("forward_ports"))
        if not pairs:
            return
        # 等 QMP 能应答（虚拟机刚起来时 QMP 可能还没监听）
        deadline = time.time() + 25
        while time.time() < deadline and self.running:
            try:
                self.qmp_call("query-status", timeout=2)
                break
            except Exception:
                time.sleep(0.6)
        else:
            return
        try:
            existing = {x["host"] for x in self.port_forward_list()}
        except Exception:
            existing = set()
        added = []
        for hp, gp in sorted(pairs):
            if hp in existing:
                continue
            if port_in_use(hp):
                self.logger.log("应用端口 %d 已被本机其它程序占用，跳过转发" % hp, "警告")
                continue
            ok, err = self.port_forward_add(hp, gp)
            if ok:
                added.append(hp if hp == gp else "%d→%d" % (hp, gp))
            else:
                self.logger.log("端口 %d 转发失败：%s" % (hp, err), "警告")
        if added:
            self.logger.log("已自动转发应用端口：" +
                            ", ".join(str(x) for x in added))
            self.app.post_event("ports_changed")

    def _embed_sdl_window(self, external_pid=None):
        """找到 QEMU 的 SDL 主画面窗口，用 SetParent 嵌入软件内的画面容器；
        隐藏命令行黑窗和其它 SDL 后台窗口。持续守护直到虚拟机停止。
        external_pid: 外部已运行虚拟机的进程号（当前程序未接管时）。"""
        import ctypes
        from ctypes import wintypes
        GWL_STYLE = -16
        WS_CHILD = 0x40000000
        WS_VISIBLE = 0x10000000
        WS_CAPTION = 0x00C00000
        WS_THICKFRAME = 0x00040000
        WS_SYSMENU = 0x00080000
        WS_POPUP = 0x80000000
        WS_MAXIMIZEBOX = 0x00010000
        WS_MINIMIZEBOX = 0x00020000
        SWP_NOZORDER = 0x0004
        SWP_FRAMECHANGED = 0x0020
        try:
            proc_id = external_pid or (self.proc.pid if self.proc else None)
            if not proc_id:
                return
            u = ctypes.windll.user32
            Proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            deadline = time.time() + 40
            # 外部 VM 时 self.running 为 False，用进程存活判断
            def alive():
                if external_pid:
                    import ctypes as _c
                    h = _c.windll.kernel32.OpenProcess(0x1000, False, external_pid)
                    if not h:
                        return False
                    _c.windll.kernel32.CloseHandle(h)
                    return True
                return self.running
            while time.time() < deadline and alive():
                found = []
                console = []
                def cb(h, l):
                    pid = wintypes.DWORD()
                    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
                    if pid.value == proc_id:
                        cls = ctypes.create_unicode_buffer(256)
                        u.GetClassNameW(h, cls, 256)
                        if cls.value == "SDL_app":
                            r = wintypes.RECT()
                            u.GetWindowRect(h, ctypes.byref(r))
                            found.append((h, (r.right - r.left) * (r.bottom - r.top)))
                        elif cls.value == "ConsoleWindowClass":
                            console.append(h)
                    return True
                try:
                    u.EnumWindows(Proc(cb), 0)
                except Exception:
                    pass

                # 隐藏所有后台窗口：命令行黑窗 + 多余 SDL 窗口
                for h in console:
                    u.ShowWindow(hwnd_ptr(h), 0)
                for h, _ in found[1:]:
                    u.ShowWindow(hwnd_ptr(h), 0)

                if found and self.embedded_hwnd is None:
                    found.sort(key=lambda x: x[1], reverse=True)
                    main = found[0][0]
                    # 取软件内画面容器的句柄（在主线程创建好的 tk 控件）
                    host = self.app.get_screen_hwnd()
                    if host:
                        try:
                            u.SetParent(hwnd_ptr(main), hwnd_ptr(host))
                            style = u.GetWindowLongW(hwnd_ptr(main), GWL_STYLE)
                            style &= ~(WS_POPUP | WS_CAPTION | WS_THICKFRAME |
                                       WS_SYSMENU | WS_MAXIMIZEBOX | WS_MINIMIZEBOX)
                            style |= WS_CHILD | WS_VISIBLE
                            u.SetWindowLongW(hwnd_ptr(main), GWL_STYLE, style)
                            r = wintypes.RECT()
                            u.GetWindowRect(hwnd_ptr(host), ctypes.byref(r))
                            u.SetWindowPos(hwnd_ptr(main), None, 0, 0,
                                           max(10, r.right - r.left),
                                           max(10, r.bottom - r.top),
                                           SWP_NOZORDER | SWP_FRAMECHANGED)
                            u.ShowWindow(hwnd_ptr(main), 5)   # SW_SHOW
                            self.embedded_hwnd = main
                            self.focus_embedded()   # 把键盘焦点交给虚拟机画面
                            self.app.on_vm_embedded(main)
                            self.logger.log("虚拟机画面已嵌入软件窗口")
                        except Exception as e:
                            self.logger.log("画面嵌入失败，退回独立窗口：%s" % e, "警告")
                            u.ShowWindow(hwnd_ptr(main), 9)
                            for h, _ in found[1:]:
                                u.ShowWindow(hwnd_ptr(h), 0)
                time.sleep(0.5)
        except Exception:
            pass

    def resize_embedded(self, width, height):
        """容器尺寸变化时，调整嵌入的 SDL 画面填满容器。"""
        if not self.embedded_hwnd or width < 10 or height < 10:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            u.SetWindowPos(hwnd_ptr(self.embedded_hwnd), None, 0, 0,
                           int(width), int(height), SWP_NOZORDER | SWP_FRAMECHANGED)
        except Exception:
            pass

    def focus_embedded(self):
        """把键盘焦点交给嵌入的虚拟机画面。
        SDL 窗口属于 QEMU 进程，跨线程 SetFocus 必须先 AttachThreadInput，
        否则调用会静默失败，导致键盘按键进不到虚拟机。"""
        if not self.embedded_hwnd:
            return
        try:
            import ctypes
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            target = self.embedded_hwnd
            tid_target = u.GetWindowThreadProcessId(hwnd_ptr(target), None)
            tid_self = k.GetCurrentThreadId()
            if tid_target and tid_self and tid_target != tid_self:
                u.AttachThreadInput(tid_self, tid_target, True)
                u.SetFocus(hwnd_ptr(target))
                u.AttachThreadInput(tid_self, tid_target, False)
            else:
                u.SetFocus(hwnd_ptr(target))
            # 确保 SDL 画面在容器内处于最上层，避免点击被上层控件截走
            # HWND_TOP=0，SWP_NOSIZE|SWP_NOMOVE（不加 SWP_NOZORDER 才会真的置顶）
            u.SetWindowPos(hwnd_ptr(target), None, 0, 0, 0, 0, 0x0001 | 0x0002)
        except Exception:
            pass

    def release_embedded(self, show=True):
        """把嵌入的 SDL 画面窗口还给系统（还原成独立顶层窗口）。
        必须在软件窗口销毁之前调用——否则作为子窗口的虚拟机画面会被
        连带销毁，虚拟机还在跑却再也看不到画面。"""
        if not self.embedded_hwnd:
            return False
        hwnd = self.embedded_hwnd
        self.embedded_hwnd = None
        try:
            import ctypes
            u = ctypes.windll.user32
            GWL_STYLE = -16
            WS_CHILD = 0x40000000
            WS_POPUP = 0x80000000
            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            WS_SYSMENU = 0x00080000
            WS_MINIMIZEBOX = 0x00020000
            WS_MAXIMIZEBOX = 0x00010000
            WS_VISIBLE = 0x10000000
            u.SetParent(hwnd_ptr(hwnd), None)
            style = u.GetWindowLongW(hwnd_ptr(hwnd), GWL_STYLE)
            style &= ~WS_CHILD
            style |= (WS_POPUP | WS_CAPTION | WS_THICKFRAME | WS_SYSMENU |
                      WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_VISIBLE)
            u.SetWindowLongW(hwnd_ptr(hwnd), GWL_STYLE, style)
            if show:
                sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
                w, h = min(1024, sw - 80), min(700, sh - 100)
                u.SetWindowPos(hwnd_ptr(hwnd), None,
                               max(0, (sw - w) // 2), max(0, (sh - h) // 2),
                               w, h, 0x0004 | 0x0020 | 0x0040)  # NOZORDER|FRAMECHANGED|SHOWWINDOW
                u.ShowWindow(hwnd_ptr(hwnd), 9)
            return True
        except Exception:
            return False

    def detach_to_window(self):
        """兜底：把嵌入的画面弹回独立窗口（嵌入模式下若鼠标/键盘异常，
        用户可一键弹出，用独立窗口完成安装）。"""
        if not self.embedded_hwnd:
            return False
        hwnd = self.embedded_hwnd
        ok = self.release_embedded(show=True)
        if ok:
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(hwnd_ptr(hwnd))
            except Exception:
                pass
            self.logger.log("虚拟机画面已弹出为独立窗口")
            return True
        self.logger.log("弹出画面失败", "警告")
        return False

    def detach_embedded(self):
        """虚拟机停止后清理嵌入引用。"""
        self.embedded_hwnd = None

    def _read_output(self):
        proc = self.proc
        path = getattr(self, "_qemu_log_path", None)
        pos = 0
        # QEMU 的输出重定向到了 qemu-output.log，这里增量读取该文件写进日志面板。
        while True:
            alive = proc.poll() is None
            if path:
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    for line in chunk.splitlines():
                        if line.strip():
                            self.logger.log(line, "QEMU")
                except Exception:
                    pass
            if not alive:
                break
            time.sleep(0.5)
        code = proc.poll()
        if code is None:
            code = proc.wait()
        if self._retry_tcg and code != 0 and time.time() - self._start_ts < 10:
            self._retry_tcg = False
            self.logger.log("硬件加速启动失败，自动切换到兼容模式重试。", "警告")
            self.app.post_event("accel_fallback")
            ok, msg = self.start(accel_override="tcg")
            if not ok:
                self.logger.log("兼容模式启动失败：" + msg, "错误")
                self.app.post_event("vm_stopped")
        else:
            self.logger.log("虚拟机已退出，退出码：%s" % code)
            self.app.post_event("vm_stopped")

    def _qmp(self, command, wait_reply=True):
        s = socket.create_connection(("127.0.0.1", int(self.cfg["qmp_port"])), timeout=3)
        s.settimeout(5)
        f = s.makefile("rwb")
        f.readline()  # 欢迎信息
        f.write(b'{"execute":"qmp_capabilities"}\n')
        f.flush()
        if wait_reply:
            f.readline()
        f.write((json.dumps({"execute": command}) + "\n").encode("utf-8"))
        f.flush()
        if wait_reply:
            f.readline()
        s.close()

    def qmp_call(self, command, arguments=None, timeout=6):
        """执行一条 QMP 命令并返回解析后的响应字典。"""
        s = socket.create_connection(("127.0.0.1", int(self.cfg["qmp_port"])), timeout=3)
        s.settimeout(timeout)
        f = s.makefile("rwb")
        try:
            f.readline()
            f.write(b'{"execute":"qmp_capabilities"}\n')
            f.flush()
            f.readline()
            msg = {"execute": command}
            if arguments:
                msg["arguments"] = arguments
            f.write((json.dumps(msg) + "\n").encode("utf-8"))
            f.flush()
            return json.loads(f.readline().decode("utf-8", "replace"))
        finally:
            s.close()

    def hmp(self, cmdline, timeout=8):
        """执行 HMP 监视器命令，返回输出文本。"""
        r = self.qmp_call("human-monitor-command", {"command-line": cmdline}, timeout)
        return r.get("return") or ""

    # ---------- 端口转发（应用访问）----------
    def port_forward_add(self, host_port, guest_port=None):
        """运行中动态添加端口转发，立即生效，无需重启虚拟机。"""
        try:
            hp = int(host_port)
            gp = int(guest_port or host_port)
            if not (1 <= hp <= 65535 and 1 <= gp <= 65535):
                return False, "端口必须在 1~65535 之间"
            out = self.hmp("hostfwd_add tcp::%d-:%d" % (hp, gp))
            if out.strip().startswith("Error") or "failed" in out.lower():
                return False, out.strip()
            return True, ""
        except Exception as e:
            return False, str(e)

    def port_forward_remove(self, host_port):
        try:
            hp = int(host_port)
            out = self.hmp("hostfwd_remove tcp::%d" % hp)
            if out.strip().startswith("Error"):
                return False, out.strip()
            return True, ""
        except Exception as e:
            return False, str(e)

    def port_forward_list(self):
        """返回当前生效的转发列表 [{'host':h,'guest':g}]。"""
        try:
            out = self.hmp("info usernet")
        except Exception:
            return []
        res = []
        for line in out.splitlines():
            if "HOST_FORWARD" not in line:
                continue
            m = re.search(r"TCP\[HOST_FORWARD\]\s+\S+\s+\S+\s+(\d+)\s+\S+\s+(\d+)", line)
            if m:
                res.append({"host": int(m.group(1)), "guest": int(m.group(2))})
        res.sort(key=lambda x: x["host"])
        return res

    def scan_guest_ports(self, ranges=None, on_progress=None):
        """扫描虚拟机里正在监听的应用端口。
        做法：逐端口临时建一条 hostfwd（宿主机用高位临时端口）探测，探测完立即移除，
        因此不会干扰现有转发，也不需要事先知道端口号。"""
        if not self.running:
            return []
        if ranges is None:
            ranges = [(80, 810), (3000, 3100), (5000, 5100), (5600, 5700),
                      (7000, 7100), (8000, 8200), (9000, 9100),
                      (10000, 10100), (18000, 18100)]
        try:
            existing = {x["host"] for x in self.port_forward_list()}
        except Exception:
            existing = set()
        hits = []
        total = sum(max(0, hi - lo) for lo, hi in ranges)
        done = 0
        tmp = 23000
        for lo, hi in ranges:
            for p in range(lo, hi):
                done += 1
                if on_progress:
                    try:
                        on_progress(done, total, p)
                    except Exception:
                        pass
                if p in existing:
                    # 已经在转发的端口也要确认背后真的有服务，一并列出
                    try:
                        if port_service_alive(p, 0.4):
                            hits.append(p)
                    except Exception:
                        pass
                    continue
                tmp += 1
                if tmp > 25900:
                    tmp = 23000
                out = self.hmp("hostfwd_add tcp::%d-:%d" % (tmp, p))
                if out.strip().startswith("Error") or "failed" in out.lower():
                    continue
                alive = False
                try:
                    alive = port_service_alive(tmp, 0.35)
                except Exception:
                    alive = False
                finally:
                    self.hmp("hostfwd_remove tcp::%d" % tmp)
                if alive:
                    hits.append(p)
        return sorted(set(hits))

    def stop(self, graceful=True):
        if not self.running:
            return False, "虚拟机未在运行"
        is_external = self.proc is None
        self._retry_tcg = False
        if graceful:
            try:
                self._qmp("system_powerdown", wait_reply=False)
                self.logger.log("已发送关机指令，等待系统关闭（最多 30 秒）…")
                for _ in range(60):
                    if not self.running:
                        self.external_pid = None
                        self.app.post_event("vm_stopped")
                        return True, "虚拟机已正常关闭"
                    time.sleep(0.5)
            except Exception as e:
                self.logger.log("优雅关机不可用：%s" % e, "警告")

        # 外部接管的进程没有 Popen 句柄，只能按 PID 结束
        if is_external:
            try:
                kernel = ctypes.windll.kernel32
                h = kernel.OpenProcess(0x0001, False, int(self.external_pid))  # TERMINATE
                if h:
                    kernel.TerminateProcess(h, 1)
                    kernel.CloseHandle(h)
            except Exception as e:
                self.logger.log("结束外部虚拟机失败：%s" % e, "警告")
            for _ in range(20):
                if not self.external_pid or not pid_alive(self.external_pid):
                    break
                time.sleep(0.3)
            self.external_pid = None
            self.logger.log("虚拟机已强制关闭（接管的外部进程）")
            self.app.post_event("vm_stopped")
            return True, "虚拟机已强制关闭"

        try:
            self.proc.terminate()
            self.proc.wait(timeout=6)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        self.logger.log("虚拟机已强制关闭")
        self.app.post_event("vm_stopped")
        return True, "虚拟机已关闭"


class VMTools:
    """虚拟机维护工具：快照还原、一键删除。"""

    def __init__(self, app):
        self.app = app
        self.logger = app.logger

    def _run_qemu_img(self, args, timeout=180):
        qi = qemu_img_exe()
        if not qi:
            return False, "未找到 qemu-img.exe"
        cmd = [str(qi)] + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="gbk", errors="replace",
                               startupinfo=hidden_startupinfo(), timeout=timeout)
            out = (r.stdout or "") + (r.stderr or "")
            if r.returncode != 0:
                return False, out.strip() or "命令执行失败"
            return True, out.strip()
        except Exception as e:
            return False, str(e)

    def disk_list(self):
        """返回当前虚拟机的所有磁盘路径（系统盘 + 数据盘）。"""
        c = self.app.cfg
        disks = []
        sysdisk = c.get("system_disk", "")
        if sysdisk and Path(sysdisk).exists():
            disks.append(Path(sysdisk))
        for d in c.get("extra_disks", []):
            if d and Path(d).exists():
                disks.append(Path(d))
        return disks

    # ---------- 快照 ----------
    def snapshot_status(self):
        """检查系统盘是否已有初始状态快照。"""
        disks = self.disk_list()
        if not disks:
            return False, "未找到系统盘"
        ok, out = self._run_qemu_img(["snapshot", "-l", str(disks[0])], timeout=30)
        if not ok:
            return False, out
        has = INIT_SNAPSHOT in out
        return has, out

    def snapshot_save(self):
        """为所有磁盘创建初始状态快照。"""
        disks = self.disk_list()
        if not disks:
            return False, "未找到系统盘，无法创建快照"
        saved = []
        for disk in disks:
            ok, out = self._run_qemu_img(["snapshot", "-c", INIT_SNAPSHOT, str(disk)])
            if not ok:
                # 若快照已存在则视为成功
                if "already exists" in out.lower() or "already" in out.lower():
                    saved.append(disk.name)
                    continue
                return False, "为 %s 创建快照失败：%s" % (disk.name, out)
            saved.append(disk.name)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.app.cfg["snapshot_time"] = ts
        save_config(self.app.cfg)
        self.logger.log("已保存初始状态快照：%s" % ", ".join(saved))
        return True, "初始状态已保存（%s）" % ts

    def snapshot_restore(self):
        """把所有磁盘还原到初始状态快照。"""
        disks = self.disk_list()
        if not disks:
            return False, "未找到系统盘，无法还原"
        restored = []
        for disk in disks:
            ok, out = self._run_qemu_img(["snapshot", "-a", INIT_SNAPSHOT, str(disk)])
            if not ok:
                return False, "还原 %s 失败：%s" % (disk.name, out)
            restored.append(disk.name)
        # 清理对应的 UEFI 变量，让系统像初次启动一样
        try:
            vp = self.app.qemu.vars_path()
            if vp.exists():
                vp.unlink()
        except Exception:
            pass
        self.logger.log("已还原初始状态：%s" % ", ".join(restored))
        return True, "已还原到初始状态"

    # ---------- 删除 ----------
    def deletion_plan(self):
        """返回将要删除/清理的文件清单，供确认对话框展示。"""
        c = self.app.cfg
        plan = []
        for label, key in (("系统盘", "system_disk"),):
            p = c.get(key, "")
            if p and Path(p).exists():
                plan.append((label, str(p), Path(p).stat().st_size))
        for i, d in enumerate(c.get("extra_disks", [])):
            if d and Path(d).exists():
                plan.append(("数据盘 %d" % (i + 1), str(d), Path(d).stat().st_size))
        # UEFI 变量文件
        try:
            vp = self.app.qemu.vars_path()
            if vp.exists():
                plan.append(("UEFI 变量", str(vp), vp.stat().st_size))
        except Exception:
            pass
        # 仅列出"当前虚拟机磁盘"对应的路径映射副本，绝不动其他虚拟机的映射。
        # 用硬链接的 inode（Windows 上为文件索引号）精确匹配同源文件。
        try:
            linkbase = _safe_base() / "fnnas-lab-links"
            if linkbase.exists():
                my_inodes = set()
                for p in [c.get("system_disk", "")] + list(c.get("extra_disks", [])):
                    if p and Path(p).exists():
                        try:
                            my_inodes.add(Path(p).stat().st_ino)
                        except Exception:
                            pass
                for f in linkbase.glob("*.qcow2"):
                    try:
                        if f.stat().st_ino in my_inodes:
                            plan.append(("路径映射副本", str(f), f.stat().st_size))
                    except Exception:
                        pass
        except Exception:
            pass
        return plan

    def delete_vm(self, keep_backup=True):
        """删除虚拟机。keep_backup=True 时先移动到备份目录而不是直接删除。"""
        c = self.app.cfg
        plan = self.deletion_plan()
        if not plan:
            return False, "没有可删除的虚拟机文件"
        errors = []
        backup_dir = None
        if keep_backup:
            backup_dir = data_dir() / ("backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return False, "创建备份目录失败：%s" % e

        for label, path, _ in plan:
            p = Path(path)
            if not p.exists():
                continue
            try:
                if keep_backup and backup_dir:
                    # 同名冲突时加序号
                    target = backup_dir / p.name
                    n = 1
                    while target.exists():
                        target = backup_dir / ("%s_%d" % (p.name, n))
                        n += 1
                    shutil.move(str(p), str(target))
                else:
                    p.unlink()
            except Exception as e:
                errors.append("%s: %s" % (p.name, e))

        # 清空配置中的磁盘引用，回到未完成状态
        c["system_disk"] = str(data_dir() / "system.qcow2")
        c["extra_disks"] = []
        c["install_mode"] = "fresh"
        c["iso_path"] = ""
        c["snapshot_time"] = ""
        save_config(c)
        self.app.qemu.cfg = c

        if errors:
            return False, "部分文件未处理成功：\n" + "\n".join(errors)
        where = ("文件已移动到备份目录：\n%s" % backup_dir) if keep_backup \
            else "文件已永久删除"
        self.logger.log("虚拟机已删除（%s）" % ("保留备份" if keep_backup else "永久删除"))
        return True, where


# ---------------------------------------------------------------------------
# GUI 组件
# ---------------------------------------------------------------------------

class ScrollFrame(ttk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, **kw)
        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#F5F7FA")
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        # 绑定到自身而非全局 bind_all：避免 6 个实例重复注册全局处理器
        # （全局重复绑定是 tkinter 进程级崩溃的常见诱因）。
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.inner.bind("<MouseWheel>", self._wheel)

    def _wheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            return "break"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.events = queue.Queue()
        self.logger = Logger(self._sink)
        self.qemu = QemuController(self)
        self.tools = VMTools(self)
        self.steps = []
        self.current_step = 0

        self.title("%s  v%s" % (APP_NAME, APP_VER))
        self.geometry("1120x760")
        self.minsize(1000, 660)
        self.configure(bg="#F5F7FA")
        self._styles()
        self._build_ui()
        self._pump()
        self._refresh()

    # ---------- 样式 ----------
    def _styles(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure(".", background="#F5F7FA", foreground="#1F2937",
                    font=("Microsoft YaHei UI", 10))
        s.configure("TFrame", background="#F5F7FA")
        s.configure("Card.TFrame", background="#FFFFFF")
        s.configure("TLabel", background="#F5F7FA", foreground="#1F2937")
        s.configure("Card.TLabel", background="#FFFFFF", foreground="#1F2937")
        s.configure("Title.TLabel", background="#F5F7FA", foreground="#111827",
                    font=("Microsoft YaHei UI", 17, "bold"))
        s.configure("Sub.TLabel", background="#F5F7FA", foreground="#6B7280",
                    font=("Microsoft YaHei UI", 9))
        s.configure("Head.TLabel", background="#FFFFFF", foreground="#111827",
                    font=("Microsoft YaHei UI", 12, "bold"))
        s.configure("Body.TLabel", background="#FFFFFF", foreground="#374151")
        s.configure("Hint.TLabel", background="#FFFFFF", foreground="#6B7280",
                    font=("Microsoft YaHei UI", 9))
        s.configure("Nav.TButton", font=("Microsoft YaHei UI", 10),
                    padding=(12, 10), anchor="w")
        s.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 6))
        s.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                    padding=(12, 8), background="#2563EB", foreground="#FFFFFF")
        s.map("Primary.TButton", background=[("active", "#1D4ED8"),
                                              ("disabled", "#93C5FD")])
        s.configure("Danger.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                    padding=(12, 8), background="#DC2626", foreground="#FFFFFF")
        s.map("Danger.TButton", background=[("active", "#B91C1C"),
                                             ("disabled", "#FCA5A5")])
        s.configure("TEntry", padding=5)
        s.configure("TCheckbutton", background="#FFFFFF")
        s.configure("TRadiobutton", background="#FFFFFF")
        s.configure("Good.TLabel", background="#FFFFFF", foreground="#059669",
                    font=("Microsoft YaHei UI", 10, "bold"))
        s.configure("Bad.TLabel", background="#FFFFFF", foreground="#DC2626",
                    font=("Microsoft YaHei UI", 10, "bold"))
        # 步骤成功提示（绿色）
        s.configure("Success.TLabel", background="#ECFDF5", foreground="#047857",
                    font=("Microsoft YaHei UI", 10, "bold"))
        s.configure("SuccessNav.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                    padding=(12, 10), anchor="w", foreground="#047857",
                    background="#ECFDF5")
        s.map("SuccessNav.TButton", background=[("active", "#D1FAE5")])

    # ---------- 整体布局 ----------
    def _build_ui(self):
        top = ttk.Frame(self, height=74)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        ttk.Label(top, text="飞牛 fnOS 本地测试环境", style="Title.TLabel").pack(
            side="left", padx=(22, 0), pady=(12, 0))
        ttk.Label(top, text="在 PC 上运行飞牛系统 · 安装并测试 FPK 应用",
                  style="Sub.TLabel").pack(side="left", padx=(14, 0), pady=(18, 0))

        # 右上角：只保留运行状态 + 管理地址展示（功能按钮统一放左侧）
        status_box = ttk.Frame(top)
        status_box.pack(side="right", padx=(0, 20))
        self.status_var = tk.StringVar(value="● 虚拟机未运行")
        self.status_lbl = ttk.Label(status_box, textvariable=self.status_var,
                                    foreground="#9CA3AF",
                                    font=("Microsoft YaHei UI", 10, "bold"),
                                    anchor="e")
        self.status_lbl.pack(anchor="e")
        self.addr_var = tk.StringVar(value="管理地址：—")
        self.addr_lbl = tk.Label(status_box, textvariable=self.addr_var,
                                 foreground="#2563EB",
                                 font=("Microsoft YaHei UI", 9),
                                 cursor="hand2", anchor="e")
        self.addr_lbl.pack(anchor="e")
        self.addr_lbl.bind("<Button-1>", lambda e: self.open_web() if self.addr_var.get().find(":") > 0 else None)

        # 顶部状态横幅：检测到虚拟机已运行时显示（纯文字提示，功能按钮统一在左侧）
        self.banner = tk.Frame(self, bg="#D1FAE5", height=44)
        self.banner_lbl = tk.Label(
            self.banner, text="", bg="#D1FAE5", fg="#065F46",
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.banner_lbl.pack(side="left", padx=18, fill="x", expand=True)

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True)
        self._body_frame = body

        nav = ttk.Frame(body, width=210)
        nav.pack(side="left", fill="y")
        nav.pack_propagate(False)

        # 左侧快捷操作区：所有功能按钮统一放这里
        ttk.Label(nav, text="快捷操作", font=("Microsoft YaHei UI", 11, "bold"),
                  foreground="#6B7280").pack(anchor="w", padx=18, pady=(16, 8))
        self.start_btn = ttk.Button(nav, text="▶ 启动虚拟机", command=self.start_vm,
                                    style="Primary.TButton")
        self.start_btn.pack(fill="x", padx=12, pady=3)
        self.stop_btn = ttk.Button(nav, text="■ 停止虚拟机", command=self.stop_vm,
                                   style="Danger.TButton", state="disabled")
        self.stop_btn.pack(fill="x", padx=12, pady=3)
        ttk.Button(nav, text="🌐 打开管理界面", command=self.open_web).pack(
            fill="x", padx=12, pady=3)

        ttk.Label(nav, text="操作向导", font=("Microsoft YaHei UI", 11, "bold"),
                  foreground="#6B7280").pack(anchor="w", padx=18, pady=(18, 8))

        titles = ["环境自检", "准备镜像", "虚拟机设置", "启动与初始化",
                  "安装 FPK 应用", "打开应用界面"]
        self.step_done = [False] * len(titles)
        self.step_badges = []
        self.step_labels = titles
        for i, t in enumerate(titles):
            row = ttk.Frame(nav)
            row.pack(fill="x", padx=10, pady=3)
            b = ttk.Button(row, text="  %d. %s" % (i + 1, t), style="Nav.TButton",
                           command=lambda k=i: self.show_step(k))
            b.pack(side="left", fill="x", expand=True)
            badge = ttk.Label(row, text="", foreground="#047857",
                              font=("Microsoft YaHei UI", 12, "bold"),
                              background="#F5F7FA", width=2)
            badge.pack(side="right", padx=(2, 6))
            self.steps.append(b)
            self.step_badges.append(badge)
        self.progress_var = tk.StringVar(value="完成进度：0 / 6")
        ttk.Label(nav, textvariable=self.progress_var,
                  foreground="#059669",
                  font=("Microsoft YaHei UI", 9, "bold")).pack(
                      anchor="w", padx=18, pady=(16, 2))
        ttk.Label(nav, text="按顺序操作即可完成全部配置",
                  style="Sub.TLabel", wraplength=180).pack(
                      anchor="w", padx=18, pady=6)

        # 独立页面：虚拟机画面（嵌入显示，不计入 6 步进度）
        ttk.Separator(nav).pack(fill="x", padx=14, pady=(10, 6))
        self.screen_nav_btn = ttk.Button(
            nav, text="  🖥 虚拟机画面", style="Nav.TButton",
            command=lambda: self.show_step(6))
        self.screen_nav_btn.pack(fill="x", padx=10, pady=3)
        self.ports_nav_btn = ttk.Button(
            nav, text="  ⇄ 应用端口转发", style="Nav.TButton",
            command=lambda: self.show_step(7))
        self.ports_nav_btn.pack(fill="x", padx=10, pady=3)

        self.container = ttk.Frame(body)
        self.container.pack(side="left", fill="both", expand=True, padx=(6, 16), pady=14)

        self.frames = [
            self._step_env(), self._step_image(), self._step_settings(),
            self._step_run(), self._step_fpk(), self._step_app(),
            self._step_screen(),   # index 6: 虚拟机画面
            self._step_ports(),    # index 7: 应用端口转发
        ]
        for f in self.frames:
            f.place(in_=self.container, x=0, y=0, relwidth=1, relheight=1)

        bottom = ttk.Frame(self, height=34)
        bottom.pack(side="bottom", fill="x")
        self.foot_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.foot_var, style="Sub.TLabel").pack(
            side="left", padx=18)
        ttk.Label(bottom, text="日志：%s" % LOG_PATH, style="Sub.TLabel").pack(
            side="right", padx=18)

    def _card(self, parent, title=None):
        card = ttk.Frame(parent, style="Card.TFrame")
        card.pack(fill="x", padx=2, pady=(0, 14))
        if title:
            ttk.Label(card, text=title, style="Head.TLabel").pack(
                anchor="w", padx=18, pady=(16, 8))
        return card

    def _para(self, parent, text, wrap=880):
        l = ttk.Label(parent, text=text, style="Body.TLabel",
                      wraplength=wrap, justify="left")
        l.pack(anchor="w", padx=18, pady=4)
        return l

    def _hint(self, parent, text, wrap=880):
        l = ttk.Label(parent, text=text, style="Hint.TLabel",
                      wraplength=wrap, justify="left")
        l.pack(anchor="w", padx=18, pady=(2, 10))
        return l

    # ---------- 第 1 步：环境自检 ----------
    def _step_env(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 1 步 · 环境自检", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "程序内置了 QEMU 虚拟机运行时，无需另行安装。下面先检查组件是否完整、"
                      "电脑是否已开启硬件虚拟化。", 880)
        card = self._card(c, "检测结果")
        ttk.Button(card, text="重新检测", command=self.check_env).pack(
            anchor="e", padx=18, pady=(0, 8))
        self.env_tree = ttk.Treeview(card, columns=("item", "status", "detail"),
                                     show="headings", height=5)
        self.env_tree.heading("item", text="检查项")
        self.env_tree.heading("status", text="状态")
        self.env_tree.heading("detail", text="说明")
        self.env_tree.column("item", width=180)
        self.env_tree.column("status", width=90)
        self.env_tree.column("detail", width=580)
        self.env_tree.pack(fill="x", padx=18, pady=(0, 16))

        card2 = self._card(c, "重要提示：建议开启硬件虚拟化")
        self._para(card2,
                   "若「硬件虚拟化」显示未开启，虚拟机仍能运行，但速度会很慢（安装系统可能需 30 分钟以上）。"
                   "开启方法：重启电脑进入 BIOS，开启 Intel VT-x / AMD-V（不同主板名称略有差异）；"
                   "或在 Windows「启用或关闭 Windows 功能」中勾选 Hyper-V 后重启。", 840)
        return f

    # ---------- 第 2 步：准备镜像 ----------
    def _step_image(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 2 步 · 准备飞牛镜像", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "选择安装方式。第一次使用请选择「全新安装」并提供飞牛 ISO 安装镜像；"
                      "以后可直接切换到已有的系统盘，实现一键更换系统镜像。", 880)

        card = self._card(c, "安装方式")
        self.mode_var = tk.StringVar(value=self.cfg.get("install_mode", "fresh"))
        ttk.Radiobutton(card, text="全新安装（使用 ISO 安装镜像）",
                        variable=self.mode_var, value="fresh",
                        command=self._mode_changed).pack(anchor="w", padx=18, pady=4)
        ttk.Radiobutton(card, text="使用已有系统盘（跳过安装，直接启动）",
                        variable=self.mode_var, value="existing",
                        command=self._mode_changed).pack(anchor="w", padx=18, pady=4)

        card2 = self._card(c, "安装镜像 ISO")
        ttk.Button(card2, text="打开飞牛官网下载",
                   command=lambda: open_url_safe("https://www.fnnas.com/download")).pack(
                       anchor="e", padx=18, pady=(12, 0))
        row = ttk.Frame(card2, style="Card.TFrame")
        row.pack(fill="x", padx=18, pady=8)
        ttk.Label(row, text="ISO 文件：", style="Card.TLabel").pack(side="left")
        self.iso_var = tk.StringVar(value=self.cfg.get("iso_path", ""))
        ttk.Entry(row, textvariable=self.iso_var, width=68).pack(side="left", padx=6)
        ttk.Button(row, text="浏览…", command=self.pick_iso).pack(side="left")

        card3 = self._card(c, "创建虚拟硬盘（系统盘 + 数据盘）")
        self._para(card3,
                   "选择存放位置，一次即可同时创建系统盘和数据盘。飞牛需要数据盘来创建「存储空间」，"
                   "建议一并创建。", 860)

        # 存储位置（用户自定义）
        row_dir = ttk.Frame(card3, style="Card.TFrame")
        row_dir.pack(fill="x", padx=18, pady=(10, 4))
        ttk.Label(row_dir, text="存储位置：", style="Card.TLabel").pack(side="left")
        default_dir = str(self.cfg.get("disk_store_dir", "") or data_dir())
        self.diskdir_var = tk.StringVar(value=default_dir)
        ttk.Entry(row_dir, textvariable=self.diskdir_var, width=62).pack(side="left", padx=6)
        ttk.Button(row_dir, text="选择文件夹…", command=self.pick_disk_dir).pack(side="left")

        # 系统盘 / 数据盘大小 + 一键创建
        row2 = ttk.Frame(card3, style="Card.TFrame")
        row2.pack(fill="x", padx=18, pady=8)
        ttk.Label(row2, text="系统盘（GB）：", style="Card.TLabel").pack(side="left")
        self.size_var = tk.StringVar(value=str(self.cfg.get("new_disk_gb", 64)))
        ttk.Spinbox(row2, from_=32, to=512, textvariable=self.size_var,
                    width=8).pack(side="left", padx=(6, 18))
        ttk.Label(row2, text="数据盘（GB）：", style="Card.TLabel").pack(side="left")
        self.data_size_var = tk.StringVar(value=str(self.cfg.get("new_data_gb", 64)))
        ttk.Spinbox(row2, from_=0, to=4096, textvariable=self.data_size_var,
                    width=8).pack(side="left", padx=(6, 18))
        ttk.Button(row2, text="同时创建两块硬盘", command=self.create_disks,
                   style="Primary.TButton").pack(side="left")
        self._hint(card3,
                   "系统盘建议 64GB（不能小于 32GB）；数据盘建议 64GB 以上，填 0 表示不创建数据盘。"
                   "两块盘都会创建到你上面选择的文件夹中。")

        # 已有磁盘（供切换 / 查看）
        card4 = self._card(c, "当前使用的磁盘")
        row = ttk.Frame(card4, style="Card.TFrame")
        row.pack(fill="x", padx=18, pady=(10, 4))
        ttk.Label(row, text="系统盘：", style="Card.TLabel").pack(side="left")
        self.disk_var = tk.StringVar(value=self.cfg.get("system_disk", ""))
        ttk.Entry(row, textvariable=self.disk_var, width=62).pack(side="left", padx=6)
        ttk.Button(row, text="浏览…", command=self.pick_disk).pack(side="left")
        self.extra_list = tk.Listbox(card4, height=3)
        self.extra_list.pack(fill="x", padx=18, pady=4)
        for d in self.cfg.get("extra_disks", []):
            if d:
                self.extra_list.insert("end", d)
        self._hint(card4, "上面列出的是当前配置使用的磁盘；切换到别人的系统盘可用「浏览…」选择。")
        return f

    # ---------- 第 3 步：虚拟机设置 ----------
    def _step_settings(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 3 步 · 虚拟机设置", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "一般保持默认值即可。若本机端口被占用，可修改下方端口号。", 880)

        card = self._card(c, "硬件资源")
        row = ttk.Frame(card, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=10)
        ttk.Label(row, text="CPU 核心数：", style="Card.TLabel").pack(side="left")
        self.cpu_var = tk.StringVar(value=str(self.cfg["cpu_cores"]))
        ttk.Spinbox(row, from_=1, to=16, textvariable=self.cpu_var, width=8).pack(side="left", padx=(6, 24))
        ttk.Label(row, text="内存（MB）：", style="Card.TLabel").pack(side="left")
        self.ram_var = tk.StringVar(value=str(self.cfg["ram_mb"]))
        ttk.Spinbox(row, from_=2048, to=16384, increment=1024,
                    textvariable=self.ram_var, width=10).pack(side="left", padx=6)
        self._hint(card, "建议至少 4 核 / 4096 MB。飞牛系统内存占用较高。")

        card2 = self._card(c, "网络端口")
        row = ttk.Frame(card2, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=10)
        ttk.Label(row, text="Web 管理端口：", style="Card.TLabel").pack(side="left")
        self.webport_var = tk.StringVar(value=str(self.cfg["web_port"]))
        ttk.Spinbox(row, from_=1024, to=65535, textvariable=self.webport_var,
                    width=9).pack(side="left", padx=(6, 24))
        ttk.Label(row, text="SSH 端口：", style="Card.TLabel").pack(side="left")
        self.sshport_var = tk.StringVar(value=str(self.cfg["ssh_port"]))
        ttk.Spinbox(row, from_=1024, to=65535, textvariable=self.sshport_var,
                    width=9).pack(side="left", padx=6)
        self._hint(card2, "访问地址： http(s)://localhost:<Web 管理端口>  （飞牛默认是 5666，程序会自动判断用 http 还是 https）")

        card3 = self._card(c, "高级选项")
        row = ttk.Frame(card3, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=10)
        fields = [
            ("启动方式", "boot_var", ["uefi", "bios"], self.cfg["boot_mode"]),
            ("加速模式", "accel_var", ["auto", "whpx", "tcg"], self.cfg["accel"]),
            ("显示窗口", "display_var", ["window", "none"], self.cfg["display"]),
            ("硬盘总线", "bus_var", ["virtio", "sata"], self.cfg["disk_bus"]),
            ("网卡型号", "nic_var", ["e1000", "virtio"], self.cfg["nic_model"]),
        ]
        for label, attr, vals, cur in fields:
            ttk.Label(row, text=label + "：", style="Card.TLabel").pack(side="left")
            var = tk.StringVar(value=cur)
            setattr(self, attr, var)
            ttk.Combobox(row, textvariable=var, values=vals, width=10,
                         state="readonly").pack(side="left", padx=(4, 14))
        self._hint(card3, "加速模式选 auto 即可；若启动失败会自动回退到兼容模式。"
                          "硬盘总线和网卡型号一般无需修改。")

        row = ttk.Frame(card3, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=(4, 14))
        ttk.Button(row, text="保存设置", command=self.apply_settings,
                   style="Primary.TButton").pack(side="left")
        ttk.Button(row, text="重置当前镜像的 UEFI 变量", command=self.reset_vars).pack(
            side="left", padx=10)
        return f

    # ---------- 第 4 步：启动与初始化 ----------
    def _step_run(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 4 步 · 启动并初始化飞牛系统", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))

        self._para(c, "启动/停止虚拟机、打开管理界面请点左侧「快捷操作」区的按钮。", 860)

        # ---- 虚拟机管理：快照还原 / 一键删除 ----
        manage = self._card(c, "虚拟机管理")
        row = ttk.Frame(manage, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=(12, 4))
        ttk.Button(row, text="💾 保存为初始状态", command=self.save_snapshot).pack(side="left")
        ttk.Button(row, text="↩ 一键还原初始状态", command=self.restore_snapshot).pack(side="left", padx=8)
        self.snap_label = ttk.Label(manage, text="", style="Hint.TLabel")
        self.snap_label.pack(anchor="w", padx=18, pady=(2, 6))
        self._hint(manage,
                   "「保存为初始状态」会把当前系统做成快照；之后无论怎么折腾，点「一键还原」"
                   "即可秒级恢复到这个状态，无需重装。还原前请保持虚拟机停止。", 860)
        row2 = ttk.Frame(manage, style="Card.TFrame"); row2.pack(fill="x", padx=18, pady=(4, 14))
        ttk.Button(row2, text="🗑 一键删除虚拟机", command=self.delete_vm,
                   style="Danger.TButton").pack(side="left")
        self._hint(manage,
                   "删除会先关闭虚拟机，并把系统盘/数据盘移到备份目录（可恢复）。"
                   "删除后可从第 2 步重新开始。", 860)

        guide = self._card(c, "首次安装请按以下顺序操作")
        steps = [
            "1. 点左侧「启动虚拟机」，画面会自动嵌入到本软件，并自动切到左侧「🖥 虚拟机画面」页。",
            "2. 在这个画面里按安装向导操作：选择安装目标硬盘 → 开始安装。当进度到 99% 并停在确认页时，"
            "直接在嵌入画面里点击「下一步 / 确认」继续（安装中途有需要人工确认的步骤）。",
            "3. 安装程序会提示重启，按提示确认；等待系统重新启动到登录控制台。",
            "4. 系统启动后控制台会显示 Web 管理地址。此时点「打开飞牛管理界面」"
            "（程序会自动识别 http/https），按页面提示创建设备名称和管理员账号。",
            "5. 进入飞牛桌面后，即可在「应用中心」安装 FPK 应用（下一步）。",
            "6. 以后再使用时：在「准备镜像」选择已有系统盘，然后直接启动即可，无需再次安装。",
        ]
        for s in steps:
            self._para(guide, s, 860)

        card2 = self._card(c, "运行日志（排错时查看）")
        self.log_text = tk.Text(card2, height=10, wrap="none",
                                bg="#111827", fg="#D1FAE5",
                                font=("Consolas", 9), relief="flat")
        self.log_text.pack(fill="x", padx=18, pady=(4, 14))

        card3 = self._card(c, "生成的启动命令（供高级用户核对）")
        self.cmd_text = tk.Text(card3, height=4, wrap="word", bg="#F3F4F6",
                                fg="#1F2937", font=("Consolas", 9), relief="flat")
        self.cmd_text.pack(fill="x", padx=18, pady=(4, 6))
        row = ttk.Frame(card3, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=(0, 14))
        ttk.Button(row, text="刷新命令", command=self.refresh_cmd).pack(side="left")
        ttk.Button(row, text="复制命令", command=self.copy_cmd).pack(side="left", padx=8)
        return f

    # ---------- 第 5 步：安装 FPK ----------
    def _step_fpk(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 5 步 · 安装 FPK 应用", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "飞牛的 FPK 应用是网页应用，安装后直接在电脑浏览器中打开使用。"
                      "推荐用图形界面安装，最简单。", 880)

        card = self._card(c, "方法一：图形界面安装（推荐）")
        for s in [
            "1. 确保虚拟机已启动，点左侧「打开管理界面」并登录。",
            "2. 进入「应用中心」→ 点击右上角菜单（三个点）→ 选择「安装本地应用」。",
            "3. 选择你准备好的 .fpk 文件，按提示完成安装。",
            "4. 安装后在飞牛桌面上会出现应用图标，点击即可在浏览器打开。",
        ]:
            self._para(card, s, 860)

        card2 = self._card(c, "方法二：命令行安装（适合调试）")
        self._para(card2,
                   "如需通过 SSH 安装，可用 Windows 自带的终端连接："
                   " ssh root@localhost -p <SSH 端口>，然后执行下面这条命令。", 860)
        row = ttk.Frame(card2, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=8)
        ttk.Label(row, text="FPK 文件：", style="Card.TLabel").pack(side="left")
        self.fpk_var = tk.StringVar(value=self.cfg.get("fpk_path", ""))
        ttk.Entry(row, textvariable=self.fpk_var, width=58).pack(side="left", padx=6)
        ttk.Button(row, text="浏览…", command=self.pick_fpk).pack(side="left")
        self.fpk_cmd = tk.StringVar(value="appcenter-cli install-fpk <请选择 FPK 文件>")
        e = ttk.Entry(card2, textvariable=self.fpk_cmd, width=90, state="readonly")
        e.pack(fill="x", padx=18, pady=(4, 8))
        row = ttk.Frame(card2, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=(0, 14))
        ttk.Button(row, text="复制安装命令", command=self.copy_fpk_cmd).pack(side="left")
        ttk.Button(row, text="复制 SSH 连接命令", command=self.copy_ssh_cmd).pack(side="left", padx=8)
        ttk.Button(row, text="打开应用中心", command=self.open_web).pack(side="left", padx=8)
        self._hint(card2, "开发者调试还可用：appcenter-cli install-local（开发模式安装）、"
                          "appcenter-cli list（查看已安装应用）。")
        return f

    # ---------- 第 6 步：打开应用界面 ----------
    def _step_app(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="第 6 步 · 在 PC 上打开 FPK 应用界面", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "FPK 应用本质上是运行在飞牛系统里的网页服务，因此可以直接在电脑浏览器中打开，"
                      "和真实 NAS 上的体验一致。", 880)

        card = self._card(c, "常用入口")
        row = ttk.Frame(card, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=12)
        ttk.Button(row, text="打开飞牛桌面（推荐）", command=self.open_web,
                   style="Primary.TButton").pack(side="left")
        ttk.Button(row, text="打开应用中心", command=self.open_web).pack(side="left", padx=10)
        self._para(card, "打开后先在飞牛桌面点击已安装的应用图标，应用界面会在浏览器的新标签页中打开。"
                         "若浏览器提示证书不受信任，选择继续访问即可。", 860)

        card2 = self._card(c, "已知应用地址（可选）")
        self._para(card2, "如果应用提供了固定访问路径，可填入后直接打开：", 860)
        row = ttk.Frame(card2, style="Card.TFrame"); row.pack(fill="x", padx=18, pady=8)
        ttk.Label(row, text="访问地址：", style="Card.TLabel").pack(side="left")
        self.app_path_var = tk.StringVar(value=self.cfg.get("app_path", ""))
        ttk.Entry(row, textvariable=self.app_path_var, width=56).pack(side="left", padx=6)
        ttk.Button(row, text="打开", command=self.open_app).pack(side="left")
        self._hint(card2, "示例： http://localhost:5666  或  http://localhost:<应用端口>。"
                          "若应用使用独立端口，请先在飞牛的应用设置中查看端口号。")

        card3 = self._card(c, "小提示")
        self._para(card3, "· 虚拟机停止后，所有应用界面都无法访问，请先启动虚拟机。\n"
                          "· 想更换测试的系统镜像：回到第 2 步选择另一个系统盘或 ISO，重启虚拟机即可。\n"
                          "· 浏览器地址栏中的 localhost 只在本机有效，其他电脑无法访问。", 860)
        return f

    # ---------- 虚拟机画面（嵌入显示） ----------
    def _step_screen(self):
        f = tk.Frame(self.container, bg="#0B0F14")
        # 顶部说明条
        bar = tk.Frame(f, bg="#1F2937", height=40)
        bar.pack(side="top", fill="x")
        bar.pack_propagate(False)
        self.screen_title_var = tk.StringVar(value="虚拟机画面（未启动）")
        tk.Label(bar, textvariable=self.screen_title_var, bg="#1F2937",
                 fg="#E5E7EB", font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(side="left", padx=16)
        tk.Button(bar, text="⧉ 弹出为独立窗口", command=self.popout_screen,
                  bg="#374151", fg="#E5E7EB", activebackground="#4B5563",
                  activeforeground="#FFFFFF", relief="flat", bd=0,
                  font=("Microsoft YaHei UI", 9), padx=10, pady=2,
                  cursor="hand2").pack(side="right", padx=12, pady=6)
        tk.Label(bar, text="点击画面即可操作虚拟机 · 鼠标无需捕获",
                 bg="#1F2937", fg="#9CA3AF",
                 font=("Microsoft YaHei UI", 9)).pack(side="right", padx=8)
        # 画面承载容器（SDL 窗口会被 SetParent 到这里）
        host = tk.Frame(f, bg="#000000")
        host.pack(side="top", fill="both", expand=True)
        self.screen_host = host
        # 占位提示（未嵌入时显示）
        self.screen_placeholder = tk.Label(
            host, text="虚拟机尚未启动\n\n点左侧「启动虚拟机」后，画面会显示在这里",
            bg="#000000", fg="#6B7280",
            font=("Microsoft YaHei UI", 12), justify="center")
        self.screen_placeholder.pack(expand=True)
        # 容器尺寸变化时同步调整嵌入的 SDL 画面
        host.bind("<Configure>", self._on_screen_configure)
        # 点容器（画面边缘空白处）时把键盘焦点交给虚拟机
        host.bind("<Button-1>", lambda e: self.qemu.focus_embedded())
        return f

    # ---------- 独立页面：应用端口转发 ----------
    def _step_ports(self):
        f = ScrollFrame(self.container)
        c = f.inner
        ttk.Label(c, text="应用端口转发", style="Title.TLabel").pack(
            anchor="w", padx=18, pady=(6, 4))
        self._para(c, "虚拟机上安装的应用（下载器、影音库、相册、网盘等）都监听各自的端口。"
                      "把端口转发到本机后，就能直接用浏览器打开 localhost:端口 访问。"
                      "虚拟机运行中新增的端口立即生效，不需要重启。", 880)

        card = self._card(c, "转发列表")
        bar = ttk.Frame(card, style="Card.TFrame")
        bar.pack(fill="x", padx=18, pady=(0, 10))
        ttk.Label(bar, text="本机端口", style="Card.TLabel").pack(side="left")
        self.pf_host_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.pf_host_var, width=7).pack(side="left", padx=(4, 10))
        ttk.Label(bar, text="虚拟机端口", style="Card.TLabel").pack(side="left")
        self.pf_guest_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.pf_guest_var, width=7).pack(side="left", padx=(4, 4))
        ttk.Label(bar, text="（留空=同上）", style="Hint.TLabel").pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="添加并转发", command=self.pf_add).pack(side="left")

        self.pf_tree = ttk.Treeview(card, columns=("host", "guest", "url", "state"),
                                    show="headings", height=9)
        for k, t, w in (("host", "本机端口", 90), ("guest", "虚拟机端口", 100),
                        ("url", "访问地址", 320), ("state", "状态", 130)):
            self.pf_tree.heading(k, text=t)
            self.pf_tree.column(k, width=w, anchor="w")
        self.pf_tree.pack(fill="x", padx=18, pady=(0, 8))
        self.pf_tree.tag_configure("on", foreground="#047857")
        self.pf_tree.tag_configure("warn", foreground="#B45309")
        self.pf_tree.tag_configure("probe", foreground="#6B7280")
        self.pf_tree.tag_configure("off", foreground="#9CA3AF")
        self.pf_tree.bind("<Double-1>", lambda e: self.pf_open())

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", padx=18, pady=(0, 14))
        ttk.Button(row, text="刷新", command=self.pf_refresh).pack(side="left")
        ttk.Button(row, text="在浏览器打开", command=self.pf_open).pack(side="left", padx=8)
        ttk.Button(row, text="移除选中", command=self.pf_remove).pack(side="left")
        ttk.Button(row, text="恢复常用端口", command=self.pf_restore).pack(side="left", padx=8)
        ttk.Button(row, text="扫描应用端口", command=self.pf_scan).pack(side="left")

        card2 = self._card(c, "说明")
        self._hint(card2,
                   "· 默认已包含常见应用端口（8080 / 8096 / 5244 / 5700 / 9000 等）。\n"
                   "· 状态显示「可访问」= 转发已通、应用能打开；\n"
                   "  「已转发·应用无响应」= 转发没问题，是虚拟机里那个应用没启动、或端口填错了，\n"
                   "  请在飞牛「应用中心 → 该应用 → 详情」里核对真实端口，必要时先打开一次应用。\n"
                   "· 端口与本机其它程序冲突时会自动跳过，并在日志中提示。",
                   860)
        self.after(300, self.pf_refresh)
        return f

    def pf_refresh(self):
        """刷新转发列表：先显示转发状态，再后台检测端口背后是否真有服务。"""
        if not hasattr(self, "pf_tree"):
            return
        live = {}
        running = False
        try:
            running = bool(self.qemu.running or find_running_qemu())
            if running:
                live = {x["host"]: x["guest"] for x in self.qemu.port_forward_list()}
        except Exception:
            live = {}
        want = parse_forward_ports(self.cfg.get("forward_ports"))
        cfg_map = {h: g for h, g in want}
        try:
            self.pf_tree.delete(*self.pf_tree.get_children())
        except Exception:
            return
        ports = sorted(set(cfg_map) | set(live.keys()))
        for p in ports:
            g = live.get(p, cfg_map.get(p, p))
            on = p in live
            if on:
                state = "已转发（检测中…）"
            else:
                state = "待转发（启动后自动）"
            self.pf_tree.insert(
                "", "end",
                values=(p, g, "http://localhost:%d" % p, state),
                tags=("off" if not on else "probe",))
        # 后台探测真实服务，避免界面卡顿
        if running and ports:
            threading.Thread(target=self._pf_probe, args=(ports,),
                             daemon=True).start()

    def _pf_probe(self, ports):
        res = {}
        for p in ports:
            try:
                res[p] = port_service_alive(p, 0.9)
            except Exception:
                res[p] = False
        try:
            self.after(0, lambda: self._pf_apply_probe(res))
        except Exception:
            pass

    def _pf_apply_probe(self, res):
        if not hasattr(self, "pf_tree"):
            return
        try:
            items = self.pf_tree.get_children()
        except Exception:
            return
        for iid in items:
            vals = self.pf_tree.item(iid, "values")
            if not vals:
                continue
            try:
                hp = int(vals[0])
            except Exception:
                continue
            if hp not in res:
                continue
            if res[hp]:
                self.pf_tree.set(iid, "state", "可访问")
                self.pf_tree.item(iid, tags=("on",))
            else:
                self.pf_tree.set(iid, "state", "已转发·应用无响应")
                self.pf_tree.item(iid, tags=("warn",))

    def pf_add(self):
        hs = self.pf_host_var.get().strip()
        gs = self.pf_guest_var.get().strip() or hs
        if not hs.isdigit() or not gs.isdigit():
            messagebox.showwarning(APP_NAME, "请填写数字端口，例如 8096。")
            return
        hp, gp = int(hs), int(gs)
        if not (1 <= hp <= 65535 and 1 <= gp <= 65535):
            messagebox.showwarning(APP_NAME, "端口范围应为 1~65535。")
            return
        pairs = parse_forward_ports(self.cfg.get("forward_ports"))
        if not any(h == hp for h, g in pairs):
            pairs.append((hp, gp))
            pairs.sort()
            # 端口相同存数字，不同则存对象，保证重启后仍按原映射恢复
            self.cfg["forward_ports"] = [
                ({"host": h, "guest": g} if h != g else h) for h, g in pairs]
            save_config(self.cfg)
        if self.qemu.running or find_running_qemu():
            ok, err = self.qemu.port_forward_add(hp, gp)
            if not ok:
                messagebox.showerror(APP_NAME, "转发失败：\n%s" % err)
                return
            self.logger.log("已添加端口转发：本机 %d → 虚拟机 %d" % (hp, gp))
            self.foot_var.set("✓ 已转发 localhost:%d → 虚拟机 %d" % (hp, gp))
        else:
            self.foot_var.set("已记录端口 %d，虚拟机启动后会自动转发" % hp)
        self.pf_host_var.set("")
        self.pf_guest_var.set("")
        self.pf_refresh()

    def pf_remove(self):
        sel = self.pf_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "请先在列表中选中一行。")
            return
        try:
            hp = int(self.pf_tree.item(sel[0], "values")[0])
        except Exception:
            return
        if hp in (int(self.cfg["web_port"]), int(self.cfg["ssh_port"])):
            messagebox.showinfo(APP_NAME, "管理界面端口和 SSH 端口是必需的，不能移除。")
            return
        pairs = [(h, g) for h, g in parse_forward_ports(self.cfg.get("forward_ports"))
                 if h != hp]
        self.cfg["forward_ports"] = [
            ({"host": h, "guest": g} if h != g else h) for h, g in pairs]
        save_config(self.cfg)
        if self.qemu.running or find_running_qemu():
            self.qemu.port_forward_remove(hp)
        self.logger.log("已移除端口转发：本机 %d" % hp)
        self.foot_var.set("已移除端口 %d" % hp)
        self.pf_refresh()

    def pf_open(self):
        sel = self.pf_tree.selection()
        if sel:
            try:
                hp = int(self.pf_tree.item(sel[0], "values")[0])
            except Exception:
                hp = int(self.cfg["web_port"])
        elif hasattr(self, "pf_host_var") and self.pf_host_var.get().strip().isdigit():
            hp = int(self.pf_host_var.get().strip())
        else:
            hp = int(self.cfg["web_port"])
        url = "http://localhost:%d" % hp
        self._open_url(url)
        self.foot_var.set("已在浏览器打开：" + url)

    def pf_scan(self):
        """扫描虚拟机里真正在监听的应用端口，避免瞎猜端口。"""
        if not (self.qemu.running or find_running_qemu()):
            messagebox.showinfo(APP_NAME, "请先启动虚拟机，再扫描应用端口。")
            return
        if getattr(self, "_scanning", False):
            messagebox.showinfo(APP_NAME, "正在扫描中，请稍候…")
            return
        self._scanning = True
        self.foot_var.set("正在扫描虚拟机端口，请稍候（约 20 秒）…")

        def worker():
            hits = []
            try:
                hits = self.qemu.scan_guest_ports()
            except Exception as e:
                self.logger.log("端口扫描失败：%s" % e, "警告")
            try:
                self.after(0, lambda: self._on_scan_done(hits))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_done(self, hits):
        self._scanning = False
        try:
            skip = {int(self.cfg.get("web_port", 5666)),
                    int(self.cfg.get("ssh_port", 2222))}
        except Exception:
            skip = set()
        new = [p for p in hits if p not in skip]
        if not new:
            self.foot_var.set("扫描完成：未发现应用端口")
            messagebox.showinfo(
                APP_NAME,
                "扫描完成，没有发现新的应用端口。\n\n"
                "如果应用刚安装或刚启动，请等它运行几秒后再扫一次。")
            return
        # 已经在转发列表里的端口不必重复询问
        pairs = parse_forward_ports(self.cfg.get("forward_ports"))
        have = {h for h, g in pairs}
        new_only = [p for p in new if p not in have]
        already = [p for p in new if p in have]
        self.foot_var.set("扫描完成，发现 %d 个端口：%s"
                          % (len(new), ", ".join(str(p) for p in new)))
        if not new_only:
            messagebox.showinfo(
                APP_NAME,
                "扫描完成，发现这些端口都有应用在运行：\n\n%s\n\n"
                "它们都已经在转发列表里，可以直接用 http://localhost:端口 访问。"
                % ", ".join(str(p) for p in already))
            return
        tip = ""
        if already:
            tip = "\n（其中 %s 已在转发列表中）" % ", ".join(str(p) for p in already)
        if not messagebox.askyesno(
                APP_NAME,
                "发现以下新端口上有应用在运行：\n\n%s%s\n\n是否全部添加转发？\n"
                "（添加后即可用 http://localhost:端口 访问）"
                % (", ".join(str(p) for p in new_only), tip)):
            return
        added = 0
        for p in new_only:
            pairs.append((p, p))
            ok, err = self.qemu.port_forward_add(p, p)
            if ok:
                added += 1
            else:
                self.logger.log("端口 %d 转发失败：%s" % (p, err), "警告")
        pairs.sort()
        self.cfg["forward_ports"] = [
            ({"host": h, "guest": g} if h != g else h) for h, g in pairs]
        save_config(self.cfg)
        self.logger.log("扫描并转发了 %d 个应用端口：%s"
                        % (added, ", ".join(str(p) for p in new_only)))
        self.foot_var.set("✓ 已转发 %d 个端口，可点「在浏览器打开」访问" % added)
        self.pf_refresh()

    def pf_restore(self):
        self.cfg["forward_ports"] = list(DEFAULT_FORWARD_PORTS)
        save_config(self.cfg)
        if self.qemu.running or find_running_qemu():
            live = {x["host"] for x in self.qemu.port_forward_list()}
            added = 0
            for p in DEFAULT_FORWARD_PORTS:
                if p in live or port_in_use(p):
                    continue
                ok, _ = self.qemu.port_forward_add(p, p)
                if ok:
                    added += 1
            self.foot_var.set("已恢复常用端口，本次新转发 %d 个" % added)
        else:
            self.foot_var.set("已恢复常用端口列表，虚拟机启动后自动转发")
        self.pf_refresh()

    def _on_screen_configure(self, event):
        if self.qemu.embedded_hwnd:
            self.qemu.resize_embedded(event.width, event.height)

    def get_screen_hwnd(self):
        """返回画面容器的窗口句柄，供后台线程嵌入 SDL 窗口。"""
        try:
            return int(self.screen_host.winfo_id())
        except Exception:
            return 0

    def popout_screen(self):
        """把嵌入的画面弹出为独立窗口（鼠标/键盘在嵌入模式下异常时的兜底）。"""
        if not self.qemu.embedded_hwnd:
            messagebox.showinfo(APP_NAME,
                                "当前没有正在嵌入的虚拟机画面。\n\n"
                                "如果虚拟机正在运行但画面没显示，请先点左侧「虚拟机画面」。")
            return
        if self.qemu.detach_to_window():
            self.screen_title_var.set("虚拟机画面（已弹出为独立窗口）")
            self.screen_placeholder.pack()
            self.screen_placeholder.config(
                text="画面已弹出为独立窗口\n\n"
                     "请在弹出的窗口中操作虚拟机。\n"
                     "安装完成后关闭软件再重新打开，画面会自动重新嵌入。")
            self.foot_var.set("✓ 画面已弹出为独立窗口，可直接在其中操作")

    def on_vm_embedded(self, hwnd):
        """嵌入成功后由后台线程调度到主线程执行：更新标题、切到画面页、调整大小。"""
        def _do():
            self.screen_placeholder.pack_forget()
            self.screen_title_var.set("● 虚拟机画面（运行中）")
            self.show_step(6)
            self.after(120, lambda: self.qemu.resize_embedded(
                self.screen_host.winfo_width(), self.screen_host.winfo_height()))
        try:
            self.after(0, _do)
        except Exception:
            pass

    # ---------- 事件与刷新 ----------
    def _sink(self, kind, *args):
        self.events.put((kind, args))

    def post_event(self, name):
        self.events.put(("event", (name,)))

    def _pump(self):
        """主线程事件泵。每个事件单独捕获异常，避免某个 UI 回调异常
        冒泡到 Tcl 层引发进程级崩溃（0xc0000409）。"""
        try:
            while True:
                try:
                    kind, args = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "log":
                        self._append_log(args[1])
                    elif kind == "event":
                        self._on_event(args[0])
                    elif kind == "env_rows":
                        self._env_rows_handler(args[0])
                except Exception as e:
                    # UI 回调异常只记录，不让其终止事件循环
                    try:
                        with LOG_PATH.open("a", encoding="utf-8") as f:
                            f.write("[%s] [界面异常已忽略] %s\n" %
                                    (time.strftime("%H:%M:%S"), e))
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            self.after(120, self._pump)
        except Exception:
            pass

    def _append_log(self, line):
        if not hasattr(self, "log_text"):
            return
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        try:
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 600:
                self.log_text.delete("1.0", "200.0")
        except Exception:
            pass

    def _hide_vm_banner(self):
        try:
            if self.banner.winfo_ismapped():
                self.banner.pack_forget()
        except Exception:
            pass

    def _update_addr_display(self, running):
        """更新右上角状态与管理地址展示。"""
        scheme = "http"
        if running:
            port = int(self.cfg.get("web_port", 5666))
            self.addr_var.set("管理地址：%s://localhost:%d" % (scheme, port))
            self.addr_lbl.configure(foreground="#2563EB")
        else:
            self.addr_var.set("管理地址：—（虚拟机未启动）")
            self.addr_lbl.configure(foreground="#9CA3AF")

    def _on_event(self, name):
        if name == "vm_started":
            self._hide_vm_banner()
            self.status_var.set("● 虚拟机运行中")
            self.status_lbl.configure(foreground="#059669")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self._update_addr_display(True)
            self.foot_var.set("虚拟机运行中 · 管理地址 http://localhost:%d" % self.cfg["web_port"])
        elif name == "vm_stopped":
            self._hide_vm_banner()
            self.status_var.set("● 虚拟机未运行")
            self.status_lbl.configure(foreground="#9CA3AF")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self._update_addr_display(False)
            self.foot_var.set("虚拟机已停止")
            # 画面容器恢复占位提示
            try:
                self.qemu.release_embedded(show=False)
            except Exception:
                self.qemu.detach_embedded()
            if hasattr(self, "screen_placeholder"):
                self.screen_title_var.set("虚拟机画面（未启动）")
                try:
                    self.screen_placeholder.pack(expand=True)
                except Exception:
                    pass
        elif name == "accel_fallback":
            messagebox.showinfo(APP_NAME,
                                "检测到硬件加速不可用，已自动切换到兼容模式（TCG）启动。\n"
                                "兼容模式下安装系统较慢，建议在 BIOS 开启 VT-x / AMD-V 或在 Windows 功能中启用 Hyper-V。")
        elif name == "ports_changed":
            # 后台自动转发完成后刷新端口列表（用户没在端口页时也不报错）
            try:
                self.pf_refresh()
            except Exception:
                pass

    def _refresh(self):
        self.show_step(0)
        self.refresh_cmd()
        self.refresh_snapshot_status()
        self._update_addr_display(self.qemu.running or bool(find_running_qemu()))
        threading.Thread(target=self.check_env, daemon=True).start()
        # 延迟检测外部已运行的虚拟机（等布局完成）
        self.after(600, self.detect_external_vm)
        # 每 4 秒对齐一次界面状态与实际进程状态
        self.after(2500, self._watch_vm_state)

    def show_step(self, idx):
        self.current_step = idx
        if idx < len(self.frames):
            self.frames[idx].tkraise()
        for i, b in enumerate(self.steps):
            if i == idx:
                b.configure(style="Primary.TButton")
            else:
                b.configure(style="Nav.TButton")
        # 画面页导航按钮高亮
        if hasattr(self, "screen_nav_btn"):
            self.screen_nav_btn.configure(
                style="Primary.TButton" if idx == 6 else "Nav.TButton")
        if hasattr(self, "ports_nav_btn"):
            self.ports_nav_btn.configure(
                style="Primary.TButton" if idx == 7 else "Nav.TButton")
        # 切到端口页时刷新一次转发状态
        if idx == 7:
            self.after(50, self.pf_refresh)
        # 切到画面页后同步一次嵌入窗口尺寸
        if idx == 6 and self.qemu.embedded_hwnd:
            self.after(100, lambda: self.qemu.resize_embedded(
                self.screen_host.winfo_width(), self.screen_host.winfo_height()))

    # ---------- 外部虚拟机检测 ----------
    def _watch_vm_state(self):
        """定时对齐界面状态与实际进程状态。
        解决"虚拟机在跑但控制台说未运行"或反过来的不一致。"""
        try:
            alive = self.qemu.running
            if not alive:
                ext = find_running_qemu()
                if ext:
                    # 发现外部启动的虚拟机：接管并同步界面
                    self.qemu.attach_external(ext[0][0])
                    alive = True
                    self.status_var.set("● 虚拟机运行中（已接管）")
                    self.status_lbl.configure(foreground="#059669")
                    self.start_btn.configure(state="disabled")
                    self.stop_btn.configure(state="normal")
                    self._update_addr_display(True)
                    if self.cfg.get("display") != "none" and not self.qemu.embedded_hwnd:
                        threading.Thread(target=self.qemu._embed_sdl_window,
                                         args=(ext[0][0],), daemon=True).start()
                elif self.qemu.proc is None and self.qemu.external_pid is None \
                        and "运行中" in self.status_var.get():
                    # 进程没了但界面还显示运行中（例如外部关掉了虚拟机窗口）
                    self.status_var.set("● 虚拟机未运行")
                    self.status_lbl.configure(foreground="#9CA3AF")
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self._update_addr_display(False)
            else:
                # 确认按钮状态与运行状态一致
                if str(self.stop_btn.cget("state")) != "normal":
                    self.stop_btn.configure(state="normal")
                if str(self.start_btn.cget("state")) != "disabled":
                    self.start_btn.configure(state="disabled")
        except Exception:
            pass
        try:
            self.after(4000, self._watch_vm_state)
        except Exception:
            pass

    def detect_external_vm(self):
        """检测是否已有虚拟机在运行（可能是程序重启前启动的）。
        若当前程序未接管但有外部 QEMU 进程，显示醒目横幅引导用户直接打开管理界面。"""
        external = find_running_qemu()
        if not external:
            return
        if self.qemu.running:
            # 当前程序已接管，不需要横幅（但更新状态）
            return
        # 有外部虚拟机但当前程序未接管
        pids = ", ".join(str(p) for p, _ in external)
        # 真正接管它：接管后「停止虚拟机」、端口转发、画面嵌入才会生效
        self.qemu.attach_external(external[0][0])
        self.logger.log("检测到已运行的虚拟机（进程 %s），已接管" % pids)
        text = ("✓ 检测到飞牛虚拟机正在运行（前面功能都正常），可直接点左侧「打开管理界面」进入后台")
        self.banner_lbl.configure(text=text)
        if not self.banner.winfo_ismapped():
            # pack 到 body 之前（top 之后），after 让布局稳定
            self.banner.pack(side="top", fill="x", before=self._body_frame)
        # 同步状态显示与按钮
        self.status_var.set("● 虚拟机运行中（已接管）")
        self.status_lbl.configure(foreground="#059669")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._update_addr_display(True)
        # 标记前面的步骤都完成（不覆盖底部提示）
        for i in range(6):
            self.step_done[i] = True
            try:
                self.step_badges[i].configure(text="✓")
            except Exception:
                pass
        self.progress_var.set("完成进度：6 / 6")
        self.foot_var.set("虚拟机已在运行，可直接点「打开管理界面」或顶部绿色按钮")
        # 尝试把外部虚拟机的画面也嵌入到本窗口
        if self.cfg.get("display") != "none":
            threading.Thread(target=self.qemu._embed_sdl_window,
                             args=(external[0][0],), daemon=True).start()

    # ---------- 成功提示 ----------
    def mark_step_done(self, idx, message=None):
        """把某个向导步骤标记为已完成（导航显示绿色对勾），并更新进度。"""
        if 0 <= idx < len(self.step_done):
            self.step_done[idx] = True
            try:
                self.step_badges[idx].configure(text="✓")
            except Exception:
                pass
        done = sum(1 for x in self.step_done if x)
        self.progress_var.set("完成进度：%d / %d" % (done, len(self.step_done)))
        if message:
            self.logger.log("[完成] " + message)
        self.foot_var.set("✓ " + message if message else "✓ 已完成")

    def step_success(self, step_frame, message):
        """在步骤内容顶部显示一条绿色成功提示条。"""
        if step_frame is None:
            return
        inner = getattr(step_frame, "inner", step_frame)
        # 避免重复添加
        for w in inner.winfo_children():
            if getattr(w, "_fn_success", False):
                w.destroy()
        # 找到第一个子控件，把提示条插到它前面，确保显示在内容顶部
        children = inner.winfo_children()
        first = children[0] if children else None
        bar = tk.Frame(inner, bg="#D1FAE5", height=34)
        bar._fn_success = True
        kw = dict(fill="x", padx=18, pady=(6, 2))
        if first is not None:
            kw["before"] = first
        bar.pack(**kw)
        bar.pack_propagate(False)
        tk.Label(bar, text="✓  " + message, bg="#D1FAE5", fg="#065F46",
                 font=("Microsoft YaHei UI", 10, "bold"), anchor="w").pack(
                     side="left", padx=12)

    # ---------- 具体动作 ----------
    def check_env(self):
        rows = []
        q = qemu_exe()
        rows.append(("QEMU 主程序", "通过" if q else "缺失",
                     str(q) if q else "runtime/qemu 中未找到 qemu-system-x86_64.exe"))
        qi = qemu_img_exe()
        rows.append(("虚拟硬盘工具", "通过" if qi else "缺失",
                     str(qi) if qi else "未找到 qemu-img.exe"))
        code, var = ovmf_code(), ovmf_vars()
        if code and var:
            rows.append(("UEFI 固件（可选）", "通过", "BIOS / UEFI 两种引导均可用"))
        else:
            rows.append(("引导固件", "通过",
                         "使用内置传统引导（SeaBIOS），可正常安装飞牛；UEFI 选项需完整固件"))
        hv = whpx_available()
        rows.append(("Windows 硬件加速", "可用" if hv else "未启用",
                     "已检测到 Hyper-V，可使用 WHPX 加速" if hv else
                     "未检测到 Hyper-V；将使用兼容模式，可正常运行但速度较慢"))
        all_ok = all(r[1] in ("通过", "可用") for r in rows)
        self.events.put(("env_rows", (rows, all_ok)))

    def _env_rows_handler(self, data):
        if not hasattr(self, "env_tree"):
            return
        rows, all_ok = data if isinstance(data, tuple) else (data, False)
        for i in self.env_tree.get_children():
            self.env_tree.delete(i)
        for r in rows:
            self.env_tree.insert("", "end", values=r)
        if all_ok:
            self.mark_step_done(0, "环境自检通过，运行组件完整")
            self.step_success(self.frames[0], "环境自检通过：虚拟机组件完整，可以开始配置")

    def _image_ready(self, message):
        """镜像/磁盘就绪：只有 ISO 和系统盘都齐备才算步骤2完成。"""
        iso_ok = bool(self.iso_var.get().strip()) or self.mode_var.get() == "existing"
        disk_ok = bool(self.disk_var.get().strip()) and Path(self.disk_var.get().strip()).exists()
        if iso_ok and disk_ok:
            self.mark_step_done(1, message)
            self.step_success(self.frames[1], message)

    def pick_iso(self):
        p = filedialog.askopenfilename(title="选择飞牛 ISO 安装镜像",
                                       filetypes=[("ISO 镜像", "*.iso"), ("所有文件", "*.*")])
        if p:
            self.iso_var.set(p)
            self.foot_var.set("✓ 已选择安装镜像：" + Path(p).name)
            self._image_ready("镜像已就绪，可以创建虚拟硬盘并安装")

    def pick_disk(self):
        p = filedialog.askopenfilename(title="选择已有系统盘",
                                       filetypes=[("QCOW2 虚拟硬盘", "*.qcow2"), ("所有文件", "*.*")])
        if p:
            self.disk_var.set(p)
            self.mode_var.set("existing")
            self._mode_changed()
            self.foot_var.set("✓ 已选择系统盘：" + Path(p).name)
            self._image_ready("系统盘已就绪，可直接启动（无需重装）")

    def pick_fpk(self):
        p = filedialog.askopenfilename(title="选择 FPK 应用包",
                                       filetypes=[("FPK 应用", "*.fpk"), ("所有文件", "*.*")])
        if p:
            self.fpk_var.set(p)
            self.fpk_cmd.set('appcenter-cli install-fpk "%s"' % p)
            self.mark_step_done(4, "已选择 FPK 应用包")
            self.step_success(self.frames[4], "已选择应用包：%s，安装命令已生成" % Path(p).name)

    def _mode_changed(self):
        pass

    def pick_disk_dir(self):
        """选择虚拟硬盘的存放文件夹。"""
        d = filedialog.askdirectory(title="选择虚拟硬盘存放位置",
                                    initialdir=self.diskdir_var.get() or str(data_dir()))
        if d:
            self.diskdir_var.set(d)
            self.cfg["disk_store_dir"] = d
            self.foot_var.set("✓ 存储位置：" + d)

    def _create_one_disk(self, qi, path, size_gb, overwrite=False):
        """创建单个 qcow2 磁盘，返回 (是否成功, 提示)。
        overwrite=False 时若文件已存在则直接复用，绝不删除已有磁盘数据。"""
        p = Path(path)
        if p.exists():
            if not overwrite:
                return True, str(p)
            try:
                p.unlink()
            except Exception as e:
                return False, "无法删除原文件：%s" % e
        p.parent.mkdir(parents=True, exist_ok=True)
        cmd = [str(qi), "create", "-f", "qcow2", str(p), "%dG" % size_gb]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               startupinfo=hidden_startupinfo(), timeout=300)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout or "创建失败").strip()
        except Exception as e:
            return False, str(e)
        return True, str(p)

    def create_disks(self):
        """在用户自定义位置，一次同时创建系统盘和数据盘。"""
        qi = qemu_img_exe()
        if not qi:
            messagebox.showerror(APP_NAME, "未找到 qemu-img.exe，无法创建虚拟硬盘。")
            return
        store = self.diskdir_var.get().strip() or str(data_dir())
        try:
            Path(store).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            messagebox.showerror(APP_NAME, "无法访问存储位置：\n%s\n\n%s" % (store, e))
            return
        try:
            sys_size = max(32, int(self.size_var.get()))
        except Exception:
            sys_size = 64
        try:
            data_size = max(0, int(self.data_size_var.get()))
        except Exception:
            data_size = 64
        sys_path = str(Path(store) / "system.qcow2")
        data_path = str(Path(store) / "data.qcow2")

        # ① 目标位置已有系统盘：真实盘一律复用，绝不覆盖
        if Path(sys_path).exists() and not disk_is_blank(sys_path):
            gb = disk_volume_bytes(sys_path) / (1024 ** 3)
            messagebox.showinfo(
                APP_NAME,
                "该位置已存在可用的系统盘（%.1f GB）：\n%s\n\n"
                "将直接使用它，不会重新创建，数据安全。" % (gb, sys_path))
            self._finish_disks(sys_path, store, sys_size, data_size)
            return

        # ② 目标位置没有系统盘：先在其它已知位置找回真正装过系统的盘
        if not Path(sys_path).exists():
            cands = find_existing_system_disks()
            # 排除掉本身就是目标路径的
            cands = [p for p in cands if str(p).lower() != sys_path.lower()]
            if cands:
                best = cands[0]
                gb = best.stat().st_size / (1024 ** 3)
                if messagebox.askyesno(
                        APP_NAME,
                        "检测到已有安装过系统的硬盘：\n%s\n（%.1f GB，共找到 %d 块）\n\n"
                        "是否直接使用它？（推荐，不会新建空盘、不会丢失数据）\n"
                        "选「否」将在此位置新建一块空白系统盘。"
                        % (best, gb, len(cands))):
                    self._finish_disks(str(best), str(best.parent), sys_size, data_size)
                    self.logger.log("复用已有系统盘：%s" % best)
                    return

        # ③ 确认真的要重建空盘
        if Path(sys_path).exists():
            if not messagebox.askyesno(
                    APP_NAME,
                    "该位置已有一块空盘（尚未安装系统）：\n%s\n\n"
                    "是否删除并重新创建？" % sys_path):
                return

        # 同时创建（前面的确认流程已通过，创建时允许覆盖空盘）
        self.foot_var.set("正在创建虚拟硬盘，请稍候…")
        self.update_idletasks()
        ok, info = self._create_one_disk(qi, sys_path, sys_size, overwrite=True)
        if not ok:
            messagebox.showerror(APP_NAME, "系统盘创建失败：\n" + info)
            self.foot_var.set("系统盘创建失败")
            return
        created = [info]
        if data_size > 0:
            ok2, info2 = self._create_one_disk(qi, data_path, data_size, overwrite=True)
            if not ok2:
                messagebox.showerror(APP_NAME, "数据盘创建失败：\n" + info2)
                self.foot_var.set("数据盘创建失败")
                return
            created.append(info2)

        self._finish_disks(sys_path, store, sys_size, data_size, created=created)

    def _finish_disks(self, sys_path, store, sys_size, data_size, created=None):
        """创建或复用磁盘后统一收尾：同步界面、写入配置、给出提示。
        created=None 表示复用已有磁盘（不新建任何文件）。"""
        self.disk_var.set(sys_path)
        try:
            self.diskdir_var.set(store)
        except Exception:
            pass
        self.cfg["disk_store_dir"] = store
        self.cfg["new_disk_gb"] = sys_size
        self.cfg["new_data_gb"] = data_size

        if created is None:
            # 复用系统盘：把同目录下已有的数据盘补进列表
            existing = [x for x in self.extra_list.get(0, "end") if x]
            dp = str(Path(store) / "data.qcow2")
            if Path(dp).exists() and dp.lower() not in [x.lower() for x in existing]:
                existing.append(dp)
            self.extra_list.delete(0, "end")
            for d in existing:
                self.extra_list.insert("end", d)
            self.logger.log("复用已有系统盘：%s" % sys_path)
            self.apply_settings(silent=True)
            messagebox.showinfo(
                APP_NAME,
                "已使用现有系统盘：\n%s\n\n接下来点「保存设置」，再启动虚拟机。"
                % sys_path)
            self._image_ready("已复用现有系统盘，准备完成")
            return

        self.extra_list.delete(0, "end")
        for d in created[1:]:
            self.extra_list.insert("end", d)
        self.logger.log("已创建虚拟硬盘：系统盘 %dGB + 数据盘 %dGB，位置 %s"
                        % (sys_size, data_size, store))
        self.apply_settings(silent=True)
        messagebox.showinfo(APP_NAME,
                            "虚拟硬盘已创建：\n" + "\n".join(created) +
                            "\n\n接下来点「保存设置」，再启动虚拟机。")
        self._image_ready("系统盘 + 数据盘已创建，准备完成")

    def apply_settings(self, silent=False):
        try:
            self.cfg.update({
                "iso_path": self.iso_var.get().strip(),
                "system_disk": self.disk_var.get().strip() or self.cfg["system_disk"],
                "install_mode": self.mode_var.get(),
                "cpu_cores": int(self.cpu_var.get()),
                "ram_mb": int(self.ram_var.get()),
                "web_port": int(self.webport_var.get()),
                "ssh_port": int(self.sshport_var.get()),
                "qmp_port": max(1024, int(self.webport_var.get()) + 2000),
                "boot_mode": self.boot_var.get(),
                "accel": self.accel_var.get(),
                "display": self.display_var.get(),
                "disk_bus": self.bus_var.get(),
                "nic_model": self.nic_var.get(),
                "new_disk_gb": int(self.size_var.get()),
                "new_data_gb": int(self.data_size_var.get()) if hasattr(self, "data_size_var") else 64,
                "disk_store_dir": self.diskdir_var.get().strip() if hasattr(self, "diskdir_var") else "",
                "fpk_path": self.fpk_var.get().strip(),
                "app_path": self.app_path_var.get().strip(),
                "extra_disks": list(self.extra_list.get(0, "end")),
            })
        except Exception as e:
            messagebox.showerror(APP_NAME, "设置值有误：%s" % e)
            return False
        ok, err = save_config(self.cfg)
        if not ok:
            messagebox.showerror(APP_NAME, "保存设置失败：%s" % err)
            return False
        self.qemu.cfg = self.cfg
        self.refresh_cmd()
        if not silent:
            messagebox.showinfo(APP_NAME, "设置已保存。")
            self.mark_step_done(2, "虚拟机参数已保存")
            self.step_success(self.frames[2], "虚拟机设置已保存，可以启动虚拟机了")
        return True

    def reset_vars(self):
        p = self.qemu.vars_path()
        if p.exists():
            try:
                p.unlink()
                messagebox.showinfo(APP_NAME, "已重置 UEFI 变量，下次启动会重新生成。")
            except Exception as e:
                messagebox.showerror(APP_NAME, "重置失败：%s" % e)
        else:
            messagebox.showinfo(APP_NAME, "当前镜像尚无 UEFI 变量文件。")

    # ---------- 虚拟机管理：快照 / 删除 ----------
    def refresh_snapshot_status(self):
        """刷新快照状态标签。"""
        if not hasattr(self, "snap_label"):
            return
        ok, out = self.tools.snapshot_status()
        ts = self.cfg.get("snapshot_time", "")
        if ok:
            self.snap_label.configure(
                text="✓ 已保存初始状态快照" + ("（%s）" % ts if ts else "") + "，可随时一键还原",
                foreground="#047857")
        else:
            self.snap_label.configure(
                text="尚未保存初始状态。建议装好系统并配置好后点「保存为初始状态」。",
                foreground="#6B7280")

    def save_snapshot(self):
        if self.qemu.running:
            messagebox.showwarning(APP_NAME, "请先停止虚拟机，再保存初始状态快照。")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "把当前系统保存为「初始状态」？\n\n"
                "以后点「一键还原初始状态」即可恢复到现在的样子。\n"
                "建议在系统安装完成、常用软件装好后执行。"):
            return
        ok, msg = self.tools.snapshot_save()
        if not ok:
            messagebox.showerror(APP_NAME, "保存失败：\n" + msg)
            return
        self.refresh_snapshot_status()
        self.step_success(self.frames[3], "初始状态已保存，可随时一键还原")
        messagebox.showinfo(APP_NAME, "✓ " + msg)

    def restore_snapshot(self):
        ok, out = self.tools.snapshot_status()
        if not ok:
            messagebox.showwarning(APP_NAME,
                                   "还没有初始状态快照。\n请先点「保存为初始状态」，再使用还原功能。")
            return
        if self.qemu.running:
            messagebox.showwarning(APP_NAME, "请先停止虚拟机，再执行还原。")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "⚠️ 确定要把虚拟机还原到初始状态吗？\n\n"
                "初始状态之后安装的所有应用、修改的设置都会丢失，且无法撤销。\n"
                "请确保重要数据已另行备份。\n\n"
                "确定继续？"):
            return
        ok, msg = self.tools.snapshot_restore()
        if not ok:
            messagebox.showerror(APP_NAME, "还原失败：\n" + msg)
            return
        self.step_success(self.frames[3], "已还原到初始状态，可重新启动虚拟机")
        messagebox.showinfo(APP_NAME, "✓ " + msg + "\n\n现在可以点「启动虚拟机」使用干净的系统。")

    def delete_vm(self):
        if self.qemu.running:
            messagebox.showwarning(APP_NAME, "请先停止虚拟机，再执行删除。")
            return
        plan = self.tools.deletion_plan()
        if not plan:
            messagebox.showinfo(APP_NAME, "当前没有可删除的虚拟机。")
            return
        # 生成文件清单
        lines = []
        total = 0
        for label, path, size in plan:
            lines.append("  · %s：%s (%.2f GB)" % (label, path, size / 1024 ** 3))
            total += size
        detail = "\n".join(lines[:12])
        if len(lines) > 12:
            detail += "\n  ... 另有 %d 个文件" % (len(lines) - 12)

        if not messagebox.askyesno(
                APP_NAME,
                "⚠️ 此操作非常危险，可能导致不可逆的数据丢失！\n\n"
                "将删除以下虚拟机文件（共 %.2f GB）：\n%s\n\n"
                "请选择「确定」进入下一步确认。" % (total / 1024 ** 3, detail)):
            return
        keep = messagebox.askyesno(
            APP_NAME,
            "请选择删除方式：\n\n"
            "「是」= 安全删除：先把文件移动到备份目录（推荐，可恢复）\n"
            "「否」= 永久删除：直接从磁盘移除，无法恢复")
        ok, msg = self.tools.delete_vm(keep_backup=keep)
        if not ok:
            messagebox.showerror(APP_NAME, "删除失败：\n" + msg)
            return
        # 刷新界面状态
        for i in range(len(self.step_done)):
            self.step_done[i] = False
            try:
                self.step_badges[i].configure(text="")
            except Exception:
                pass
        self.progress_var.set("完成进度：0 / %d" % len(self.step_done))
        self.disk_var.set(self.cfg.get("system_disk", ""))
        self.iso_var.set("")
        if hasattr(self, "extra_list"):
            self.extra_list.delete(0, "end")
        self.refresh_cmd()
        self.refresh_snapshot_status()
        messagebox.showinfo(APP_NAME, "✓ 虚拟机已删除\n\n" + msg + "\n\n可回到第 2 步重新创建。")

    def autostart_vm(self):
        """无人值守启动（--autostart）：自动挑选可用的系统盘并直接启动，
        不弹任何确认框。用于一键启动脚本或开机自启。"""
        sd = str(self.cfg.get("system_disk") or "").strip()
        if not sd or not Path(sd).exists() or disk_is_blank(sd):
            cands = [p for p in find_existing_system_disks()
                     if str(p).lower() != sd.lower()]
            if not cands:
                self.foot_var.set("自动启动失败：没有找到可用的系统盘")
                self.logger.log("自动启动失败：没有可用的系统盘")
                return
            best = cands[0]
            self.disk_var.set(str(best))
            self.cfg["system_disk"] = str(best)
            try:
                self.diskdir_var.set(str(best.parent))
            except Exception:
                pass
            self.apply_settings(silent=True)
            self.logger.log("自动启动：切换到已装系统的盘 %s" % best)
        if self.qemu.running or find_running_qemu():
            self.foot_var.set("虚拟机已在运行，可直接打开管理界面")
            return
        ok, msg = self.qemu.start()
        if not ok:
            self.foot_var.set("自动启动失败：" + msg)
            self.logger.log("自动启动失败：" + msg)
        else:
            self.show_step(3)
            self.foot_var.set("✓ 虚拟机已自动启动，约 3 分钟后可打开管理界面")

    def _ensure_system_disk(self):
        """启动前确认系统盘真的可用，避免启动一块空盘让用户以为"卡住了"。"""
        sd = str(self.cfg.get("system_disk") or "").strip()
        if not sd or not Path(sd).exists():
            cands = find_existing_system_disks()
            if cands:
                best = cands[0]
                gb = best.stat().st_size / (1024 ** 3)
                if messagebox.askyesno(
                        APP_NAME,
                        "当前系统盘路径无效：\n%s\n\n"
                        "但检测到一块已安装过系统的硬盘：\n%s（%.1f GB）\n\n"
                        "是否改用它启动？" % (sd or "（空）", best, gb)):
                    self.disk_var.set(str(best))
                    self.cfg["system_disk"] = str(best)
                    self.apply_settings(silent=True)
                    self.logger.log("启动前自动切换系统盘：%s" % best)
                    return True
            messagebox.showerror(
                APP_NAME,
                "系统盘不存在：\n%s\n\n请先在第 2 步创建虚拟硬盘，或选择已有的系统盘。"
                % (sd or "（空）"))
            return False

        if disk_is_blank(sd):
            others = [p for p in find_existing_system_disks()
                      if str(p).lower() != sd.lower()]
            if others:
                best = others[0]
                gb = best.stat().st_size / (1024 ** 3)
                if messagebox.askyesno(
                        APP_NAME,
                        "当前选中的系统盘是一块空盘（还没装系统）：\n%s\n\n"
                        "同时检测到已装过系统的硬盘：\n%s（%.1f GB）\n\n"
                        "是否改用它启动？（推荐）" % (sd, best, gb)):
                    self.disk_var.set(str(best))
                    self.cfg["system_disk"] = str(best)
                    self.apply_settings(silent=True)
                    self.logger.log("启动前从空盘切换到真实系统盘：%s" % best)
                    return True
            return messagebox.askyesno(
                APP_NAME,
                "系统盘是一块空盘（%.0f KB），里面还没有安装飞牛系统。\n\n"
                "若这是首次安装，启动后需从 ISO 引导安装；\n"
                "若你之前已装好系统，说明选错了盘。\n\n仍要启动吗？"
                % (disk_volume_bytes(sd) / 1024))
        return True

    def start_vm(self):
        # 外部虚拟机还在跑时先接管，避免重复启动导致端口冲突
        if not self.qemu.running:
            ext = find_running_qemu()
            if ext:
                self.qemu.attach_external(ext[0][0])
        if self.qemu.running:
            messagebox.showinfo(APP_NAME, "虚拟机已经在运行中。")
            return
        if not self.apply_settings(silent=True):
            return
        if not self._ensure_system_disk():
            self.foot_var.set("已取消启动：请先准备可用的系统盘")
            return
        ok, msg = self.qemu.start()
        if not ok:
            messagebox.showerror(APP_NAME, msg)
        else:
            self.show_step(3)
            self.foot_var.set("✓ 虚拟机已启动，等待系统引导完成后点「打开管理界面」")

    def stop_vm(self):
        # 程序重启后外部虚拟机还在跑：先接管，否则会误判成"未运行"
        if not self.qemu.running:
            ext = find_running_qemu()
            if ext:
                self.qemu.attach_external(ext[0][0])
        if not self.qemu.running:
            messagebox.showinfo(APP_NAME, "虚拟机未在运行。")
            return
        if not messagebox.askyesno(
                APP_NAME,
                "确定要关闭虚拟机吗？\n建议先在系统内正常关机以保护数据。\n\n"
                "如果系统不响应，约 40 秒后会强制结束虚拟机。"):
            return
        self.foot_var.set("正在关闭虚拟机，请稍候…")
        self.stop_btn.configure(state="disabled")

        def _worker():
            try:
                ok, msg = self.qemu.stop()
            except Exception as e:
                ok, msg = False, str(e)
            try:
                self.after(0, lambda: self._on_stop_done(ok, msg))
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_stop_done(self, ok, msg):
        if ok:
            self.foot_var.set("✓ " + msg)
            self.status_var.set("● 虚拟机未运行")
            self.status_lbl.configure(foreground="#9CA3AF")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self._update_addr_display(False)
            self._hide_vm_banner()
        else:
            self.stop_btn.configure(state="normal")
            self.foot_var.set("关闭失败：" + msg)
            messagebox.showwarning(APP_NAME, msg)

    def detect_web_scheme(self):
        """探测飞牛 Web 是否就绪。返回 (scheme, ready)。
        只用标准 socket 发原始 HTTP 请求，不加载 ssl/urllib——
        PyInstaller 环境下 SSL 上下文会在 C 层触发 0xc0000409 fail-fast。"""
        port = int(self.cfg["web_port"])
        # 纯 socket 探测 HTTP：能连上并收到 HTTP 响应头即视为就绪
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=3)
            s.settimeout(3)
            s.sendall(("GET / HTTP/1.0\r\nHost: localhost:%d\r\n\r\n" % port).encode())
            data = s.recv(256)
            s.close()
            if data.startswith(b"HTTP/"):
                return "http", True
        except Exception:
            pass
        # 连不上端口：默认 http（当前主流飞牛版本用 http）
        return "http", False

    @staticmethod
    def _open_url(url):
        """用最底层的 Windows ShellExecute 打开浏览器，避开 webbrowser 模块
        在 PyInstaller GUI 程序里 subprocess 触发的 C 层崩溃（0xc0000409）。"""
        return open_url_safe(url)

    def open_web(self):
        # 当前程序接管的虚拟机，或外部检测到正在运行的虚拟机，都直接打开
        vm_on = self.qemu.running or bool(find_running_qemu())
        if not vm_on:
            if not messagebox.askyesno(APP_NAME, "虚拟机尚未启动，此时打开页面可能无法访问。\n仍要继续吗？"):
                return
        scheme, ready = self.detect_web_scheme()
        url = "%s://localhost:%d" % (scheme, self.cfg["web_port"])
        self._open_url(url)
        if ready:
            self.foot_var.set("✓ 飞牛管理界面已打开：" + url)
            self.mark_step_done(3, "飞牛系统已就绪，管理界面可访问")
            self.step_success(self.frames[3], "飞牛系统启动成功，管理界面已可正常访问")
            self.mark_step_done(5, "已打开飞牛桌面，可进入应用中心")
        else:
            self.foot_var.set("已打开：" + url + "（系统仍在启动，请稍候刷新）")

    def open_app(self):
        path = self.app_path_var.get().strip()
        if not path:
            messagebox.showinfo(APP_NAME, "请先填写应用访问地址。")
            return
        if not path.startswith(("http://", "https://")):
            scheme = self.detect_web_scheme()
            path = "%s://localhost:%d/%s" % (scheme, self.cfg["web_port"], path.lstrip("/"))
        self._open_url(path)

    def refresh_cmd(self):
        if not hasattr(self, "cmd_text"):
            return
        try:
            cmd, accel = self.qemu.build_command()
            text = subprocess.list2cmdline(cmd)
        except Exception as e:
            text = "（配置不完整：%s）" % e
        self.cmd_text.delete("1.0", "end")
        self.cmd_text.insert("1.0", text)

    def copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self.cmd_text.get("1.0", "end").strip())
        messagebox.showinfo(APP_NAME, "命令已复制到剪贴板。")

    def copy_fpk_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(self.fpk_cmd.get())
        messagebox.showinfo(APP_NAME, "安装命令已复制。")

    def copy_ssh_cmd(self):
        self.clipboard_clear()
        self.clipboard_append("ssh root@localhost -p %d" % self.cfg["ssh_port"])
        messagebox.showinfo(APP_NAME, "SSH 命令已复制。")

    def destroy(self):
        # 只结束本程序；虚拟机保持运行，下次打开程序会自动检测并接管。
        # 关键：必须先把嵌入的虚拟机画面还给系统，否则它作为本窗口的子窗口
        # 会被一起销毁，导致虚拟机仍在运行却再也看不到画面。
        try:
            if self.qemu.embedded_hwnd:
                self.qemu.release_embedded(show=True)
        except Exception:
            pass
        # 不在销毁流程里弹对话框，避免 Tk 销毁期的重入崩溃。
        try:
            super().destroy()
        except Exception:
            pass


def _prepare_runtime_with_splash():
    """准备 QEMU 运行时。若需要解压/复制（首次启动），显示一个进度窗口。"""
    src = _source_qemu_root()
    if src.exists() and src.is_dir() and _is_ascii_path(src):
        return True, "运行时位于程序目录"

    root = None
    try:
        root = tk.Tk()
        root.title(APP_NAME)
        w, h = 500, 160
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        root.resizable(False, False)
        ttk.Label(root, text="首次启动，正在准备运行环境",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(20, 4))
        lbl = ttk.Label(root, text="请稍候…", font=("Microsoft YaHei UI", 9),
                        foreground="#4B5563")
        lbl.pack()
        bar = ttk.Progressbar(root, length=400, maximum=100, mode="determinate")
        bar.pack(pady=14)
        root.update()
    except Exception:
        root = None

    state = {"text": "请稍候…", "frac": 0.0, "done": False, "result": (False, "")}

    def progress(text, frac=None):
        if text:
            state["text"] = text
        if frac is not None:
            state["frac"] = frac

    def work():
        try:
            state["result"] = ensure_qemu_runtime(progress)
        except Exception as e:
            state["result"] = (False, "准备运行环境失败：%s" % e)
        state["done"] = True

    threading.Thread(target=work, daemon=True).start()

    if root is not None:
        while not state["done"]:
            try:
                bar["value"] = min(100.0, max(0.0, state["frac"] * 100))
                lbl.configure(text=state["text"])
                root.update()
            except Exception:
                break
            time.sleep(0.05)
        try:
            root.destroy()
        except Exception:
            pass
    else:
        while not state["done"]:
            time.sleep(0.1)
    return state["result"]


def main():
    # 若程序在中文路径下，先把 QEMU 放到纯英文目录（QEMU 固件加载需要）。
    # 单文件版首次启动会解压内嵌运行时，此时显示进度窗口。
    ok, msg = _prepare_runtime_with_splash()
    if not ok:
        print(msg)
    app = App()
    if not ok:
        messagebox.showwarning(APP_NAME, msg + "\n\n程序仍可使用，但可能无法启动虚拟机。")

    # 带 --autostart 参数时，界面起来后自动启动虚拟机（供一键启动脚本使用）
    if "--autostart" in sys.argv:
        app.after(1200, app.autostart_vm)

    app.mainloop()


if __name__ == "__main__":
    main()
