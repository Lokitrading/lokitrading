"""2.84: lighter pages (compression, shared reads, calmer animations) and the desktop app."""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loki.api.server import app

ROOT = Path(__file__).resolve().parents[1]
client = TestClient(app)
GZ = {"Accept-Encoding": "gzip"}


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


class TestCompression:
    def test_pages_scripts_and_json_are_compressed(self):
        assert client.get("/", headers=GZ).headers.get("content-encoding") == "gzip"
        assert client.get("/static/js/workspace.js", headers=GZ).headers.get("content-encoding") == "gzip"
        book = client.get("/api/fusion/research-book", headers=GZ)
        assert book.status_code == 200 and book.headers.get("content-encoding") == "gzip"
        assert book.json()  # still valid JSON once decoded

    def test_small_answers_are_left_alone(self):
        small = client.get("/api/auth/session", headers=GZ)
        assert len(small.content) < 1024 and "content-encoding" not in small.headers

    def test_the_live_pumpfun_stream_is_never_buffered_by_compression(self, monkeypatch):
        def refuse(*a, **k):
            raise OSError("refused")

        monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=refuse))
        reply = client.get("/api/fusion/ai/pumpfun/stream", params={"seconds": 5}, headers=GZ)
        assert reply.headers["content-type"].startswith("application/x-ndjson")
        assert "content-encoding" not in reply.headers


class TestLighterPages:
    def test_shared_reads_load_before_every_panel(self):
        html = read("templates/index.html")
        first_script = html.index("<script")
        assert html.index("/static/js/fetch_share.js") == html.index('src="', first_script) + len('src="')
        share = read("static/js/fetch_share.js")
        for path in ("/api/fusion/ai/portfolio", "/api/fusion/research-book", "/api/fusion/capabilities"):
            assert path in share
        assert "method !== 'GET'" in share and "response.clone()" in share and "!response.ok" in share

    def test_animations_run_at_30_frames_and_rest_when_idle(self):
        radar = read("static/js/loki_radar.js")
        assert "FRAME_MS = 1000 / 30" in radar and "drawBase()" in radar and "drawImage(base" in radar
        scene = read("static/js/loki_scene.js")
        assert "ts - lastTs < 31" in scene and "Math.pow(0.95, steps)" in scene
        assert "requestAnimationFrame(follow); else moving = false" in scene  # the cursor glow stops when settled
        xmap = read("static/js/engine/execmap.js")
        assert "checkShaderErrors = false" in xmap and "if (!busy() && now - last < 31) return;" in xmap
        assert "if (!running || document.hidden) { loop = 0; return; }" in xmap  # no loop while off screen


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "desktop"))
    try:
        import loki_desktop
    finally:
        sys.path.remove(str(ROOT / "desktop"))
    saved = dict(os.environ)
    monkeypatch.setenv("LOKI_APP_DATA", str(tmp_path / "Loki"))
    for key in ("LOKI_DATA_DIR", "LOKI_MEMORY_DIR", "LOKI_ANALOGUE_DIR", "LOKI_PAPER_AI_DB", "LOKI_REAL_ORDER_JOURNAL",
                "LOKI_BROKER_ENCRYPTION_KEY", "LOKI_GATE_SECRET", "LOKI_DESKTOP"):
        monkeypatch.delenv(key, raising=False)
    yield loki_desktop
    os.environ.clear()
    os.environ.update(saved)


