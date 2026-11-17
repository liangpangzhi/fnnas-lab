# -*- mode: python ; coding: utf-8 -*-
"""目录版打包配置：exe + _internal + runtime/qemu 文件夹。

构建： pyinstaller packaging/onedir.spec  （或 python src/build.py）
"""
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src" / "fnnas_lab.py"
APPNAME = "飞牛fnOS本地测试环境"

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
    [],
    exclude_binaries=True,
    name=APPNAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APPNAME,
)
