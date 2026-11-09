"""Loki's brain: real conversations, grounded in the AI engine and real quotes."""

import pytest
from fastapi.testclient import TestClient

from loki.api import loki_brain
from loki.api.server import app
from loki_v8.config import LokiConfig

client = TestClient(app)


def fake_read(allowed=False):
    gates = [
        {"name": "signal", "passed": allowed, "detail": "score"},
        {"name": "volatility_cap", "passed": True, "detail": "ok"},
        {"name": "trend_filter", "passed": allowed, "detail": "x"},
        {"name": "regime_filter", "passed": True, "detail": "y"},
    ]
    plan = {"ready": True, "action": "BUY" if allowed else "HOLD", "allowed": allowed, "score": 41.0 if allowed else 3.0,
            "threshold": 30.0, "ann_vol": 0.2, "gates": gates, "win_probability": 0.52}
    cfg = LokiConfig(sizing_mode="vol_target", target_vol_pct=20.0, exit_mode="trend", stop_loss_atr=10.0)
    return lambda symbol: {"analysis": {"price": 512.4, "regime": "STRONG_BULL", "plan": plan}, "cfg": cfg,
                           "source": "universal_long_history"}


class TestDetection:
    @pytest.mark.parametrize("text,expected", [
        ("should I buy SPY?", ["SPY"]),
        ("what about bitcoin", ["BTC-USD"]),
        ("ETH/USD pulse", ["ETH-USD"]),
        ("tell me about apple and tesla", ["AAPL", "TSLA"]),
        ("$NVDA looks hot", ["NVDA"]),
        ("what is a stop loss?", []),
        ("I think AI is cool", []),
        ("nvda vs amd", ["NVDA", "AMD"]),
        ("compare aapl and msft", ["AAPL", "MSFT"]),
        ("which coin should I look at", []),
        ("costs an arm and a leg", []),
    ])
    def test_symbols(self, text, expected):
        assert loki_brain.detect_symbols(text) == expected


class TestIntents:
    def test_greeting_thanks_help_identity(self):
        assert loki_brain.answer("hey", "Sam")["intent"] == "greeting"
        assert "Sam" in loki_brain.answer("hello", "Sam")["text"]
        assert loki_brain.answer("thanks!")["intent"] == "thanks"
        assert "ticker" in loki_brain.answer("what can you do?")["text"]
        assert loki_brain.answer("who are you?")["intent"] == "identity"

    def test_glossary_answers_in_plain_english(self):
        reply = loki_brain.answer("what is a stop loss?")
        assert reply["intent"] == "glossary" and "exit price" in reply["text"]
        assert "Average True Range" in loki_brain.answer("explain ATR")["text"]
        assert "peak" in loki_brain.answer("what does drawdown mean")["text"]

    def test_track_record_and_how_it_works_use_the_shipped_report(self):
        record = loki_brain.answer("how did the AI do historically?")
        assert record["intent"] == "track_record" and "2023" in record["text"] and "a year" in record["text"]
        assert "Honesty check" in record["text"] and "hindsight" in record["text"]  # the unflattering test too
        how = loki_brain.answer("how does the AI learn from mistakes?")
        assert how["intent"] == "how_ai" and "200-day" in how["text"]

    def test_orders_fall_through_to_the_refusal_path(self):
        assert loki_brain.answer("buy 10 BTC now") is None
        order = client.post("/api/copilot", json={"message": "buy 10 BTC now"}).json()
        assert order["intent"] == "brain:order" and "can't place orders" in order["text"]

    def test_unknown_chatter_falls_back(self):
        assert loki_brain.answer("the weather is purple and my cat sings")["intent"] == "off_topic"
    assert loki_brain.answer("purple monkey dishwasher sings") is None


