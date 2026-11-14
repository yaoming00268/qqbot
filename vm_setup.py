"""Install a private QEMU runtime without installers, elevation or reboot."""
from __future__ import annotations

import hashlib
import json
import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import urllib.request
import zipfile

from runtime_paths import application_home

ROOT = application_home()
QEMU_URL = "https://qemu.weilnetz.de/w64/2026/qemu-w64-setup-20260422.exe"
QEMU_SHA256 = "da95183368398b651acdb9236f033a76175c9d1298dd90e6c7836f528a63b27d"
SEVENZIP_URL = "https://api.nuget.org/v3-flatcontainer/7-zip.commandline/25.1.0/7-zip.commandline.25.1.0.nupkg"
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
SEVENZIP_MSI = "https://www.7-zip.org/a/7z2501-x64.msi"


def extract_msi_cabinets(source, destination):
    """Read MSI embedded cabinets. This does not install or execute the MSI."""
    api = ctypes.WinDLL("msi", use_last_error=True)
    handle = wintypes.UINT
    api.MsiOpenDatabaseW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(handle)]
    api.MsiDatabaseOpenViewW.argtypes = [handle, wintypes.LPCWSTR, ctypes.POINTER(handle)]
    api.MsiViewExecute.argtypes = [handle, handle]
    api.MsiViewFetch.argtypes = [handle, ctypes.POINTER(handle)]
    api.MsiRecordGetStringW.argtypes = [handle, wintypes.UINT, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    api.MsiRecordReadStream.argtypes = [handle, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    database, view = handle(), handle()
    def check(code):
        if code:
            raise OSError(code, "Unable to read the extractor MSI.")
    check(api.MsiOpenDatabaseW(str(source.resolve()), None, ctypes.byref(database)))
    cabinets = []
    try:
        check(api.MsiDatabaseOpenViewW(database, "SELECT `Name`, `Data` FROM `_Streams`", ctypes.byref(view)))
        check(api.MsiViewExecute(view, 0))
        while True:
            record = handle()
            code = api.MsiViewFetch(view, ctypes.byref(record))
            if code == 259:
                break
            check(code)
            try:
                size = wintypes.DWORD(1024)
                name = ctypes.create_unicode_buffer(size.value)
                check(api.MsiRecordGetStringW(record, 1, name, ctypes.byref(size)))
                if not name.value.lower().endswith(".cab"):
                    continue
                output = destination / Path(name.value).name
                with output.open("wb") as stream:
                    while True:
                        buffer = ctypes.create_string_buffer(1024 * 1024)
                        size = wintypes.DWORD(len(buffer))
                        check(api.MsiRecordReadStream(record, 2, buffer, ctypes.byref(size)))
                        if not size.value:
                            break
                        stream.write(buffer.raw[:size.value])
                cabinets.append(output)
            finally:
                api.MsiCloseHandle(record)
    finally:
        api.MsiCloseHandle(view)
        api.MsiCloseHandle(database)
    return cabinets


def download(url, path, expected=""):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and (not expected or hashlib.sha256(path.read_bytes()).hexdigest() == expected):
        return path
    partial = path.with_suffix(path.suffix + ".partial")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=30) as source, partial.open("wb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
            digest.update(chunk)
    if expected and digest.hexdigest() != expected:
        raise ValueError("Downloaded runtime checksum does not match the pinned winget manifest.")
    partial.replace(path)
    print(json.dumps({"downloaded": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}), flush=True)
    return path


def install():
    try:
        import pycdlib
    except ImportError:
        if getattr(sys, "frozen", False):
            raise RuntimeError("Runtime is missing pycdlib; rebuild the complete QQBot runtime.") from None
        print("[vm_setup] Installing pycdlib dependency...", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "pycdlib>=1.14.0"], check=False)
    directory = ROOT / "tools" / "qemu"
    cache = ROOT / "tools" / "downloads"
    binary = directory / "qemu-system-x86_64.exe"
    if not binary.is_file():
        package = download(SEVENZIP_URL, cache / "7zip-commandline-25.1.0.zip",
                           "ff4901e41cecf902c2d49b06cc836a2cb170d09ab934dc9e5d2fc5bfed7878a9")
        extract = ROOT / "tools" / "7zip"
        extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package) as archive:
            (extract / "7za.exe").write_bytes(archive.read("tools/x64/7za.exe"))
        msi = download(SEVENZIP_MSI, cache / "7zip-25.01-x64.msi")
        for cabinet in extract_msi_cabinets(msi, extract):
            subprocess.run([str(extract / "7za.exe"), "x", str(cabinet), "-o" + str(extract), "-y"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for filename in ("7z.exe", "7z.dll"):
            (extract / filename).write_bytes((extract / ("_" + filename)).read_bytes())
        sevenzip = extract / "7z.exe"
        if not sevenzip.is_file():
            raise ValueError("7-Zip command line package is missing the extractor.")
        installer = download(QEMU_URL, cache / "qemu-11.0.0.exe", QEMU_SHA256)
        directory.mkdir(parents=True, exist_ok=True)
        # Extract NSIS data only. Never run the QEMU installer or write HKLM.
        subprocess.run([str(sevenzip), "x", str(installer), "-o" + str(directory), "-y"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not binary.is_file():
            raise ValueError("QEMU executable was not extracted.")
    embedded = download(PYTHON_URL, cache / "python-3.12.10-embed-amd64.zip")
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.splitlines()[0]
    result = {"qemu": str(binary), "version": version, "guest_python": str(embedded),
              "host_input_used": False, "elevation_used": False, "reboot_required": False,
              "installer_sha256": QEMU_SHA256, "qemu_source": QEMU_URL, "extractor_source": SEVENZIP_URL}
    (directory / "installation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result), flush=True)
    return result


if __name__ == "__main__":
    install()
