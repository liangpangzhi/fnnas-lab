# 飞牛 fnOS 本地测试环境

在 Windows 电脑上用一台虚拟机跑飞牛 fnOS，不用买真机就能安装系统、安装 FPK 应用、用本机浏览器打开应用界面。

整个软件是纯 Python 标准库 + 随包携带的便携版 QEMU，**不需要用户安装 Python、也不需要单独装虚拟机软件**。

## 功能

- 环境自检：QEMU / 固件 / 硬件虚拟化是否可用
- 镜像与系统盘管理：ISO 全新安装，或直接使用已有的 `.qcow2` 系统盘
- 虚拟机画面**嵌入软件窗口**，鼠标点哪里就是哪里（USB 绝对定位鼠标，无需 Ctrl+Alt+G）
- 一键启动 / 优雅关机 / 打开管理界面
- **应用端口自动转发**：装完应用直接用 `http://localhost:端口` 打开
- **应用端口扫描**：不知道应用用了哪个端口时，一键扫描并批量添加转发
- 支持接管外部已启动的虚拟机（关掉软件不影响虚拟机继续运行）
- 单文件版：整个软件打包成一个 exe，首次启动自解压运行环境

## 目录结构

```
src/fnnas_lab.py          主程序（单文件，约 3400 行，仅标准库）
src/build.py              旧版打包脚本（目录版）
packaging/onedir.spec     目录版 PyInstaller 配置
packaging/single_file.spec 单文件版 PyInstaller 配置
scripts/build_single.py   单文件版打包脚本（含运行时瘦身 + 附加数据）
docs/使用说明.txt          面向使用者的完整说明与常见问题
runtime/qemu/             便携版 QEMU（不入库，需自行放入）
```

## 运行

```bash
python src/fnnas_lab.py
```

需要 Python 3.9+（带 tkinter）。程序会自动在 `runtime/qemu`、`tools/qemu` 等位置寻找 QEMU。

## 打包

### 1. 准备 QEMU 运行时

把 Windows 版 QEMU（建议 11.x，需包含 `qemu-system-x86_64.exe`、`qemu-img.exe` 与 `share/` 固件目录）解压到：

```
runtime/qemu/
```

### 2. 目录版（exe + 文件夹）

```bash
python src/build.py
```

产物在 `dist/飞牛fnOS本地测试环境/`，QEMU 会被复制进 `runtime/qemu`。

### 3. 单文件版（推荐分发）

```bash
python scripts/build_single.py
```

做三件事：

1. 用 PyInstaller `--onefile` 把 Python 部分打进一个 exe（约 10 MB）
2. 精简 QEMU 运行时（去掉非 x86 固件与多余工具，721 MB → 305 MB），压缩成 zip（约 117 MB）
3. 把 zip 以**附加数据**形式拼在 exe 末尾，并在尾部写入 `[8 字节偏移][魔数 FNPLD001]`

运行时首次启动时，程序读取自身尾部标记，把内嵌压缩包解压到
`C:\Users\Public\Documents\fnnas-qemu-runtime-v1\`（纯英文路径，QEMU 不能读中文路径），
之后启动直接复用，不再解压。

产物：`dist/飞牛fnOS本地测试环境_单文件_v<版本>.exe`

## 关键实现说明

| 问题 | 处理方式 |
|---|---|
| QEMU 不能读含中文的路径 | 运行时和平铺的磁盘副本都放到 `C:\Users\Public\Documents` 下 |
| 嵌入窗口后鼠标点不动 | 接入 `usb-tablet` 绝对定位鼠标 + `SDL_MOUSE_FOCUS_CLICKTHROUGH` |
| 关掉软件会连带杀死虚拟机 | QEMU 输出写入日志文件，不走管道 |
| 关掉软件会销毁嵌入的画面 | 退出前把画面还原成独立窗口 |
| 端口转发绑定失败会导致虚拟机起不来 | 启动成功后再用 QMP `hostfwd_add` 动态补加 |
| 应用端口不可知 | 扫描 guest 端口段，逐个试连后批量转发 |
| 系统盘被误覆盖 | 建盘前校验已有盘是否可用，可用则复用；启动前再校验一次 |

## 版本

当前版本 **2.4.0**（单文件版）。历史变更见 `docs/使用说明.txt` 开头的版本说明。

## 说明

仓库不包含 QEMU 可执行文件与虚拟机磁盘镜像（体积原因），也不包含打包产物。