@pytest.mark.brain_data
class TestLiveReads:
    def test_symbol_read_explains_every_check(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_ai_read", fake_read(allowed=False))
        monkeypatch.setattr(loki_brain, "_universal", lambda: {})
        reply = loki_brain.answer("should I buy SPY?", "Sam")
        assert reply["intent"] == "symbol" and reply["symbols"] == ["SPY"]
        text = reply["text"]
        assert "can't tell you what to do" in text
        assert "WAIT" in text and "below its 200-day average" in text and "52%" in text
        assert "never places orders" in text

    def test_buy_setup_reports_size_and_exit_rule(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_ai_read", fake_read(allowed=True))
        text = loki_brain.answer("what about QQQ?")["text"]
        assert "BUY setup" in text and "100% of a paper account" in text and "200-day" in text

    def test_market_overview_and_unreachable_feeds(self, monkeypatch):
        rows = [{"ok": True, "symbol": "SPY", "label": "S&P 500", "price": 700.0, "change_pct": -1.2},
                {"ok": True, "symbol": "^VIX", "label": "VIX (fear gauge)", "price": 27.0, "change_pct": 9.0}]
        monkeypatch.setattr(loki_brain, "_quotes", lambda symbols: rows)
        text = loki_brain.answer("how's the market today?")["text"]
        assert "S&P 500 700.00 (-1.2%)" in text and "Fear is elevated" in text
        monkeypatch.setattr(loki_brain, "_quotes", lambda symbols: [])
        assert "aren't answering" in loki_brain.answer("how's the market today?")["text"]

    def test_both_chats_use_the_brain(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_ai_read", fake_read(allowed=False))
        copilot = client.post("/api/copilot", json={"message": "what does the AI think of NVDA?"}).json()
        assert copilot["intent"] == "brain:symbol" and "NVDA" in copilot["text"]
        room = TestClient(app)
        room.post("/api/ai/chat/access", json={"name": "Sam", "code": ""})
        reply = room.post("/api/ai/chat/message", json={"text": "what is a stop loss?"}).json()
        assert "exit price" in reply["text"]


def test_portfolio_question_uses_the_shipped_record():
    reply = loki_brain.answer("what is the portfolio AI?")
    assert reply["intent"] == "portfolio" and "21 markets" in reply["text"] and "a year" in reply["text"]


@pytest.mark.brain_data
def test_portfolio_answer_lists_current_holdings(monkeypatch):
    monkeypatch.setattr(loki_brain, "_portfolio", lambda: {"as_of": "2026-11-06", "cash": 0.6, "positions": [
        {"symbol": "SPY", "weight": 0.07}, {"symbol": "BTC-USD", "weight": 0.0}]})
    text = loki_brain.answer("what is the AI holding right now?")["text"]
    assert "SPY 7.0%" in text and "60% in cash" in text


def test_workspace_routes_portfolio_ai_questions_to_the_brain():
    reply = client.post("/api/copilot", json={"message": "What is the Portfolio AI holding?"}).json()
    assert reply["intent"] == "brain:portfolio"
    mine = client.post("/api/copilot", json={"message": "how is my portfolio doing?"}).json()
    assert not mine["intent"].startswith("brain:")


@pytest.mark.brain_data
def test_portfolio_answer_reports_health_moves_and_uncertainty(monkeypatch):
    monkeypatch.setattr(loki_brain, "_portfolio", lambda: {
        "as_of": "2026-11-06", "cash": 0.5, "positions": [{"symbol": "SPY", "weight": 0.1}],
        "health": {"status": "normal", "last_year_sharpe": 0.41, "percentile_vs_history": 29.2,
                   "detail": "behaving within its normal historical range"},
        "changes": [{"symbol": "AMZN", "action": "entered", "date": "2026-11-06"}]})
    reply = loki_brain.answer("how is the AI doing this week?")
    assert reply["intent"] == "portfolio"
    text = reply["text"]
    assert "better than 29%" in text and "entered AMZN (2026-11-06)" in text and "by luck alone" in text


def test_questions_about_the_ai_reach_the_brain_even_with_personal_words():
    assert client.post("/api/copilot", json={"message": "how is the AI doing this week?"}).json()["intent"] == "brain:portfolio"
    assert not client.post("/api/copilot", json={"message": "how am I doing?"}).json()["intent"].startswith("brain:")


class TestEverydayQuestions:
    def test_who_made_you_names_the_operator(self):
        reply = loki_brain.answer("who made you?")
        assert reply["intent"] == "identity" and "built and is run by" in reply["text"]

    def test_index_questions_are_explained(self):
        reply = loki_brain.answer("what is the s&p 500")
        assert reply["intent"] == "glossary" and "500 of the largest" in reply["text"]

    def test_timing_question_gets_an_honest_answer_not_a_prediction(self):
        reply = loki_brain.answer("is now a good time to invest?")
        assert reply["intent"] == "timing"
        assert "Nobody can reliably time the market" in reply["text"]
        assert loki_brain.FOOTER in reply["text"]

    def test_comparison_reads_both_and_summarises(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_ai_read", fake_read(allowed=True))
        monkeypatch.setattr(loki_brain, "_symbol_record", lambda symbol: None)
        reply = loki_brain.answer("nvda vs amd")
        assert reply["intent"] == "symbol" and reply["symbols"] == ["NVDA", "AMD"]
        assert reply["text"].startswith("Side by side: NVDA — Buy · AMD — Buy")

    def test_regime_and_momentum_disagreement_is_explained(self, monkeypatch):
        read = fake_read(allowed=False)

        def falling(symbol):
            data = read(symbol)
            data["analysis"]["plan"] = dict(data["analysis"]["plan"], score=-20.0, action="SELL")
            return data

        monkeypatch.setattr(loki_brain, "_ai_read", falling)
        monkeypatch.setattr(loki_brain, "_symbol_record", lambda symbol: None)
        text = loki_brain.answer("what about SPY?")["text"]
        assert "regime is still up, but short-term momentum has turned down" in text


class TestEvolution:
    def test_self_improvement_questions_use_the_evolution_log(self, monkeypatch):
        log = {"latest": {"generation": 3, "date": "2026-11-08", "equity": {"promoted": True, "challengers_tested": 15,
                                                                           "mistakes": {"kept": True, "lessons": 686}},
                          "crypto": {"promoted": False, "challengers_tested": 16, "mistakes": {"kept": False}}},
               "promotions": 1}
        monkeypatch.setattr(loki_brain, "_evolution", lambda: log)
        for question in ("is the AI self-improving?", "how has the AI evolved", "does loki get better over time?"):
            reply = loki_brain.answer(question)
            assert reply["intent"] == "evolution", question
        text = loki_brain.answer("is the AI self-improving?")["text"]
        assert "a challenger was promoted after 15 challengers" in text
        assert "686 closed trades" in text and "generation 3" in text

    def test_no_log_yet_is_said_plainly(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_evolution", lambda: {})
        assert "hasn't been recorded" in loki_brain.answer("how does the ai evolve?")["text"]


class TestEverydayBattery:
    """Real visitor phrasings (from a 127-question battery) each get the right kind of answer."""

    @pytest.mark.parametrize("message,intent", [
        ("how are you", "smalltalk"), ("what's up", "smalltalk"), ("lol", "laugh"), ("bye", "bye"),
        ("tell me a joke", "joke"), ("what's the weather", "off_topic"),
        ("i'm nervous about the market", "feelings"), ("i lost money today", "feelings"),
        ("what should i buy", "tips"), ("i have 1000 dollars what should i buy", "tips"), ("give me a stock tip", "tips"),
        ("how much should i invest", "tips"), ("can i trust the ai", "trust"), ("how risky is the ai", "trust"),
        ("is my data safe", "privacy"), ("how do i delete my data", "privacy"),
        ("is this financial advice", "advice_disclaimer"), ("how do i start", "getting_started"),
        ("how do i use the paper trader", "getting_started"), ("what's a good strategy for beginners", "getting_started"),
        ("can you connect to my broker", "broker"), ("what is the game", "game"),
        ("what is a dividend", "glossary"), ("what is dollar cost averaging", "glossary"), ("what is market cap", "glossary"),
        ("what is pe ratio", "glossary"), ("what does hodl mean", "glossary"), ("what is a limit order", "glossary"),
        ("what does stand aside mean", "glossary"), ("what does the trend score mean", "glossary"),
        ("what is the entry bar", "glossary"), ("what is diversification", "glossary"), ("what is the dow", "glossary"),
        ("how much money does the ai make", "track_record"),
    ])
    def test_intents(self, message, intent):
        reply = loki_brain.answer(message, "Josh")
        assert reply and reply["intent"] == intent, (message, reply and reply["intent"])

    def test_typos_are_read_and_said_out_loud(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_quotes", lambda symbols: [{"ok": True, "symbol": symbols[0][0], "price": 75000.0, "change_pct": -0.3}])
        reply = loki_brain.answer("bitcon price")
        assert reply["intent"] == "price" and reply["symbols"] == ["BTC-USD"]
        assert reply["text"].startswith("(I read 'bitcon' as Bitcoin.)") and "75,000.00" in reply["text"]
        assert loki_brain.fuzzy_symbols("which coin is better than the weather") == []

    def test_price_questions_get_a_quick_quote_not_an_essay(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_quotes", lambda symbols: [{"ok": True, "symbol": "ETH-USD", "price": 2231.5, "change_pct": 1.2}])
        text = loki_brain.answer("price of ethereum")["text"]
        assert text.startswith("Ethereum is at 2,231.50, +1.2% today")

    def test_movers_come_from_the_live_board(self, monkeypatch):
        board = {"movers": {"gainers": [{"symbol": "AMD", "label": "AMD", "change_pct": 11.0}],
                            "losers": [{"symbol": "ADAUSDT", "label": "Cardano", "change_pct": -2.6}]}}
        monkeypatch.setattr(loki_brain, "_board", lambda: board)
        text = loki_brain.answer("what are the biggest gainers today")["text"]
        assert "Leading: AMD +11.0%" in text and "Lagging: Cardano -2.6%" in text

    def test_follow_up_uses_the_last_ticker(self, monkeypatch):
        monkeypatch.setattr(loki_brain, "_ai_read", fake_read(allowed=True))
        monkeypatch.setattr(loki_brain, "_symbol_record", lambda symbol: None)
        reply = loki_brain.answer("what about its risk?", last_symbol="NVDA")
        assert reply["intent"] == "symbol" and reply["symbols"] == ["NVDA"]
        assert loki_brain.answer("what about its risk?", last_symbol="<script>") is None or \
            loki_brain.answer("what about its risk?", last_symbol="<script>")["intent"] != "symbol"

    def test_orders_get_a_friendly_refusal_only_when_asked(self):
        assert loki_brain.answer("buy 10 shares of apple") is None  # the private room keeps its own refusal
        reply = loki_brain.answer("buy 10 shares of apple", "Josh", order_reply=True)
        assert reply["intent"] == "order" and "can't place orders" in reply["text"]

    def test_copilot_route_never_returns_a_blank_snapshot(self):
        for message in ("asdfgh", "?", "can you connect to my broker", "is my data safe", "i lost money today"):
            data = client.post("/api/copilot", json={"message": message, "context": {"visitor_name": "Josh"}}).json()
            assert not data["text"].startswith("Snapshot: unknown data"), message
        greet = client.post("/api/copilot", json={"message": "hi", "context": {"visitor_name": "Josh"}}).json()
        assert "Josh" in greet["text"]
        hostile = client.post("/api/copilot", json={"message": "hi", "context": {"visitor_name": "<img src=x onerror=alert(1)>"}}).json()
        assert "<img" not in hostile["text"]
