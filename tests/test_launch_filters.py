"""New-launch screening and exits: every filter and every exit rule, by hand."""
from loki_v8 import launch_filters as lf


def report(**over):
    base = {"mint": "MINT", "mintAuthority": None, "freezeAuthority": None, "rugged": False, "graphInsidersDetected": 0, "risks": [],
            "markets": [{"pubkey": "POOL", "marketType": "raydium", "lp": {"lpLockedPct": 100}}],
            "knownAccounts": {"POOL": {"type": "AMM"}, "LOCK": {"type": "LOCKER"}},
            "topHolders": [{"owner": "POOL", "pct": 60.0}, {"owner": "LOCK", "pct": 10.0}] + [{"owner": f"W{i}", "pct": 2.0} for i in range(12)]}
    base.update(over)
    return base


def test_a_clean_token_passes_and_pools_do_not_count_as_holders():
    assert lf.safety(report()) == []  # top 10 people own 20%: the pool's 60% and the locker's 10% are left out
    assert [h["owner"] for h in lf.holders_outside_pools(report())][:2] == ["W0", "W1"]


def test_each_safety_filter_rejects_on_its_own():
    assert "mint authority" in lf.safety(report(mintAuthority="X"))[0]
    assert "freeze authority" in lf.safety(report(freezeAuthority="X"))[0]
    assert "not locked" in lf.safety(report(markets=[{"pubkey": "POOL", "marketType": "raydium", "lp": {"lpLockedPct": 40}}]))[0]
    heavy = report(topHolders=[{"owner": "POOL", "pct": 50.0}, {"owner": "WHALE", "pct": 30.0}])
    assert "top 10 holders own 30%" in lf.safety(heavy)[0]
    assert "insider" in lf.safety(report(graphInsidersDetected=4))[0]
    assert lf.safety(report(risks=[{"name": "Creator history of rugged tokens", "level": "danger"}, {"name": "Low LP providers", "level": "warn"}])) == ["RugCheck: Creator history of rugged tokens"]
    assert "bundled" in lf.safety(report(), launch_bundle_pct=55.0)[0] and lf.safety(report(), launch_bundle_pct=12.0) == []
    assert lf.safety({}) == ["no safety report"]


def test_a_bonding_curve_counts_as_locked_liquidity():
    curve = report(markets=[{"pubkey": "POOL", "marketType": "pump_fun", "lp": {"lpLockedPct": 0}}])
    assert lf.safety(curve) == []


def test_velocity_needs_volume_over_liquidity_more_buys_and_enough_depth():
    good = {"volume": {"m5": 30_000}, "txns": {"m5": {"buys": 90, "sells": 40}}, "liquidity": {"usd": 10_000}}
    assert lf.velocity(good, None)[0] == []
    assert "volume" in " ".join(lf.velocity({**good, "volume": {"m5": 5_000}}, None)[0])
    assert "buys/sells" in " ".join(lf.velocity({**good, "txns": {"m5": {"buys": 40, "sells": 40}}}, None)[0])
    assert "liquidity" in lf.velocity({**good, "liquidity": {"usd": 1_000}}, None)[0][0]
    assert lf.velocity({"volume": {"m5": 20_000}, "txns": {"m5": {"buys": 9, "sells": 2}}, "liquidity": None}, 8_000.0)[0] == []  # curve: RugCheck's liquidity


def test_launch_bundle_counts_only_the_first_two_slots_and_not_pools():
    def tx(slot, owner, before, after, err=None):
        return {"slot": slot, "meta": {"err": err, "preTokenBalances": [{"owner": owner, "mint": "M", "uiTokenAmount": {"uiAmount": before}}],
                                       "postTokenBalances": [{"owner": owner, "mint": "M", "uiTokenAmount": {"uiAmount": after}}]}}
    txs = [tx(100, "A", 0, 300), tx(100, "POOL", 1000, 0), tx(101, "B", 0, 200), tx(101, "C", 0, 999, err="failed"), tx(105, "D", 0, 400)]
    assert lf.launch_bundle_pct(txs, "M", 1000.0, ["POOL"]) == 50.0  # A + B; the pool, the failed tx and slot 105 do not count
    assert lf.launch_bundle_pct([], "M", 1000.0, []) is None


def test_ladder_takes_profit_then_trails_the_moonbag():
    plan, pos = lf.ExitPlan(), lf.Position("M", 1.0, opened_at=0)
    assert lf.step(pos, 2.1, 60, plan) == [(0.5, "take profit at 2.0x")] and pos.remaining == 0.5
    assert lf.step(pos, 5.0, 120, plan) == [(0.25, "take profit at 4.5x")] and abs(pos.remaining - 0.25) < 1e-9
    assert lf.step(pos, 6.0, 180, plan) == []  # new peak 6x
    out = lf.step(pos, 4.1, 240, plan)  # 32% under the peak
    assert out[0][1].startswith("trailing stop") and abs(out[0][0] - 0.25) < 1e-9 and pos.remaining == 0


def test_time_stop_hard_stop_and_holder_sell_exit_everything():
    plan = lf.ExitPlan()
    slow = lf.Position("M", 1.0, opened_at=0)
    assert lf.step(slow, 1.1, 11 * 60, plan) == [] and lf.step(slow, 1.1, 12 * 60, plan)[0][1].startswith("time stop")
    rug = lf.Position("M", 1.0, opened_at=0)
    assert lf.step(rug, 0.6, 30, plan) == [(1.0, "hard stop: -40%")]
    whale = lf.Position("M", 1.0, opened_at=0, holders={"WHALE1": 1000.0})
    assert lf.step(whale, 1.3, 30, plan, {"WHALE1": 900.0}) == []
    assert lf.step(whale, 1.3, 40, plan, {"WHALE1": 400.0})[0][1].startswith("tracked holder")
    risen = lf.Position("M", 1.0, opened_at=0)
    lf.step(risen, 1.25, 60, plan)
    assert lf.step(risen, 1.15, 13 * 60, plan) == []  # it reached +20% in time: the time stop no longer applies