class TestDesktopApp:
    def test_everything_the_user_saves_goes_to_their_app_data_folder(self, launcher, tmp_path, monkeypatch):
        monkeypatch.setenv("VERCEL", "1")
        base = launcher.data_dir()
        assert base == tmp_path / "Loki" and base.is_dir()
        launcher.configure_environment(base)
        for key in ("LOKI_DATA_DIR", "LOKI_MEMORY_DIR", "LOKI_ANALOGUE_DIR", "LOKI_PAPER_AI_DB", "LOKI_REAL_ORDER_JOURNAL"):
            assert Path(os.environ[key]).is_relative_to(base), key
        assert "VERCEL" not in os.environ  # the app is never the hosted site: no gate, no https-only cookies

    def test_local_secrets_are_created_once_and_kept(self, launcher):
        base = launcher.data_dir()
        launcher.configure_environment(base)
        key, cookie = os.environ["LOKI_BROKER_ENCRYPTION_KEY"], os.environ["LOKI_GATE_SECRET"]
        from cryptography.fernet import Fernet

        Fernet(key.encode())  # a valid key
        for name in ("LOKI_BROKER_ENCRYPTION_KEY", "LOKI_GATE_SECRET"):
            del os.environ[name]
        launcher.configure_environment(base)
        assert (os.environ["LOKI_BROKER_ENCRYPTION_KEY"], os.environ["LOKI_GATE_SECRET"]) == (key, cookie)

    def test_the_app_serves_loki_on_this_computer_only(self, launcher):
        port = launcher._free_port()
        server = launcher.LocalServer(port)
        assert server.url == f"http://127.0.0.1:{port}"
        server.start()
        try:
            assert server.wait_ready(60)
            assert launcher._answers(server.url + "/health")
        finally:
            server.stop()
        assert not server.thread.is_alive()

    def test_build_bundles_pages_scripts_and_model_records(self):
        build = read("desktop/build.py")
        for piece in ('("templates", "templates")', '("static", "static")', '"loki_v8"', '"--windowed"', '"--onedir"'):
            assert piece in build


class TestWindowSelfTest:
    """The window self-test is proved by the requests the page's own scripts make (no eval)."""

    def _fake_webview(self, monkeypatch, browse):
        calls = {"destroyed": False}

        class Window:
            def destroy(self):
                calls["destroyed"] = True

        def create_window(title, url, **kwargs):
            calls["url"] = url
            return Window()

        def start(func, **kwargs):
            import threading

            threading.Thread(target=browse, args=(calls["url"],), daemon=True).start()
            func()

        monkeypatch.setitem(sys.modules, "webview", types.SimpleNamespace(create_window=create_window, start=start))
        return calls

    def test_passes_once_the_workspace_scripts_have_run(self, launcher, monkeypatch):
        import urllib.request

        server = launcher.LocalServer(launcher._free_port())
        server.start()

        def browse(url):  # what the real window does: the page, then the requests its scripts make
            base = url.split("/#")[0]
            for path in ("/", "/static/js/workspace.js", "/api/status/feeds", "/api/fusion/research-book"):
                urllib.request.urlopen(base + path, timeout=30).read()

        try:
            calls = self._fake_webview(monkeypatch, browse)
            assert launcher.window_check(server, seconds=30) == 0
            assert calls["destroyed"] and calls["url"].endswith("/#overview")
        finally:
            server.stop()

    def test_fails_when_the_scripts_never_run(self, launcher, monkeypatch):
        import urllib.request

        server = launcher.LocalServer(launcher._free_port())
        server.start()
        try:
            self._fake_webview(monkeypatch, lambda url: urllib.request.urlopen(url.split("/#")[0] + "/", timeout=30).read())
            assert launcher.window_check(server, seconds=2) == 4
        finally:
            server.stop()

    def test_never_uses_evaluate_js(self):
        assert "evaluate_js(" not in read("desktop/loki_desktop.py")


def test_the_app_process_always_ends_when_loki_is_done():
    """A lingering server worker thread must never keep a closed app (or a self-test) alive."""
    src = read("desktop/loki_desktop.py")
    assert "os._exit(code)" in src and 'if __name__ == "__main__":\n    run()' in src
    flow = read(".github/workflows/desktop-app.yml")
    assert flow.count("timeout-minutes: 5") == 4  # each self-test fails fast instead of hanging


def test_a_windowed_app_with_no_console_still_starts(tmp_path):
    """Windows windowed builds have sys.stdout/sys.stderr = None; the server must still start."""
    import subprocess

    code = (
        "import sys; sys.stdout = None; sys.stderr = None; "
        f"sys.path.insert(0, {str(ROOT / 'desktop')!r}); sys.argv = ['Loki', '--smoke']; "
        "import loki_desktop; loki_desktop.run()"
    )
    env = {**os.environ, "LOKI_APP_DATA": str(tmp_path / "Loki")}
    done = subprocess.run([sys.executable, "-c", code], env=env, timeout=120)
    log = (tmp_path / "Loki" / "loki.log").read_text(encoding="utf-8")
    assert done.returncode == 0, log
    assert "smoke ok" in log
