# -*- coding: utf-8 -*-
"""打包单文件版：Python 程序打成一个 exe，QEMU 运行时压缩后附加在其末尾。

用法：
    python tools/build_single.py            # 打包 dist/飞牛fnOS本地测试环境_单文件_v<版本>.exe
    python tools/build_single.py --no-slim  # 不瘦身运行时（体积更大，排查问题时用）

前置条件：
    1. pip install pyinstaller
    2. 便携版 QEMU 放在 runtime/qemu/（需含 qemu-system-x86_64.exe、qemu-img.exe、share/）

产物结构（exe 末尾）：
    [PyInstaller 主体][zip 压缩的 QEMU 运行时][8 字节起始偏移][8 字节魔数]

程序运行时由 _payload_span() / _extract_payload() 解析尾部并解压到
C:\\Users\\Public\\Documents\\fnnas-qemu-runtime-v1\\qemu（QEMU 不能读中文路径）。
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "fnnas_lab.py"
SPEC = ROOT / "packaging" / "single_file.spec"
QEMU_SRC = ROOT / "runtime" / "qemu"
BUILD = ROOT / "build"
WORK = BUILD / "_work_single"
PACK = BUILD / "_pack_single"
SLIM = BUILD / "_slim_runtime" / "qemu"
PAYLOAD = BUILD / "_payload" / "runtime.zip"
DIST = ROOT / "dist"
APPNAME = "飞牛fnOS本地测试环境"
MAGIC = b"FNPLD001"

# 单文件版用不到的组件：其他架构的固件与多余的可执行文件
# 保留 qemu-system-x86_64.exe、qemu-img.exe 与全部 DLL。
DROP_FILES = {
    "qemu-system-x86_64w.exe",
    "qemu-io.exe",
    "qemu-nbd.exe",
    "qemu-storage-daemon.exe",
    "qemu-edid.exe",
    "qemu-ga.exe",
    "share/edk2-aarch64-code.fd",
    "share/edk2-arm-code.fd",
    "share/edk2-arm-vars.fd",
    "share/edk2-riscv-code.fd",
    "share/edk2-riscv-vars.fd",
    "share/edk2-loongarch64-code.fd",
    "share/edk2-loongarch64-vars.fd",
}


def say(msg):
    print("\n=== %s ===" % msg, flush=True)


def app_version():
    m = re.search(r'^APP_VER\s*=\s*"([^"]+)"', SRC.read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def check_env():
    if not SRC.exists():
        sys.exit("找不到 %s" % SRC)
    if not QEMU_SRC.exists():
        sys.exit("找不到 QEMU 运行时：%s\n请先把便携版 QEMU 解压到该目录。" % QEMU_SRC)
    r = subprocess.run([sys.executable, "-m", "PyInstaller", "--version"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("未安装 PyInstaller，请先执行： pip install pyinstaller")
    print("PyInstaller %s / Python %s" % (r.stdout.strip(), sys.version.split()[0]))


def build_exe():
    say("打包单文件 exe")
    for d in (WORK, PACK):
        shutil.rmtree(d, ignore_errors=True)
    r = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(PACK), "--workpath", str(WORK), str(SPEC)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout[-1500:])
        print(r.stderr[-2500:])
        sys.exit("PyInstaller 打包失败")
    exe = PACK / (APPNAME + ".exe")
    if not exe.exists():
        sys.exit("未生成 exe：%s" % exe)
    print("exe %.1f MB" % (exe.stat().st_size / 2 ** 20))
    return exe


def make_slim_runtime(slim=True):
    """把 runtime/qemu 精简后放到构建目录（保留目录结构，DLL 全留）。"""
    say("准备运行时（%s）" % ("瘦身" if slim else "完整复制"))
    shutil.rmtree(SLIM.parent, ignore_errors=True)
    n = 0
    raw = 0
    skipped = 0
    for cur, _dirs, files in os.walk(QEMU_SRC):
        rel = os.path.relpath(cur, QEMU_SRC)
        out_dir = SLIM if rel == "." else SLIM / rel
        out_dir.mkdir(parents=True, exist_ok=True)
        for fn in files:
            key = fn if rel == "." else rel.replace("\\", "/") + "/" + fn
            if slim and key in DROP_FILES:
                skipped += 1
                continue
            src = Path(cur) / fn
            dst = out_dir / fn
            try:
                os.link(src, dst)          # 硬链接，不额外占空间
            except OSError:
                shutil.copy2(src, dst)
            n += 1
            raw += src.stat().st_size
    print("保留 %d 个文件 %.0f MB，跳过 %d 个" % (n, raw / 2 ** 20, skipped))
    return SLIM


def make_payload():
    say("压缩运行时")
    PAYLOAD.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    n = 0
    with zipfile.ZipFile(PAYLOAD, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for cur, _dirs, files in os.walk(SLIM):
            rel = os.path.relpath(cur, SLIM)
            parts = [] if rel == "." else rel.split(os.sep)
            for fn in files:
                z.write(Path(cur) / fn, "/".join(["qemu"] + parts + [fn]))
                n += 1
    print("%d 个文件 -> %.1f MB，耗时 %.0f 秒"
          % (n, PAYLOAD.stat().st_size / 2 ** 20, time.time() - t0))
    return PAYLOAD


def append_payload(exe, payload, ver):
    say("附加到 exe 末尾")
    DIST.mkdir(parents=True, exist_ok=True)
    final = DIST / ("%s_单文件_v%s.exe" % (APPNAME, ver))
    # copy2 直接覆盖（先删除在某些受限环境会被拦截）
    shutil.copy2(exe, final)
    start = final.stat().st_size
    with open(final, "ab") as out, open(payload, "rb") as src:
        shutil.copyfileobj(src, out, 8 << 20)
    with open(final, "ab") as out:
        out.write(struct.pack("<Q", start))
        out.write(MAGIC)
    size = final.stat().st_size
    print("产物 %s" % final)
    print("  主体 %.1f MB + 运行时 %.1f MB = %.1f MB"
          % (start / 2 ** 20, (size - start - 16) / 2 ** 20, size / 2 ** 20))
    # 自检尾部标记
    with open(final, "rb") as f:
        f.seek(-16, os.SEEK_END)
        tail = f.read(16)
        off = struct.unpack("<Q", tail[:8])[0]
        f.seek(off)
        head = f.read(4)
    assert tail.endswith(MAGIC) and off == start, "尾部标记写入异常"
    assert head == b"PK\x03\x04", "附加数据不是 zip"
    print("  尾部标记校验通过")
    return final


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-slim", action="store_true", help="不精简运行时")
    args = ap.parse_args()
    check_env()
    ver = app_version()
    print("版本 %s" % ver)
    exe = build_exe()
    make_slim_runtime(slim=not args.no_slim)
    payload = make_payload()
    append_payload(exe, payload, ver)


if __name__ == "__main__":
    main()
