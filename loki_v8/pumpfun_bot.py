"""The pump.fun bot: decision-time features, exact bonding-curve replay, and its training.

A launch is a stream of trades on its bonding curve. At every trade the bot sees only what had
happened by then (``features``) and enters at the first trade that meets its rule. Its order lands
``LATENCY_S`` later, at whatever reserves the curve has by then; a buy that would fill more than
``MAX_SLIP`` above the price it decided at fails (as an order with that slippage cap would) and costs
only the transaction fee. Exits are decided on the curve's real sale value of the tokens held (after
the program's fees and the transaction cost) and land ``LATENCY_S`` later too.

Exit rules: take profit, stop loss, a trailing stop once up 20%, a time stop, and an immediate exit
when the launch's creator sells. A launch that graduates is sold at its last curve price less 1%
(pump.fun's AMM fee and slippage).

Training (``train``) uses recorded launches in time order: rules are fitted on the first half,
the best of them are compared on the next quarter, and the one chosen is reported once on the last
quarter, which played no part in choosing it.
"""
from __future__ import annotations

import collections
import gzip
import itertools
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from loki_v8 import pumpfun as pf

SIZE_SOL = 0.2              # paper stake per trade
TX_COST_SOL = 0.0005        # network + priority fee per transaction
LATENCY_S = 1.5             # decision to landing
MAX_SLIP = 0.15             # buys that would fill more than 15% above the decision price fail
GRAD_HAIRCUT = 0.01         # selling right after graduation, on pump.fun's AMM
TRAIL_ARM = 0.20            # the trailing stop arms once the position is up this much
WINDOW_S = 30.0             # the "recent" window for flow and buyer counts
SILENCE_S = 3.0             # a silence this long in the recording counts as a dropped connection
FEATURES = ("age", "prog", "buyers30", "flow30", "dev_sold", "bundle", "top_share", "chg30", "sells30", "mcap", "mayhem",
            "buyers_total", "peak_dd", "trades10", "whale30")


@dataclass
class Launch:
    mint: str
    symbol: str
    dev: str
    created_at: float
    slot: int
    mayhem: bool = False
    completed_at: Optional[float] = None
    rows: List[Tuple[float, int, int, int, str, int, int, int, int, int]] = field(default_factory=list)
    # rows: (at, slot, buy, sol, user, tokens, vsol, vtok, rtok, fee_bps)


