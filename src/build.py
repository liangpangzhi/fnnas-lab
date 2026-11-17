# -*- coding: utf-8 -*-
"""打包脚本：把主程序 + QEMU 运行时组装成可直接运行的绿色文件夹。"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
DIST = ROOT / "dist"
APPNAME = "飞牛fnOS本地测试环境"

# 用带 tkinter 的完整版 Python 来打包
FULL_PY = Path(r"C:\fnnas-build-python\python.exe")
PY = str(FULL_PY) if FULL_PY.exists() else sys.executable


def run(cmd, cwd=None):
    print(">", subprocess.list2cmdline(cmd))
    subprocess.check_call(cmd, cwd=cwd)


def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)

    # 1) PyInstaller onedir 打包主程序
    cmd = [
        PY, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APPNAME,
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        str(SRC / "fnnas_lab.py"),
    ]
    run(cmd, cwd=str(ROOT))

    out = DIST / APPNAME

    # 2) 复制 QEMU 运行时
    qsrc = ROOT / "runtime" / "qemu"
    qdst = out / "runtime" / "qemu"
    if qdst.exists():
        shutil.rmtree(qdst)
    qdst.parent.mkdir(parents=True, exist_ok=True)
    print("Copy QEMU runtime ->", qdst)
    shutil.copytree(str(qsrc), str(qdst))

    # 3) 复制说明文档
    docs = ROOT / "docs"
    if docs.exists():
        for f in docs.glob("*"):
            if f.is_file():
                shutil.copy2(str(f), str(out / f.name))

    # 4) 生成一键启动批处理（可选入口）
    bat = out / ("启动" + APPNAME + ".bat")
    bat.write_text(
        '@echo off\nchcp 65001 >nul\n'
        'start "" "%%~dp0%s.exe"\n' % APPNAME,
        encoding="utf-8")

    print("\nBUILD_DONE:", out)


if __name__ == "__main__":
    main()
