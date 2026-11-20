"""Loki as a desktop app: the same workspace, served on this computer, in its own window.

Double-click the built app (or run ``python desktop/loki_desktop.py``) and it:

1. keeps everything you save in your own app-data folder (Windows ``%LOCALAPPDATA%\\Loki``,
   macOS ``~/Library/Application Support/Loki``, Linux ``~/.local/share/loki``), so updates and
   reinstalls never touch it;
2. starts Loki's server on this computer only (127.0.0.1, never your network), so the trading
   AIs, research and paper trading all run on the laptop;
3. opens Loki in its own window (Edge WebView2 on Windows, WebKit on macOS). If that window
   cannot be created, it opens Loki in Chrome or Edge as an app window, or in your browser.

Closing the window stops the server. ``--browser`` skips the native window; ``--smoke`` starts
the server, checks the pages answer and exits (used by the build to test each packaged app).
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any, Callable, Dict

APP_NAME = "Loki"
# A fixed port keeps the window's saved data (journals, settings, nickname) under one address
# from launch to launch; another port is used only when something else already holds this one.
PREFERRED_PORT = 47821
HOST = "127.0.0.1"

SPLASH = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;height:100%;background:#0b1015;color:#a8f0c6;font:15px/1.6 Inter,"Segoe UI",system-ui,sans-serif}
.w{height:100%;display:grid;place-items:center;text-align:center}
.m{width:64px;height:64px;border:2px solid #a8f0c6;border-radius:18px;display:grid;place-items:center;margin:0 auto 18px;
font:600 30px/1 Inter,"Segoe UI",sans-serif;animation:p 1.6s ease-in-out infinite}
@keyframes p{50%{box-shadow:0 0 0 10px rgba(168,240,198,.08)}}
small{color:#7f93a1;display:block;margin-top:6px}</style></head>
<body><div class="w"><div><div class="m">L</div>Starting Loki on this computer…<small>The trading AIs run locally.</small></div></div></body></html>"""


def bundle_root() -> Path:
    """Where templates/, static/ and the model records live (the unpacked app, or the repository)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    override = os.environ.get("LOKI_APP_DATA")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def _persistent_secret(path: Path, make: Callable[[], str]) -> str:
    """A secret created once on this computer and reused, readable only by this user."""
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(make())
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


def configure_environment(base: Path) -> None:
    """Point every file Loki writes at the app-data folder and give it stable local secrets."""
    from cryptography.fernet import Fernet

    secrets_dir = base / "secrets"
    secrets_dir.mkdir(exist_ok=True)
    defaults = {
        "LOKI_DATA_DIR": str(base / "data"),
        "LOKI_MEMORY_DIR": str(base / "memory"),
        "LOKI_ANALOGUE_DIR": str(base / "analogue_models"),
        "LOKI_PAPER_AI_DB": str(base / "paper_ai" / "research.sqlite3"),
        "LOKI_REAL_ORDER_JOURNAL": str(base / "real" / "orders.sqlite3"),
        # Encrypts a saved funded-account login and signs this computer's cookies; created once.
        "LOKI_BROKER_ENCRYPTION_KEY": _persistent_secret(secrets_dir / "broker.key", lambda: Fernet.generate_key().decode()),
        "LOKI_GATE_SECRET": _persistent_secret(secrets_dir / "cookie.key", lambda: secrets.token_urlsafe(48)),
        "LOKI_DESKTOP": "1",
    }
    for key, value in defaults.items():
        os.environ.setdefault(key, value)
    for key in ("LOKI_DATA_DIR", "LOKI_MEMORY_DIR", "LOKI_ANALOGUE_DIR"):
        Path(os.environ[key]).mkdir(parents=True, exist_ok=True)
    # The desktop app is never the hosted site: no access gate, no secure-only cookies over http.
    os.environ.pop("VERCEL", None)
    os.environ.pop("LOKI_SITE_GATE", None)


def _answers(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            return reply.status == 200 and b'"service":"loki"' in reply.read(4096)
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def pick_port() -> tuple[int, bool]:
    """(port, already_running): reuse a Loki that is already open, else the preferred port, else any free one."""
    if _answers(f"http://{HOST}:{PREFERRED_PORT}/health"):
        return PREFERRED_PORT, True
    if _port_free(PREFERRED_PORT):
        return PREFERRED_PORT, False
    return _free_port(), False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


class LocalServer:
    """Loki's FastAPI app under uvicorn, in a background thread, on loopback only."""

    def __init__(self, port: int) -> None:
        import uvicorn

        root = bundle_root()
        os.chdir(root)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from loki.api.server import app

        # Every path the window has requested: the window self-test reads it, because the page's
        # security policy rightly forbids the eval() that pywebview's evaluate_js relies on.
        self.seen: set[str] = set()

        async def watched(scope: Dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") == "http":
                self.seen.add(str(scope.get("path", "")))
            await app(scope, receive, send)

        # log_config=None: keep the app's own file logging; uvicorn's console logging cannot work
        # in a windowed app, which has no console.
        config = uvicorn.Config(watched, host=HOST, port=port, log_level="warning", access_log=False, lifespan="on",
                                log_config=None)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="loki-server", daemon=True)
        self.url = f"http://{HOST}:{port}"

    def start(self) -> None:
        self.thread.start()

    def wait_ready(self, seconds: float = 90.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if _answers(self.url + "/health"):
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.15)
        return False

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=8)