# --- loading -------------------------------------------------------------------------------------
def load(data_dir: Path) -> Tuple[List[Launch], List[Tuple[float, float]], float]:
    """Launches (in creation order), connection gaps and the time the recording ended."""
    launches: Dict[str, Launch] = {}
    gaps: List[Tuple[float, float]] = []
    end = 0.0
    times: List[float] = []
    for path in sorted(Path(data_dir).glob("pump-*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # a file cut off mid-line by a stop
                    k = r.get("k")
                    if k == "gap":
                        gaps.append((float(r["from"]), float(r["to"])))
                        continue
                    end = max(end, float(r.get("at", 0)))
                    times.append(float(r.get("at", 0)))
                    if k == "c":
                        launches[r["m"]] = Launch(r["m"], r.get("sym", ""), r.get("dev", ""), float(r["at"]), int(r["slot"]),
                                                  bool(r.get("may", 0)))
                    elif k == "t" and r["m"] in launches:
                        launches[r["m"]].rows.append((float(r["at"]), int(r["slot"]), int(r["b"]), int(r["sol"]), r["u"],
                                                      int(r["tok"]), int(r["vs"]), int(r["vt"]), int(r["rt"]), int(r.get("fee", pf.DEFAULT_FEE_BPS))))
                    elif k == "x" and r["m"] in launches:
                        launches[r["m"]].completed_at = float(r["at"])
        except (OSError, EOFError):
            continue
    # The stream is dense (tens of rows a second), so any silence over SILENCE_S is a dropped
    # connection too, including the seconds before a reconnect was logged.
    times.sort()
    for t0, t1 in zip(times, times[1:]):
        if t1 - t0 > SILENCE_S:
            gaps.append((t0, t1))
    return sorted(launches.values(), key=lambda la: la.created_at), sorted(gaps), end


# --- features ------------------------------------------------------------------------------------
class Tracker:
    """Decision-time features of one launch, updated trade by trade (the live bot runs the same)."""

    def __init__(self, dev: str, created_at: float, created_slot: int, mayhem: bool = False) -> None:
        self.dev, self.created_at, self.created_slot, self.mayhem = dev, created_at, created_slot, mayhem
        self.recent: Deque[Tuple[float, int, int, str, float]] = collections.deque()  # at, buy, sol, user, price
        self.holdings: Dict[str, int] = collections.defaultdict(int)
        self.bought_total = 0
        self.bundle_tokens = 0
        self.dev_sold = False
        self.n_buys = self.n_sells = 0
        self.buyers_all: set = set()
        self.peak_px = 0.0

    def update(self, at: float, slot: int, buy: int, sol: int, user: str, tokens: int, vsol: int, vtok: int, rtok: int) -> Dict[str, float]:
        px = pf.price(vsol, vtok)
        if buy:
            self.n_buys += 1
            self.holdings[user] += tokens
            if user != self.dev:
                self.buyers_all.add(user)
            self.bought_total += tokens
            if slot <= self.created_slot + 1:
                self.bundle_tokens += tokens
        else:
            self.n_sells += 1
            self.holdings[user] = max(0, self.holdings[user] - tokens)
            if user == self.dev:
                self.dev_sold = True
        self.recent.append((at, buy, sol, user, px))
        while self.recent and self.recent[0][0] < at - WINDOW_S:
            self.recent.popleft()
        buyers = {u for (_, b, _, u, _) in self.recent if b and u != self.dev}
        flow = sum((s if b else -s) for (_, b, s, _, _) in self.recent) / pf.LAMPORTS
        sells = sum(1 for (_, b, _, _, _) in self.recent if not b)
        old_px = self.recent[0][4]
        self.peak_px = max(self.peak_px, px)
        buys = [s for (_, b, s, _, _) in self.recent if b]
        trades10 = sum(1 for r in self.recent if r[0] >= at - 10)
        held = sum(v for u, v in self.holdings.items() if u != self.dev)
        top = max((v for u, v in self.holdings.items() if u != self.dev), default=0)
        return {
            "age": at - self.created_at,
            "prog": pf.progress(rtok),
            "buyers30": float(len(buyers)),
            "flow30": flow,
            "dev_sold": 1.0 if self.dev_sold else 0.0,
            "bundle": self.bundle_tokens / (1_000_000_000 * pf.TOKEN_UNIT),
            "top_share": top / held if held else 1.0,
            "chg30": px / old_px - 1 if old_px else 0.0,
            "sells30": float(sells),
            "mcap": pf.market_cap_sol(vsol, vtok),
            "mayhem": 1.0 if self.mayhem else 0.0,
            "buyers_total": float(len(self.buyers_all)),
            "peak_dd": px / self.peak_px - 1 if self.peak_px else 0.0,
            "trades10": float(trades10),
            "whale30": max(buys) / sum(buys) if buys and sum(buys) > 0 else 0.0,
        }


@dataclass
class Prepared:
    """One launch as arrays: its trade path and the features at each trade."""
    launch: Launch
    at: np.ndarray
    vsol: np.ndarray
    vtok: np.ndarray
    rtok: np.ndarray
    fee: np.ndarray
    dev_sell: np.ndarray
    feats: np.ndarray  # (n_trades, len(FEATURES))


def prepare(launch: Launch) -> Optional[Prepared]:
    rows = sorted(launch.rows, key=lambda r: (r[1], r[0]))
    if not rows:
        return None
    tr = Tracker(launch.dev, launch.created_at, launch.slot, launch.mayhem)
    feats = []
    for (at, slot, buy, sol, user, tok, vs, vt, rt, _fee) in rows:
        f = tr.update(at, slot, buy, sol, user, tok, vs, vt, rt)
        feats.append([f[name] for name in FEATURES])
    return Prepared(
        launch=launch,
        at=np.array([r[0] for r in rows], dtype=float),
        vsol=np.array([r[6] for r in rows], dtype=np.int64),
        vtok=np.array([r[7] for r in rows], dtype=np.int64),
        rtok=np.array([r[8] for r in rows], dtype=np.int64),
        fee=np.array([r[9] for r in rows], dtype=np.int64),
        dev_sell=np.array([(not r[2]) and r[4] == launch.dev for r in rows], dtype=bool),
        feats=np.array(feats, dtype=float),
    )


# --- exits ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Exit:
    tp: float
    sl: float
    trail: Optional[float]
    hold_s: float


@dataclass(frozen=True)
class Costs:
    size_sol: float = SIZE_SOL
    tx_cost_sol: float = TX_COST_SOL
    latency_s: float = LATENCY_S
    max_slip: float = MAX_SLIP


def _state_at(p: Prepared, t: float) -> int:
    """Index of the last trade that landed by time ``t`` (the curve state an order landing then meets)."""
    return int(np.searchsorted(p.at, t, side="right")) - 1


def _sale_values(p: Prepared, tokens: int, costs: Costs, spend: float) -> np.ndarray:
    """Return on the stake if the position were sold at each trade's reserves (fees and tx cost paid).

    The recorded reserves do not include our own buy, so it is added back (``spend`` lamports more
    virtual SOL, same k): selling then returns our own price impact, as it would on the real curve.
    """
    vs0, vt0 = p.vsol.astype(float), p.vtok.astype(float)
    vs = vs0 + spend
    vt = vs0 * vt0 / vs
    gross = vs - (vs * vt) / (vt + tokens)
    net = gross * (1 - p.fee / 10_000) / pf.LAMPORTS - costs.tx_cost_sol
    return net / costs.size_sol - 1


def entry_fill(p: Prepared, i: int, costs: Costs) -> Optional[Tuple[int, int, float]]:
    """(state index at landing, tokens bought, landing time) for a decision at trade ``i``; None = failed."""
    t_land = p.at[i] + costs.latency_s
    j = max(_state_at(p, t_land), i)
    decided = pf.price(int(p.vsol[i]), int(p.vtok[i]))
    filled = pf.price(int(p.vsol[j]), int(p.vtok[j]))
    if decided <= 0 or filled > decided * (1 + costs.max_slip):
        return None
    if p.launch.completed_at is not None and p.launch.completed_at <= t_land:
        return None  # the curve closed before the order landed
    tokens = pf.buy_tokens(int(p.vsol[j]), int(p.vtok[j]), int(p.rtok[j]), int(costs.size_sol * pf.LAMPORTS), int(p.fee[j]))
    if tokens <= 0:
        return None
    return j, tokens, t_land


def exit_returns(p: Prepared, i: int, exits: Sequence[Exit], costs: Costs) -> np.ndarray:
    """Net return on the stake for an entry decided at trade ``i``, under each exit rule."""
    entry_cost = costs.tx_cost_sol / costs.size_sol
    fill = entry_fill(p, i, costs)
    if fill is None:
        return np.full(len(exits), -entry_cost)
    j, tokens, t_land = fill
    value = _sale_values(p, tokens, costs, costs.size_sol * pf.LAMPORTS * 10_000 / (10_000 + float(p.fee[j])))
    after = np.arange(j + 1, len(p.at))
    completed = p.launch.completed_at
    out = np.empty(len(exits))
    for e_idx, ex in enumerate(exits):
        deadline = t_land + ex.hold_s
        if completed is not None:
            deadline = min(deadline, completed)
        live = after[p.at[after] <= deadline] if len(after) else after
        trigger_at: Optional[float] = None
        if len(live):
            r = value[live]
            hit = (r >= ex.tp) | (r <= ex.sl) | p.dev_sell[live]
            if ex.trail is not None:
                peak = np.maximum.accumulate(r)
                hit |= (peak >= TRAIL_ARM) & (r <= peak - ex.trail)
            first = int(np.argmax(hit)) if hit.any() else -1
            if first >= 0:
                trigger_at = float(p.at[live[first]])
        if trigger_at is None:
            if completed is not None and completed <= t_land + ex.hold_s:
                k = max(_state_at(p, completed), j)
                out[e_idx] = (value[k] + 1) * (1 - GRAD_HAIRCUT) - 1 - entry_cost
                continue
            trigger_at = t_land + ex.hold_s
        k = max(_state_at(p, trigger_at + costs.latency_s), j)
        if completed is not None and p.at[k] >= completed - 1e-9 and completed <= trigger_at + costs.latency_s:
            out[e_idx] = (value[k] + 1) * (1 - GRAD_HAIRCUT) - 1 - entry_cost
        else:
            out[e_idx] = value[k] - entry_cost
    return out


# --- entries -------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    age_max: float
    prog_lo: float
    prog_hi: float
    buyers_min: float
    flow_min: float
    bundle_max: float
    top_max: float
    dev_must_hold: bool
    mayhem: str = "any"  # "exclude", "only" or "any": launches in pump.fun's Mayhem mode


def entry_mask(feats: np.ndarray, e: Entry) -> np.ndarray:
    f = {name: feats[:, k] for k, name in enumerate(FEATURES)}
    m = (f["age"] <= e.age_max) & (f["prog"] >= e.prog_lo) & (f["prog"] <= e.prog_hi)
    m &= (f["buyers30"] >= e.buyers_min) & (f["flow30"] >= e.flow_min)
    m &= (f["bundle"] <= e.bundle_max) & (f["top_share"] <= e.top_max)
    if e.dev_must_hold:
        m &= f["dev_sold"] < 0.5
    if e.mayhem == "exclude":
        m &= f["mayhem"] < 0.5
    elif e.mayhem == "only":
        m &= f["mayhem"] > 0.5
    return m


def decide(features: Dict[str, float], rule: Dict[str, Any]) -> bool:
    """The live entry test, the same as ``entry_mask`` (plus the learned score, if the rule has one)."""
    ok = (features["age"] <= rule["age_max"] and rule["prog_lo"] <= features["prog"] <= rule["prog_hi"]
          and features["buyers30"] >= rule["buyers_min"] and features["flow30"] >= rule["flow_min"]
          and features["bundle"] <= rule["bundle_max"] and features["top_share"] <= rule["top_max"]
          and (not rule["dev_must_hold"] or features["dev_sold"] < 0.5)
          and (rule.get("mayhem", "any") == "any" or (features["mayhem"] > 0.5) == (rule["mayhem"] == "only")))
    model = rule.get("model")
    if ok and model:
        row = np.array([[features[n] for n in FEATURES]], dtype=float)
        ok = bool(score(model, row)[0] >= model["threshold"])
    return ok


# --- the learned filter ----------------------------------------------------------------------------
def model_inputs(feats: np.ndarray) -> np.ndarray:
    """The model's inputs from the feature columns (browser: modelInputs in pumpfun_core.js)."""
    f = {name: feats[:, k] for k, name in enumerate(FEATURES)}
    return np.column_stack([
        np.minimum(f["age"], 600) / 600, f["prog"], np.log1p(f["buyers30"]),
        np.sign(f["flow30"]) * np.log1p(np.abs(f["flow30"])), f["dev_sold"], f["bundle"], f["top_share"],
        np.clip(f["chg30"], -1, 5), np.log1p(f["sells30"]), np.log(np.maximum(f["mcap"], 1e-3)), f["mayhem"],
        np.log1p(f["buyers_total"]), f["peak_dd"], np.log1p(f["trades10"]), f["whale30"],
    ])


MODEL_INPUTS = ("age", "prog", "log_buyers30", "log_flow30", "dev_sold", "bundle", "top_share", "chg30",
                "log_sells30", "log_mcap", "mayhem", "log_buyers_total", "peak_dd", "log_trades10", "whale30")


def fit_model(feats: np.ndarray, returns: np.ndarray, ridge: float = 5.0) -> Dict[str, Any]:
    """Ridge regression of each trade's return (capped at -100%..+300%) on its entry-time inputs."""
    x = model_inputs(feats)
    mean, std = x.mean(axis=0), x.std(axis=0)
    std[std < 1e-9] = 1.0
    z = (x - mean) / std
    y = np.clip(returns, -1.0, 3.0)
    a = z.T @ z + ridge * np.eye(z.shape[1])
    w = np.linalg.solve(a, z.T @ (y - y.mean()))
    return {"inputs": list(MODEL_INPUTS), "mean": mean.round(8).tolist(), "std": std.round(8).tolist(),
            "weights": w.round(8).tolist(), "bias": round(float(y.mean()), 8), "threshold": 0.0}


def score(model: Dict[str, Any], feats: np.ndarray) -> np.ndarray:
    """Predicted return of entering now, for each row of features."""
    z = (model_inputs(feats) - np.array(model["mean"])) / np.array(model["std"])
    return np.asarray(z @ np.array(model["weights"]) + model["bias"], dtype=float)


ENTRY_GRID: Dict[str, List[Any]] = {
    "age_max": [20.0, 60.0, 180.0, 600.0],
    "prog": [(0.0, 0.15), (0.05, 0.3), (0.15, 0.5), (0.3, 0.8)],
    "buyers_min": [3.0, 8.0, 15.0, 30.0],
    "flow_min": [0.0, 1.0, 3.0, 8.0],
    "bundle_max": [0.1, 1.0],
    "top_max": [0.3, 1.0],
    "dev_must_hold": [True, False],
    "mayhem": ["exclude", "only", "any"],
}
NO_CAP = 1e9  # "let it run": no take-profit
EXIT_GRID = [Exit(tp, sl, trail, hold) for tp in (0.3, 0.6, 1.0, 2.0, 5.0, NO_CAP) for sl in (-0.15, -0.3, -0.5)
             for trail in (None, 0.25) for hold in (30.0, 120.0, 600.0, 1800.0)]
SETTLE_S = max(e.hold_s for e in EXIT_GRID) + 60  # a launch is used once this much of its life is recorded


def entry_grid() -> List[Entry]:
    g = ENTRY_GRID
    return [Entry(a, p[0], p[1], b, f, bu, t, d, mh) for a, p, b, f, bu, t, d, mh in itertools.product(
        g["age_max"], g["prog"], g["buyers_min"], g["flow_min"], g["bundle_max"], g["top_max"], g["dev_must_hold"], g["mayhem"])]


# --- evaluation ----------------------------------------------------------------------------------
class Book:
    """Prepared launches with their exit outcomes cached per entry trade."""

    def __init__(self, prepared: List[Prepared], exits: Sequence[Exit], costs: Costs) -> None:
        self.prepared, self.exits, self.costs = prepared, list(exits), costs
        self.offsets = np.cumsum([0] + [len(p.at) for p in prepared])
        self.feats = np.vstack([p.feats for p in prepared]) if prepared else np.zeros((0, len(FEATURES)))
        self.owner = np.repeat(np.arange(len(prepared)), [len(p.at) for p in prepared])
        self.cache: Dict[int, np.ndarray] = {}

    def first_entries(self, e: Entry, model: Optional[Dict[str, Any]] = None) -> np.ndarray:
        """Global index of the first qualifying trade in each launch (launches with none are skipped)."""
        mask = entry_mask(self.feats, e)
        if model is not None and mask.any():
            mask[mask] = score(model, self.feats[mask]) >= model["threshold"]
        idx = np.flatnonzero(mask)
        if not len(idx):
            return idx
        _, first = np.unique(self.owner[idx], return_index=True)
        return idx[first]

    def returns(self, g: int) -> np.ndarray:
        if g not in self.cache:
            li = int(self.owner[g])
            self.cache[g] = exit_returns(self.prepared[li], g - int(self.offsets[li]), self.exits, self.costs)
        return self.cache[g]

    def matrix(self, e: Entry, model: Optional[Dict[str, Any]] = None) -> np.ndarray:
        firsts = self.first_entries(e, model)
        if not len(firsts):
            return np.zeros((0, len(self.exits)))
        return np.vstack([self.returns(int(g)) for g in firsts])


def summarize(r: Iterable[float], size_sol: float = SIZE_SOL) -> Dict[str, Any]:
    arr = np.asarray(list(r), dtype=float)
    if not len(arr):
        return {"trades": 0}
    rng = np.random.default_rng(7)
    boots = [float(rng.choice(arr, len(arr)).mean()) for _ in range(500)]
    pnl = np.cumsum(arr * size_sol)
    drawdown = float((pnl - np.maximum.accumulate(np.concatenate([[0.0], pnl]))[1:]).min())
    return {
        "trades": int(len(arr)),
        "mean_return_pct": round(float(arr.mean()) * 100, 2),
        "median_return_pct": round(float(np.median(arr)) * 100, 2),
        "win_rate_pct": round(float((arr > 0).mean()) * 100, 1),
        "total_sol": round(float(arr.sum()) * size_sol, 3),
        "worst_drawdown_sol": round(drawdown, 3),
        "mean_ci90_pct": [round(float(np.percentile(boots, 5)) * 100, 2), round(float(np.percentile(boots, 95)) * 100, 2)],
        "best_trade_pct": round(float(arr.max()) * 100, 1),
        "worst_trade_pct": round(float(arr.min()) * 100, 1),
    }


def _score(col: np.ndarray, min_trades: int) -> float:
    """Selection score: mean less one standard error, with each trade capped at +300% so that one
    lucky outlier cannot pick the rule."""
    if len(col) < min_trades:
        return -math.inf
    c = np.minimum(col, 3.0)
    return float(c.mean() - c.std(ddof=1) / math.sqrt(len(c)))


def portfolio(book: Book, e: Entry, exit_idx: int, max_open: int = 5, model: Optional[Dict[str, Any]] = None) -> List[float]:
    """Returns in time order when at most ``max_open`` positions can be held at once."""
    firsts = book.first_entries(e, model)
    trades = []
    for g in firsts:
        li = int(book.owner[g])
        p = book.prepared[li]
        start = float(p.at[int(g) - int(book.offsets[li])])
        ex = book.exits[exit_idx]
        trades.append((start, start + book.costs.latency_s * 2 + ex.hold_s, float(book.returns(int(g))[exit_idx])))
    trades.sort()
    open_until: List[float] = []
    taken = []
    for start, end, r in trades:
        open_until = [u for u in open_until if u > start]
        if len(open_until) < max_open:
            open_until.append(end)
            taken.append(r)
    return taken


def usable(launches: List[Launch], gaps: List[Tuple[float, float]], end: float, settle_s: float = SETTLE_S,
           early_s: float = 30.0) -> List[Launch]:
    """Launches with their whole tradable life recorded (``settle_s``) and no connection gap at birth.

    Every trade carries the curve's reserves, so a gap never corrupts the price path; it can only hide
    some trades. The live bot's feed reconnects too, so launches with a gap later in life are kept, as
    the live bot would see them; only launches born in a gap (or ``early_s`` before one) are left out.
    """
    out = []
    for la in launches:
        if la.created_at + settle_s > end:
            continue
        if any(g0 - early_s <= la.created_at <= g1 for g0, g1 in gaps):
            continue
        out.append(la)
    return out


def rule_dict(e: Entry, ex: Exit) -> Dict[str, Any]:
    return {"age_max": e.age_max, "prog_lo": e.prog_lo, "prog_hi": e.prog_hi, "buyers_min": e.buyers_min,
            "flow_min": e.flow_min, "bundle_max": e.bundle_max, "top_max": e.top_max, "dev_must_hold": e.dev_must_hold,
            "mayhem": e.mayhem,
            "take_profit": ex.tp, "stop_loss": ex.sl, "trail": ex.trail, "trail_arm": TRAIL_ARM, "max_hold_s": ex.hold_s}


def baseline(book: Book, wait_s: float, hold_s: float) -> List[float]:
    """Buy every launch at its first trade ``wait_s`` after creation; sell after ``hold_s``."""
    ex = Exit(tp=1e9, sl=-1e9, trail=None, hold_s=hold_s)
    out = []
    for li, p in enumerate(book.prepared):
        idx = np.flatnonzero(p.at - p.launch.created_at >= wait_s)
        if len(idx):
            out.append(float(exit_returns(p, int(idx[0]), [ex], book.costs)[0]))
    return out


def train(data_dir: Path, costs: Costs = Costs(), seed: int = 7) -> Dict[str, Any]:
    launches, gaps, end = load(data_dir)
    kept = usable(launches, gaps, end)
    prepared = [p for p in (prepare(la) for la in kept) if p is not None]
    n = len(prepared)
    a, b = int(n * 0.5), int(n * 0.75)
    parts = {"fit": prepared[:a], "check": prepared[a:b], "test": prepared[b:]}
    books = {k: Book(v, EXIT_GRID, costs) for k, v in parts.items()}

    entries = entry_grid()
    fit_min = max(30, len(parts["fit"]) // 100)
    ranked: List[Tuple[float, int, int]] = []
    for ei, e in enumerate(entries):
        m = books["fit"].matrix(e)
        if len(m) < fit_min:
            continue
        for xi in range(len(EXIT_GRID)):
            ranked.append((_score(m[:, xi], fit_min), ei, xi))
    ranked.sort(reverse=True)
    finalists = [r for r in ranked[:25] if r[0] > 0]

    check_min = max(15, len(parts["check"]) // 200)
    best: Optional[Tuple[float, int, int]] = None
    shortlist = []
    for score, ei, xi in finalists:
        col = books["check"].matrix(entries[ei])[:, xi] if len(books["check"].matrix(entries[ei])) else np.zeros(0)
        s = summarize(col, costs.size_sol)
        shortlist.append({"rule": rule_dict(entries[ei], EXIT_GRID[xi]), "fit_score_pct": round(score * 100, 2), "check": s})
        if len(col) >= check_min and col.mean() > 0 and (best is None or col.mean() > best[0]):
            best = (float(col.mean()), ei, xi)

    times = [p.launch.created_at for p in prepared]
    result: Dict[str, Any] = {
        "connector": "pump.fun program on Solana (" + pf.PROGRAM + "), read from its event logs",
        "recorded": {"launches": len(launches), "used": n, "gaps": len(gaps),
                     "from": _iso(times[0]) if times else None, "to": _iso(times[-1]) if times else None,
                     "hours": round((times[-1] - times[0]) / 3600, 2) if len(times) > 1 else 0,
                     "graduated": sum(1 for p in prepared if p.launch.completed_at is not None)},
        "split": {k: {"launches": len(v), "from": _iso(v[0].launch.created_at) if v else None} for k, v in parts.items()},
        "costs": {"stake_sol": costs.size_sol, "tx_cost_sol": costs.tx_cost_sol, "latency_s": costs.latency_s,
                  "max_slippage": costs.max_slip, "curve_fee_bps": pf.DEFAULT_FEE_BPS, "graduation_haircut": GRAD_HAIRCUT},
        "searched": {"entry_rules": len(entries), "exit_rules": len(EXIT_GRID), "finalists": len(finalists)},
        "shortlist": shortlist[:10],
        "baselines_test": {
            "buy_every_launch_at_5s_hold_60s": summarize(baseline(books["test"], 5, 60), costs.size_sol),
            "buy_every_launch_at_30s_hold_300s": summarize(baseline(books["test"], 30, 300), costs.size_sol),
        },
    }
    learned = learn(books, costs, fit_min, check_min)
    result["learned"] = {k: v for k, v in learned.items() if k not in ("entry", "exit_idx", "model")} if learned else None
    choice = "rule"
    if learned and (best is None or learned["check_mean"] > best[0]):
        choice = "learned"
    result["chosen"] = choice
    if best is None and choice != "learned":
        result["verdict"] = "no rule passed"
        result["summary"] = {"passed": False, "why": "nothing that made money on the fit launches also made money on the check launches"}
        return result
    model: Optional[Dict[str, Any]] = None
    if choice == "learned" and learned:
        e, xi, model = learned["entry"], learned["exit_idx"], learned["model"]
    else:
        assert best is not None
        _, ei, xi = best
        e = entries[ei]
    ex = EXIT_GRID[xi]
    test_col = books["test"].matrix(e, model)
    test_r = test_col[:, xi] if len(test_col) else np.zeros(0)
    test = summarize(test_r, costs.size_sol)
    port = summarize(portfolio(books["test"], e, xi, model=model), costs.size_sol)
    stress = {}
    for label, c in (("latency_3s", Costs(costs.size_sol, costs.tx_cost_sol, 3.0, costs.max_slip)),
                     ("tx_cost_0.002", Costs(costs.size_sol, 0.002, costs.latency_s, costs.max_slip)),
                     ("stake_1_sol", Costs(1.0, costs.tx_cost_sol, costs.latency_s, costs.max_slip))):
        bk = Book(parts["test"], [ex], c)
        m = bk.matrix(e, model)
        stress[label] = summarize(m[:, 0] if len(m) else [], c.size_sol)
    passed = bool(test["trades"] >= 20 and test["mean_return_pct"] > 0 and test["mean_ci90_pct"][0] > 0)
    rule = rule_dict(e, ex)
    if model is not None:
        rule["model"] = model
    result.update({
        "rule": rule,
        "fit": summarize(books["fit"].matrix(e, model)[:, xi] if len(books["fit"].matrix(e, model)) else [], costs.size_sol),
        "check": summarize(books["check"].matrix(e, model)[:, xi] if len(books["check"].matrix(e, model)) else [], costs.size_sol),
        "test": test,
        "test_max_5_open": port,
        "test_stress": stress,
        "verdict": "passed" if passed else "did not pass",
    })
    result["summary"] = {"passed": passed, "rule": result["rule"], "test": test, "stress": stress}
    return result


LEARN_BASE = Entry(300.0, 0.0, 0.8, 3.0, 0.0, 1.0, 1.0, False, "any")


def learn(books: Dict[str, "Book"], costs: Costs, fit_min: int, check_min: int, per_launch: int = 6) -> Optional[Dict[str, Any]]:
    """Learn which launches to skip: a ridge model of a trade's return from its entry-time features.

    Candidates are trades that pass a broad base rule (``LEARN_BASE``). The exit is the best one for
    that base rule on the fit launches; the model is fitted on up to ``per_launch`` candidate moments
    per fit launch; its threshold is chosen on the check launches. Returns None if nothing makes money
    on the check launches.
    """
    fit, check = books["fit"], books["check"]
    base_fit = fit.matrix(LEARN_BASE)
    if len(base_fit) < fit_min:
        return None
    exit_idx = int(np.argmax([_score(base_fit[:, k], fit_min) for k in range(base_fit.shape[1])]))
    rng = np.random.default_rng(11)
    idx = np.flatnonzero(entry_mask(fit.feats, LEARN_BASE))
    picks: List[int] = []
    for li in np.unique(fit.owner[idx]):
        own = idx[fit.owner[idx] == li]
        picks.extend(rng.choice(own, size=min(per_launch, len(own)), replace=False).tolist())
    if len(picks) < fit_min * 3:
        return None
    picks.sort()
    y = np.array([fit.returns(g)[exit_idx] for g in picks])
    model = fit_model(fit.feats[picks], y)
    cand = check.feats[entry_mask(check.feats, LEARN_BASE)]
    if not len(cand):
        return None
    scores = score(model, cand)
    best: Optional[Tuple[float, float, Dict[str, Any]]] = None
    for q in (50, 60, 70, 80, 90, 95):
        model["threshold"] = round(float(np.percentile(scores, q)), 6)
        col = check.matrix(LEARN_BASE, model)
        col = col[:, exit_idx] if len(col) else np.zeros(0)
        if len(col) >= check_min and col.mean() > 0 and (best is None or col.mean() > best[0]):
            best = (float(col.mean()), model["threshold"], summarize(col, costs.size_sol))
    if best is None:
        return None
    model["threshold"] = best[1]
    return {"entry": LEARN_BASE, "exit_idx": exit_idx, "model": model, "check_mean": best[0], "check": best[2],
            "base_rule": rule_dict(LEARN_BASE, EXIT_GRID[exit_idx]), "trained_on": len(picks)}


def _iso(t: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shuffled_check(data_dir: Path, rule: Dict[str, Any], seed: int = 7) -> float:
    """Placebo: the chosen rule's mean return when entry times are moved to random trades (for reports)."""
    launches, gaps, end = load(data_dir)
    prepared = [p for p in (prepare(la) for la in usable(launches, gaps, end)) if p is not None]
    rng = random.Random(seed)
    ex = Exit(rule["take_profit"], rule["stop_loss"], rule["trail"], rule["max_hold_s"])
    out = []
    for p in prepared:
        if len(p.at) > 2:
            out.append(float(exit_returns(p, rng.randrange(len(p.at)), [ex], Costs())[0]))
    return float(np.mean(out)) if out else 0.0


# --- live paper trading ----------------------------------------------------------------------------
def rows_from_logs(slot: int, logs: List[str], at: float) -> List[Dict[str, Any]]:
    """One transaction's pump.fun events as stream rows (the recorder's format, which the relay sends too)."""
    rows: List[Dict[str, Any]] = []
    for ev in pf.events_from_logs(logs):
        if ev["kind"] == "create" and pf.is_standard(ev):
            rows.append({"k": "c", "slot": slot, "at": round(at, 3), "m": ev["mint"], "sym": ev["symbol"], "name": ev["name"],
                         "dev": ev["creator"], "may": int(ev["mayhem"])})
        elif ev["kind"] == "trade":
            rows.append({"k": "t", "slot": slot, "at": round(at, 3), "m": ev["mint"], "b": int(ev["buy"]), "sol": ev["sol"],
                         "tok": ev["tokens"], "u": ev["user"], "vs": ev["vsol"], "vt": ev["vtok"], "rt": ev["rtok"], "fee": ev["fee_bps"]})
        elif ev["kind"] == "complete":
            rows.append({"k": "x", "slot": slot, "at": round(at, 3), "m": ev["mint"]})
    return rows


class LivePaper:
    """The trained rule on the live pump.fun stream, on paper, in stream time.

    Feed it stream rows in order (``handle_row``), or a transaction's logs (``handle``). An order
    decided at one row lands at the first row ``latency_s`` later, at that moment's reserves; exits
    likewise. ``static/js/engine/pumpfun_core.js`` (PaperBot) is the same machine, row for row.
    Nothing is signed or sent.
    """

    def __init__(self, rule: Dict[str, Any], costs: Costs = Costs(), max_open: int = 5) -> None:
        self.rule, self.costs, self.max_open = rule, costs, max_open
        self.launches: Dict[str, Dict[str, Any]] = {}
        self.positions: Dict[str, Dict[str, Any]] = {}
        self.closed: List[Dict[str, Any]] = []
        self.failed = 0
        self.seen = 0
        self.now = 0.0

    def _sale(self, state: Dict[str, Any], tokens: int, spend: float) -> float:
        vs = state["vs"] + spend
        vt = state["vs"] * state["vt"] / vs
        net = pf.sell_sol(int(round(vs)), int(round(vt)), tokens, state["fee"]) / pf.LAMPORTS - self.costs.tx_cost_sol
        return net / self.costs.size_sol - 1

    def _close(self, mint: str, reason: str, ret: float) -> None:
        pos = self.positions.pop(mint)
        net = ret - self.costs.tx_cost_sol / self.costs.size_sol
        self.closed.append({"mint": mint, "symbol": pos["symbol"], "reason": reason, "return": round(net, 6),
                            "pnl_sol": round(net * self.costs.size_sol, 6), "held_s": round(self.now - pos.get("landed", self.now), 3)})

    def _due(self) -> None:
        """Land orders whose time has come, at the reserves the curve has now."""
        at = self.now
        for mint, pos in list(self.positions.items()):
            la = self.launches.get(mint)
            if la is None or la["state"] is None:
                continue
            if pos["status"] == "pending" and at >= pos["land_at"]:
                st = la["state"]
                if la["done"] or pf.price(st["vs"], st["vt"]) > pos["decided"] * (1 + self.costs.max_slip):
                    self.positions.pop(mint)
                    self.failed += 1
                    continue
                lamports = int(round(self.costs.size_sol * pf.LAMPORTS))
                pos.update(status="open", landed=at, peak=-1.0, spend=lamports * 10_000 / (10_000 + st["fee"]),
                           tokens=pf.buy_tokens(st["vs"], st["vt"], st["rt"], lamports, st["fee"]))
            elif pos["status"] == "open":
                if pos.get("exit_at") is None and at >= pos["landed"] + self.rule["max_hold_s"]:
                    pos.update(exit_at=at + self.costs.latency_s, reason="time stop")
                if pos.get("exit_at") is not None and at >= pos["exit_at"]:
                    self._close(mint, pos["reason"], self._sale(la["state"], pos["tokens"], pos["spend"]))

    def handle_row(self, row: Dict[str, Any]) -> None:
        self.now = max(self.now, float(row.get("at", self.now)))
        k, mint = row.get("k"), str(row.get("m", ""))
        if k == "c":
            self.seen += 1
            self.launches[mint] = {"symbol": row.get("sym", ""), "created": self.now, "dev": row.get("dev"), "state": None,
                                   "done": False, "entered": False,
                                   "tracker": Tracker(row.get("dev", ""), self.now, int(row.get("slot", 0)), bool(row.get("may")))}
        elif k == "t" and mint in self.launches:
            la = self.launches[mint]
            la["state"] = row
            f = la["tracker"].update(self.now, int(row["slot"]), int(row["b"]), int(row["sol"]), row["u"], int(row["tok"]),
                                     int(row["vs"]), int(row["vt"]), int(row["rt"]))
            pos = self.positions.get(mint)
            if pos and pos["status"] == "open" and pos.get("exit_at") is None:
                r = self._sale(row, pos["tokens"], pos["spend"])
                pos["peak"] = max(pos["peak"], r)
                rule = self.rule
                reason = ("take profit" if r >= rule["take_profit"] else "stop loss" if r <= rule["stop_loss"]
                          else "creator sold" if (not row["b"] and row["u"] == la["dev"])
                          else "trailing stop" if rule["trail"] is not None and pos["peak"] >= rule.get("trail_arm", TRAIL_ARM) and r <= pos["peak"] - rule["trail"]
                          else None)
                if reason:
                    pos.update(exit_at=self.now + self.costs.latency_s, reason=reason)
            elif not pos and not la["entered"] and decide(f, self.rule):
                la["entered"] = True
                if len(self.positions) < self.max_open:
                    self.positions[mint] = {"symbol": la["symbol"], "status": "pending", "land_at": self.now + self.costs.latency_s,
                                            "decided": pf.price(int(row["vs"]), int(row["vt"]))}
        elif k == "x" and mint in self.launches:
            la = self.launches[mint]
            la["done"] = True
            pos = self.positions.get(mint)
            if pos and pos["status"] == "open" and la["state"] is not None:
                self._close(mint, "graduated", (self._sale(la["state"], pos["tokens"], pos["spend"]) + 1) * (1 - GRAD_HAIRCUT) - 1)
        self._due()
        if len(self.launches) > 2000:
            for m in [m for m, la in self.launches.items() if self.now - la["created"] > 900 and m not in self.positions]:
                del self.launches[m]

    def handle(self, slot: int, logs: List[str], at: float) -> None:
        rows = rows_from_logs(slot, logs, at)
        for row in rows:
            self.handle_row(row)
        if not rows:
            self.handle_row({"k": "tick", "at": at})

    def report(self) -> Dict[str, Any]:
        return {"launches_seen": self.seen, "failed_buys": self.failed, "open": len(self.positions),
                "closed": summarize([t["return"] for t in self.closed], self.costs.size_sol), "trades": self.closed[-50:]}
