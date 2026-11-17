# -*- mode: python ; coding: utf-8 -*-
"""单文件版配置：Python 部分全部打进一个 exe。

QEMU 运行时不在 spec 里，由 tools/build_single.py 压缩后附加到 exe 末尾。
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src" / "fnnas_lab.py"

a = Analysis(
    [str(SRC)],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["numpy", "pandas", "matplotlib", "PIL", "scipy"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="飞牛fnOS本地测试环境",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
