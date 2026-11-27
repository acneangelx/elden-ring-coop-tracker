"""Packages Elden Ring Seamless Co-op Live Tracker into a standalone single-file .exe."""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
BUILD_ASSETS = ROOT / "build_assets"
WEB_ZIP = BUILD_ASSETS / "web.zip"
PYINSTALLER = ROOT / ".venv_build" / "Scripts" / "pyinstaller.exe"


def make_web_zip() -> None:
    BUILD_ASSETS.mkdir(parents=True, exist_ok=True)
    if WEB_ZIP.exists():
        WEB_ZIP.unlink()
    compressed_exts = {".html", ".js", ".css", ".json", ".txt"}
    count = 0
    with zipfile.ZipFile(WEB_ZIP, "w") as zf:
        for path in sorted(WEB_DIR.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(WEB_DIR).as_posix()
            comp = zipfile.ZIP_DEFLATED if path.suffix.lower() in compressed_exts else zipfile.ZIP_STORED
            zf.write(path, arcname=rel, compress_type=comp)
            count += 1
    size_mb = WEB_ZIP.stat().st_size / (1024 * 1024)
    print(f"Packed {count} web assets into {WEB_ZIP.name} ({size_mb:.2f} MB)")


def run_pyinstaller() -> None:
    if not PYINSTALLER.exists():
        raise FileNotFoundError(f"PyInstaller not found at {PYINSTALLER}")
    cmd = [
        str(PYINSTALLER),
        "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--name", "EldenRingCoopTracker",
        "--add-data", f"{ROOT / 'data'};data",
        "--add-data", f"{WEB_ZIP};.",
        str(ROOT / "server.py"),
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)


def main() -> None:
    make_web_zip()
    run_pyinstaller()
    exe = ROOT / "dist" / "EldenRingCoopTracker.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        print(f"\nDONE! Standalone EXE: {exe} ({size_mb:.2f} MB)")


if __name__ == "__main__":
    main()
