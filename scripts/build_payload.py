#!/usr/bin/env python3
"""
Assemble the payload that gets embedded in OnlyScans.exe:

    python/          a standalone Windows CPython (python-build-standalone), trimmed
    python/Lib/site-packages/serial/   pyserial
    onlyscans.py     the app

Writes build/payload.tar.gz and build/payload.id (a short hash the launcher uses to
know when to unpack a new version). Runs on Windows, macOS or Linux; the Python that
goes into the payload is always the Windows build.

    python scripts/build_payload.py
    python scripts/build_payload.py --python-tarball path/to/cpython-...-windows-msvc-install_only.tar.gz
"""

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

# Pinned so builds are reproducible. Newer builds: https://github.com/astral-sh/python-build-standalone/releases
PBS_TAG = "20261123"
PY_VERSION = "3.12.16"
PYSERIAL = "pyserial==3.5"
# 32-bit: J2534 dealer-tool drivers (Mongoose, VCM II, VCMM, GNA600 ...) are mostly 32-bit
# DLLs, and a process can only load DLLs of its own bitness. 32-bit Python runs fine on
# 64-bit Windows and talks to serial (ELM327) adapters exactly the same.
ARCH = "i686-pc-windows-msvc"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(ROOT, "build")

# Parts of the Windows Python that OnlyScans never uses (saves ~60% of the size).
TRIM_DIRS = ["Lib/test", "Lib/idlelib", "Lib/tkinter", "Lib/turtledemo", "Lib/ensurepip",
             "Lib/lib2to3", "Lib/unittest/test", "tcl", "include", "libs", "Scripts"]
TRIM_FILES = ["Lib/turtle.py", "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll"]


def python_url():
    name = f"cpython-{PY_VERSION}+{PBS_TAG}-{ARCH}-install_only.tar.gz"
    return f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_TAG}/{name}"


def fetch_python(dest):
    url = python_url()
    print(f"Downloading Windows Python {PY_VERSION} ({ARCH}) ...\n  {url}")
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def trim(py):
    for d in TRIM_DIRS:
        shutil.rmtree(os.path.join(py, d), ignore_errors=True)
    for f in TRIM_FILES:
        p = os.path.join(py, f)
        if os.path.exists(p):
            os.remove(p)
    sp = os.path.join(py, "Lib", "site-packages")
    for name in os.listdir(sp):
        if name.startswith("pip"):
            shutil.rmtree(os.path.join(sp, name), ignore_errors=True)
    for dirpath, dirnames, filenames in os.walk(py):
        for f in filenames:
            if f.endswith(".pdb"):
                os.remove(os.path.join(dirpath, f))
        for d in list(dirnames):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(dirpath, d))
                dirnames.remove(d)


def add_pyserial(py, work):
    wheels = os.path.join(work, "wheels")
    print(f"Downloading {PYSERIAL} ...")
    subprocess.check_call([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
                           "--only-binary=:all:", "-d", wheels, PYSERIAL])
    whl = next(os.path.join(wheels, w) for w in os.listdir(wheels) if w.endswith(".whl"))
    zipfile.ZipFile(whl).extractall(os.path.join(py, "Lib", "site-packages"))


def make_tar(stage, out):
    def norm(ti):  # stable metadata, so identical inputs give an identical payload
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mtime = 0
        return ti
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for top in ("python", "onlyscans.py"):
            path = os.path.join(stage, top)
            if os.path.isdir(path):
                for dirpath, dirnames, filenames in os.walk(path):
                    dirnames.sort()
                    rel = os.path.relpath(dirpath, stage).replace(os.sep, "/")
                    tar.add(dirpath, arcname=rel, recursive=False, filter=norm)
                    for f in sorted(filenames):
                        tar.add(os.path.join(dirpath, f), arcname=f"{rel}/{f}", filter=norm)
            else:
                tar.add(path, arcname=top, filter=norm)
    import gzip
    with open(out, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(buf.getvalue())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--python-tarball", help="use an already-downloaded python-build-standalone tarball")
    args = ap.parse_args()

    shutil.rmtree(BUILD, ignore_errors=True)
    work = os.path.join(BUILD, "work")
    stage = os.path.join(BUILD, "stage")
    os.makedirs(work)
    os.makedirs(stage)

    tarball = args.python_tarball or fetch_python(os.path.join(work, "python.tar.gz"))
    print("Unpacking and trimming Python ...")
    with tarfile.open(tarball) as t:
        t.extractall(stage, filter="data") if sys.version_info >= (3, 12) else t.extractall(stage)
    py = os.path.join(stage, "python")
    if not os.path.exists(os.path.join(py, "python.exe")):
        sys.exit("The tarball doesn't look like a Windows python-build-standalone 'install_only' build.")
    trim(py)
    add_pyserial(py, work)
    shutil.copy(os.path.join(ROOT, "onlyscans.py"), os.path.join(stage, "onlyscans.py"))

    out = os.path.join(BUILD, "payload.tar.gz")
    print("Compressing payload ...")
    make_tar(stage, out)
    pid = hashlib.sha256(open(out, "rb").read()).hexdigest()[:12]
    with open(os.path.join(BUILD, "payload.id"), "w") as f:
        f.write(pid)
    print(f"Done: {out} ({os.path.getsize(out) / 1e6:.1f} MB), id {pid}")


if __name__ == "__main__":
    main()
