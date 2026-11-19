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
