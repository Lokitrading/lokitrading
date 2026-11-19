"""Package Loki as a desktop app with PyInstaller (run on the OS you are building for).

    python desktop/build.py            # -> dist/Loki/ (Windows, Linux) or dist/Loki.app (macOS)

The app bundles the server code, the pages, the scripts and the trained model records; it saves
everything the user creates in their own app-data folder, never inside the app.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEP = ";" if os.name == "nt" else ":"


def main() -> int:
    data = [
        ("templates", "templates"),
        ("static", "static"),
        ("loki.ico", "."),
    ]
    data += [(str(p.relative_to(ROOT)), "loki_v8") for p in sorted((ROOT / "loki_v8").glob("*.json"))]
    icon = ROOT / ("loki.ico" if sys.platform != "darwin" else "static/favicon.png")
    args = [
        sys.executable, "-m", "PyInstaller", str(ROOT / "desktop" / "loki_desktop.py"),
        "--name", "Loki", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--paths", str(ROOT),
        "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        # Loki's own packages are imported by name from the server; uvicorn picks its loop and
        # protocol modules at run time; yfinance talks through curl_cffi's bundled library.
        "--collect-submodules", "loki", "--collect-submodules", "core", "--collect-submodules", "loki_v8",
        "--collect-submodules", "third_party", "--collect-submodules", "uvicorn",
        "--collect-all", "curl_cffi", "--collect-data", "certifi", "--collect-data", "yfinance",
        "--exclude-module", "tkinter", "--exclude-module", "matplotlib", "--exclude-module", "IPython",
        "--exclude-module", "pytest",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "app.loki.trading"]
    for src, dest in data:
        args += ["--add-data", f"{ROOT / src}{SEP}{dest}"]
    for stale in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(stale, ignore_errors=True)
    return subprocess.call(args, cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
