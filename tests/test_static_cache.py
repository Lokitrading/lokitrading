"""Static files: a link tagged with the current release is cached for a year; anything else revalidates."""
import re

from fastapi.testclient import TestClient

from loki.api.server import app
from loki.version import ASSET_TAG, __version__

client = TestClient(app)
JS = "/static/js/status_bar.js"


def test_asset_tag_starts_with_the_release():
    assert ASSET_TAG == __version__ or ASSET_TAG.startswith(__version__ + "-")


def test_current_tag_is_immutable():
    res = client.get(f"{JS}?v={ASSET_TAG}")
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_old_or_missing_tag_revalidates():
    for url in (f"{JS}?v=0.0.1", JS):
        res = client.get(url)
        assert res.status_code == 200
        assert res.headers["Cache-Control"] == "no-cache", url


def test_pages_link_assets_with_the_current_tag():
    html = client.get("/gate").text
    tags = set(re.findall(r'/static/[^"?]+\?v=([^"]+)"', html))
    assert tags == {ASSET_TAG}
