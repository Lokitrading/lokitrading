"""Sure, run on this computer beside the Loki desktop app (one app, both programs).

When Docker is installed, the Loki app starts Sure (https://github.com/we-promise/sure) in the
background from the official, unmodified image: it copies ``desktop/sure/compose.yml`` into its
app-data folder, writes a ``.env`` beside it with secrets created once on this computer (never
reused from anywhere else), and runs ``docker compose -p loki-sure up -d``. Sure then answers on
http://127.0.0.1:3000 (this computer only) and Loki's "Your money" page connects to it.

Sure keeps its own licence (AGPL-3.0) and its data in Docker volumes; Loki only talks to it over
its API. Closing Loki leaves Sure running in Docker (it restarts with Docker), so your money data
is there next time; ``docker compose -p loki-sure down`` in the app-data ``sure`` folder stops it.
Set ``LOKI_SURE=0`` to never start it.
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Dict, Optional

PROJECT = "loki-sure"
PORT = 3000
URL = f"http://127.0.0.1:{PORT}"
ENV_KEYS = ("SECRET_KEY_BASE", "POSTGRES_PASSWORD", "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY",
            "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY", "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT")


def compose_source(bundle: Path) -> Path:
    return bundle / "desktop" / "sure" / "compose.yml"


def _docker() -> Optional[str]:
    exe = shutil.which("docker")
    if exe:
        return exe
    for candidate in (r"C:\Program Files\Docker\Docker\resources\bin\docker.exe", "/usr/local/bin/docker",
                      "/opt/homebrew/bin/docker", "/Applications/Docker.app/Contents/Resources/bin/docker"):
        if Path(candidate).exists():
            return candidate
    return None


def _run(args: list, cwd: Optional[Path] = None, timeout: float = 30) -> "subprocess.CompletedProcess[str]":
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0  # no console flashes
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=flags)


def docker_ready() -> Optional[str]:
    """The docker command when Docker is installed and its engine is running, else None."""
    exe = _docker()
    if not exe:
        return None
    try:
        return exe if _run([exe, "info", "--format", "{{.ServerVersion}}"], timeout=20).returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def answering(url: str = URL, timeout: float = 2.0) -> bool:
    """True when a Sure server answers its health check at ``url``."""
    try:
        with urllib.request.urlopen(url + "/up", timeout=timeout) as reply:
            return bool(reply.status == 200)
    except Exception:  # noqa: BLE001 - not up (yet)
        return False


def prepare(base: Path, bundle: Path) -> Path:
    """Write compose.yml and a .env with this computer's own secrets (created once, then kept)."""
    folder = base / "sure"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(compose_source(bundle), folder / "compose.yml")
    env_path = folder / ".env"
    values: Dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    makers = {
        "SECRET_KEY_BASE": lambda: secrets.token_hex(64),
        "POSTGRES_PASSWORD": lambda: secrets.token_urlsafe(24),
        "ACTIVE_RECORD_ENCRYPTION_PRIMARY_KEY": lambda: secrets.token_urlsafe(32),
        "ACTIVE_RECORD_ENCRYPTION_DETERMINISTIC_KEY": lambda: secrets.token_urlsafe(32),
        "ACTIVE_RECORD_ENCRYPTION_KEY_DERIVATION_SALT": lambda: secrets.token_urlsafe(32),
    }
    changed = False
    for key in ENV_KEYS:
        if not values.get(key):
            values[key] = makers[key]()
            changed = True
    values.setdefault("SURE_PORT", str(PORT))
    if changed or not env_path.exists():
        env_path.write_text("# Created by Loki on this computer. Keep it: Sure's data is encrypted with these.\n" +
                            "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
    return folder


def start(base: Path, bundle: Path) -> str:
    """Start Sure in the background; returns a state: running, starting, no-docker or failed."""
    if os.environ.get("LOKI_SURE") == "0":
        return "off"
    if answering():
        return "running"  # already up (Loki's or your own Sure on this port)
    exe = docker_ready()
    if not exe:
        return "no-docker"
    folder = prepare(base, bundle)
    try:
        # A first start downloads the images (about 2 GB); that can take minutes, so do not block on it.
        done = _run([exe, "compose", "-p", PROJECT, "-f", "compose.yml", "--env-file", ".env", "up", "-d"],
                    cwd=folder, timeout=1800)
    except (OSError, subprocess.SubprocessError) as error:
        logging.error("Sure did not start: %s", error)
        return "failed"
    if done.returncode != 0:
        logging.error("Sure did not start: %s", (done.stderr or done.stdout)[-800:])
        return "failed"
    return "starting"
