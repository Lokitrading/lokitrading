"""The overview stays lean: no private chat room, no empty research placeholders, no duplicate cards."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def test_the_private_chat_room_is_gone():
    assert "/ai-chat" not in INDEX and "private room" not in INDEX
    assert not (ROOT / "loki" / "api" / "ai_chat.py").exists()
    assert not (ROOT / "templates" / "ai_chat.html").exists() and not (ROOT / "static" / "js" / "ai_chat.js").exists()
    assert 'href="#copilot">Talk to Loki' in INDEX  # the home page's chat link opens the workspace panel


def test_research_tiles_wait_for_research():
    for block in ('section-title metrics-heading needs-research', 'metrics needs-research', 'panel chart-panel needs-research'):
        assert block in INDEX, block
    css = (ROOT / "static" / "css" / "declutter.css").read_text(encoding="utf-8")
    assert "body.loki-workspace:not(.has-research) .needs-research { display: none; }" in css
    js = (ROOT / "static" / "js" / "workspace.js").read_text(encoding="utf-8")
    assert "function renderReport(){document.body.classList.add('has-research');" in js


def test_duplicate_cards_and_footnote_are_gone():
    assert "workflow-grid" not in INDEX and "goal-note" not in INDEX and "A decision you can trace" not in INDEX