def open_in_browser(url: str) -> None:
    """Chrome or Edge in app mode looks like a native window; otherwise the default browser."""
    candidates = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            root = os.environ.get(env)
            if root:
                candidates += [Path(root) / "Microsoft/Edge/Application/msedge.exe", Path(root) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                       Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        candidates += [Path(p) for p in filter(None, (shutil.which(n) for n in ("google-chrome", "chromium", "microsoft-edge")))]
    for exe in candidates:
        if exe.exists():
            try:
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=1440,900"], close_fds=True)
                return
            except OSError:
                continue
    webbrowser.open(url)


def run_window(server: LocalServer | None, url: str, base: Path, icon: Path | None) -> bool:
    """The native window. Returns False when no native webview is available on this computer."""
    try:
        import webview
    except Exception as error:  # pywebview missing or its GUI backend cannot load
        logging.warning("native window unavailable: %s", error)
        return False
    webview.settings["ALLOW_DOWNLOADS"] = True  # journal backups and CSV exports
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    window = webview.create_window(
        APP_NAME, html=SPLASH, width=1440, height=900, min_size=(380, 600),
        background_color="#0b1015", text_select=True, zoomable=True,
    )

    def load_when_ready() -> None:
        if server is not None and not server.wait_ready():
            window.load_html(SPLASH.replace("Starting Loki on this computer…", "Loki could not start.")
                             .replace("The trading AIs run locally.", f"Details are in {base / 'loki.log'}"))
            return
        window.load_url(url + "/#overview")

    try:
        webview.start(load_when_ready, private_mode=False, storage_path=str(base / "window"),
                      icon=str(icon) if icon and icon.exists() else None)
    except Exception as error:
        logging.warning("native window failed: %s", error)
        return False
    return True


def start_sure(base: Path) -> None:
    """Start Sure (personal finance) beside Loki in the background, when Docker is available."""
    import sure_local

    os.environ["LOKI_SURE_LOCAL_URL"] = sure_local.URL
    os.environ["LOKI_SURE_LOCAL"] = "checking"

    def go() -> None:
        try:
            state = sure_local.start(base, bundle_root())
        except Exception as error:  # noqa: BLE001 - Loki works without Sure
            logging.error("Sure start failed: %s", error)
            state = "failed"
        os.environ["LOKI_SURE_LOCAL"] = state
        logging.info("Sure on this computer: %s", state)

    threading.Thread(target=go, name="sure-start", daemon=True).start()


def smoke(server: LocalServer) -> int:
    """Packaged-app self-test: the server starts and the workspace, a script and an AI read answer."""
    if not server.wait_ready():
        return 1
    checks = ["/health", "/", "/static/js/workspace.js", "/api/fusion/research-book"]
    for path in checks:
        try:
            with urllib.request.urlopen(server.url + path, timeout=30) as reply:
                body = reply.read()
                if reply.status != 200 or not body:
                    return 2
        except Exception as error:
            logging.error("smoke %s failed: %s", path, error)
            return 3
    return 0


# Requests only the workspace's own scripts make once they have run in the window.
WINDOW_PROOF = ("/static/js/workspace.js", "/api/status/feeds", "/api/fusion/research-book")


def window_check(server: LocalServer, seconds: float = 90.0) -> int:
    """Packaged-app self-test of the real window: the workspace loads in it and its scripts run.

    Proof comes from the server: the page's scripts, once running in the window, fetch the feed
    status and the research book. Nothing is read through evaluate_js (it needs eval(), which the
    workspace's Content-Security-Policy blocks, as it should).
    """
    import webview

    if not server.wait_ready():
        return 1
    result = {"code": 4}
    window = webview.create_window(APP_NAME, server.url + "/#overview", width=1280, height=820, background_color="#0b1015")

    def watch() -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if all(path in server.seen for path in WINDOW_PROOF):
                result["code"] = 0
                logging.info("window check: the workspace loaded and its scripts ran")
                break
            time.sleep(0.25)
        else:
            missing = [p for p in WINDOW_PROOF if p not in server.seen]
            logging.error("window check timed out; never requested: %s; requested: %s", missing, sorted(server.seen)[:40])
        try:
            window.destroy()
        except Exception as error:  # noqa: BLE001 - the window may already be gone
            logging.warning("window close: %s", error)

    webview.start(watch, private_mode=True)
    return result["code"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="Loki", description="Loki trading workspace on this computer.")
    parser.add_argument("--browser", action="store_true", help="open in a browser window instead of the app window")
    parser.add_argument("--smoke", action="store_true", help="start, check the pages answer, and exit")
    parser.add_argument("--window-check", action="store_true", help="open the app window, check Loki loads in it, and exit")
    args = parser.parse_args(argv)

    base = data_dir()
    logging.basicConfig(filename=str(base / "loki.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    configure_environment(base)
    # The self-test never touches a Loki you already have open: it takes any free port.
    port, running = (_free_port(), False) if (args.smoke or args.window_check) else pick_port()
    if args.smoke or args.window_check:
        checked = LocalServer(port)
        checked.start()
        code = smoke(checked) if args.smoke else window_check(checked)
        checked.stop()
        logging.info("smoke %s", "ok" if code == 0 else f"failed ({code})")
        print("smoke", "ok" if code == 0 else f"failed ({code})")  # no console in a windowed build: print is a no-op
        return code

    server = None if running else LocalServer(port)
    url = f"http://{HOST}:{port}"
    if server is not None:
        server.start()
    start_sure(base)
    icon = bundle_root() / ("loki.ico" if sys.platform == "win32" else "static/favicon.png")
    try:
        if args.browser or not run_window(server, url, base, icon):
            if server is not None and not server.wait_ready():
                logging.error("Loki did not start; see the log above")
                return 1
            open_in_browser(url + "/#overview")
            if server is not None:
                # No window to watch: keep serving until the process is stopped (Ctrl+C).
                while server.thread.is_alive():
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.stop()
    return 0


def _quiet_console() -> None:
    """A windowed app has no console: sys.stdout/sys.stderr are None, and libraries that print or
    set up console logging then crash. Point them at the null device instead."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def run() -> None:
    """Run and then end the process outright.

    The server leaves worker threads behind (sync request handlers, market-data fetches). On
    Windows a normal interpreter exit waits on them, so a closed window could leave an invisible
    Loki running and a self-test could hang. Once Loki is done, nothing else needs to finish.
    """
    code = 1
    _quiet_console()
    threading.excepthook = lambda args: logging.error(
        "thread %s failed: %s", getattr(args.thread, "name", "?"), "".join(
            __import__("traceback").format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
    try:
        code = main()
    except BaseException:  # noqa: BLE001 - recorded in loki.log, then the process ends
        logging.basicConfig(filename=str(data_dir() / "loki.log"), level=logging.INFO)
        logging.exception("Loki stopped with an error")
    finally:
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream:
                    stream.flush()
            except Exception:  # noqa: BLE001 - no console in a windowed build
                pass
        os._exit(code)


if __name__ == "__main__":
    run()
