# -*- coding: utf-8 -*-
"""
打包 Windows 版（GitHub Actions 在 Windows 上自动运行，不用手动跑）：
把当前 Python（含 tkinter）复制成便携的 python 文件夹，和工具脚本一起压成 zip。
解压后双击「启动.bat」就能用，不用装 Python。
"""
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
APP = "视觉小说翻译器"
STAGE = os.path.join(DIST, APP)
ZIP_NAME = sys.argv[1] if len(sys.argv) > 1 else "VNTranslator-windows-x64.zip"

TOOL_FILES = ["cn_core.py", "cn_gui.py", "cn_unity.py", "启动.bat", "README.md"]
# 运行用不到的标准库部分，删掉省体积
SKIP_LIB = {"test", "idlelib", "ensurepip", "site-packages", "lib2to3", "pydoc_data", "turtledemo", "venv", "__pycache__"}
ROOT_FILES = ["python.exe", "pythonw.exe", "python3.dll", "LICENSE.txt"]


def copy_runtime(dst):
    src = sys.base_prefix
    os.makedirs(dst)
    for name in os.listdir(src):
        low = name.lower()
        if name in ROOT_FILES or (low.startswith(("python3", "vcruntime")) and low.endswith(".dll")):
            shutil.copy2(os.path.join(src, name), dst)
    shutil.copytree(os.path.join(src, "DLLs"), os.path.join(dst, "DLLs"),
                    ignore=shutil.ignore_patterns("*.ico", "_test*.pyd", "_ctypes_test.pyd", "xxlimited*.pyd", "__pycache__"))
    shutil.copytree(os.path.join(src, "tcl"), os.path.join(dst, "tcl"),
                    ignore=shutil.ignore_patterns("tix*", "*.lib", "demos"))

    def ignore_lib(d, names):
        if os.path.abspath(d) == os.path.abspath(os.path.join(src, "Lib")):
            return [n for n in names if n in SKIP_LIB]
        return [n for n in names if n == "__pycache__"]

    shutil.copytree(os.path.join(src, "Lib"), os.path.join(dst, "Lib"), ignore=ignore_lib)


def main():
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(STAGE)
    for f in TOOL_FILES:
        shutil.copy2(os.path.join(HERE, f), STAGE)
    copy_runtime(os.path.join(STAGE, "python"))

    # 自检：用打包出来的 Python 跑一遍，确认 tkinter / ssl 都在，脚本能导入
    py = os.path.join(STAGE, "python", "python.exe")
    check = ("import sys, os, tkinter, ssl, sqlite3, json;"
             "assert os.path.normcase(sys.prefix) == os.path.normcase(os.path.abspath('python')), sys.prefix;"
             "tkinter.Tcl().eval('info patchlevel');"
             "import cn_core, cn_unity, cn_gui;"
             "assert not os.path.exists('config.json');"
             "print('ok', sys.version)")
    subprocess.check_call([py, "-E", "-s", "-c", check], cwd=STAGE,
                          env={k: v for k, v in os.environ.items() if not k.startswith("PYTHON")})
    for root, dirs, files in os.walk(STAGE):
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d))
                dirs.remove(d)

    out = os.path.join(DIST, ZIP_NAME)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, _, files in os.walk(STAGE):
            for f in files:
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, DIST))
    print("built", out, "%.1f MB" % (os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
