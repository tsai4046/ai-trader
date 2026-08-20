"""六道風控閘測試。風控是程式碼，不是提示詞。"""
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import core.risk as risk_mod
from core.backtest import EdgeStats
from core.datasource import Bars, SyntheticProvider
from core.plan import TradePlan
from core.risk import Portfolio, Position, assess, atr_spike_ratio, global_risk_state


def make_bars(symbol="TEST", market="us", n=300, seed=7):
    """隨機漫步（固定 seed）→ 有變異數可算相關係數，ATR ratio ≈ 1。"""
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, n)))
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=n)
    df = pd.DataFrame({
        "open": close, "high": close * 1.01, "low": close * 0.99,
        "close": close, "volume": np.full(n, 1e6),
    }, index=idx)
    return Bars(symbol=symbol, market=market, df=df, source="test",
                fetched_at=datetime.now(), stale_days=0)


def make_plan(symbol="TEST", market="us", entry_low=100.0, entry_high=101.0,
              stop=95.0, rr=None, sector="tech"):
    if rr is None:
        target = entry_low + 2.5 * (entry_low - stop)
        rr = (target - entry_high) / (entry_high - stop)
    else:
        target = entry_high + rr * (entry_high - stop)
    edge = EdgeStats("breakout", symbol, 20, 0.5, 0.3, 5, 0.3, 0.2, False)
    now = datetime.now().astimezone()
    return TradePlan(symbol=symbol, market=market, detector="breakout",
                     direction="long", entry_low=entry_low, entry_high=entry_high,
                     stop=stop, target=round(target, 4), invalidation="test",
                     invalidation_check={"type": "close_below_ma", "ma": 20, "consecutive": 2},
                     risk_reward=round(rr, 4), expected_hold_days=5, score=70,
                     edge=edge, created_at=now, expires_at=now + timedelta(days=5),
                     sector=sector)


def empty_portfolio(equity=1_000_000.0):
    return Portfolio(equity=equity)


def gate(ra, name):
    return next(g for g in ra.gates if g.name == name)


def test_low_rr_is_blocked(cfg):
    # RR = 1.2 且 min_risk_reward = 1.8 → 必被擋
    plan = make_plan(rr=1.2)
    ra = assess(plan, empty_portfolio(), make_bars(), {}, cfg)
    assert not gate(ra, "min_risk_reward").passed
    assert "min_risk_reward" in ra.blocking_reasons
    assert ra.all_passed is False


def test_total_risk_cap_blocks_new_trade(cfg):
    # 現有持倉風險總和已達 20% → 新單必被擋
    equity = 1_000_000.0
    existing = Position("r1", "AAA", "us", "tech", 100.0, 90.0, 20000,
                        risk_amount=equity * cfg.risk.max_total_risk_pct / 100.0,
                        opened_at=date.today())
    pf = Portfolio(equity=equity, positions=[existing])
    ra = assess(make_plan(), pf, make_bars(), {}, cfg)
    assert not gate(ra, "total_risk").passed
    assert "max_total_risk" in ra.blocking_reasons


def test_tw_shares_are_multiples_of_1000(cfg):
    # 台股 allow_odd_lot: false → 股數必為 1000 的倍數
    assert cfg.account.allow_odd_lot is False
    plan = make_plan(market="tw", entry_low=100.0, entry_high=105.0, stop=96.9)
    ra = assess(plan, empty_portfolio(), make_bars(market="tw"), {}, cfg)
    assert ra.shares > 0
    assert ra.shares % 1000 == 0


def test_tw_high_price_stock_rejected_not_one_share(cfg):
    # 高價股整股捨去後變 0 股 → REJECT（position_too_small），不默默放行 1 股
    plan = make_plan(market="tw", entry_low=1040.0, entry_high=1050.0, stop=1000.0)
    ra = assess(plan, empty_portfolio(), make_bars(market="tw"), {}, cfg)
    assert ra.shares == 0
    assert "position_too_small" in ra.blocking_reasons


def test_atr_spike_halves_position(cfg, monkeypatch):
    plan = make_plan()
    pf = empty_portfolio()
    ra_normal = assess(plan, pf, make_bars(), {}, cfg)
    monkeypatch.setattr(risk_mod, "atr_spike_ratio", lambda df: 2.5)
    ra_spike = assess(plan, pf, make_bars(), {}, cfg)
    assert ra_spike.all_passed  # 2.5 只砍半不擋單
    assert ra_spike.shares == int(ra_normal.shares * 0.5) or \
        ra_spike.shares == pytest.approx(ra_normal.shares * 0.5, abs=1)


def test_atr_spike_extreme_rejects(cfg, monkeypatch):
    monkeypatch.setattr(risk_mod, "atr_spike_ratio", lambda df: 3.5)
    ra = assess(make_plan(), empty_portfolio(), make_bars(), {}, cfg)
    assert "volatility_extreme" in ra.blocking_reasons
    assert not gate(ra, "volatility").passed
    assert not gate(ra, "position_size").passed


def test_syn_crash_triggers_volatility_reject(cfg):
    # 整合驗證：SYN_CRASH 的 ATR ratio 實算 > reject 門檻
    bars = SyntheticProvider(market="tw").fetch("SYN_CRASH", 500, "1d")
    ratio = atr_spike_ratio(bars.df)
    assert ratio > cfg.risk.atr_spike_reject
    plan = make_plan(symbol="SYN_CRASH", market="tw",
                     entry_low=85.0, entry_high=86.0, stop=80.0)
    ra = assess(plan, empty_portfolio(), bars, {}, cfg)
    assert "volatility_extreme" in ra.blocking_reasons


def test_drawdown_triggers_observe_mode(cfg):
    # 回撤達 max_drawdown_pct → 全域觀察模式（所有候選降為 WATCH，由 scan 執行）
    pf = Portfolio(equity=1_000_000.0,
                   current_drawdown_pct=cfg.risk.max_drawdown_pct + 2.0)
    mode, reasons = global_risk_state(pf, cfg)
    assert mode == "observe" and reasons
    ra = assess(make_plan(), pf, make_bars(), {}, cfg)
    assert not gate(ra, "drawdown").passed
    assert "global_risk_observe_mode" in ra.blocking_reasons


def test_daily_loss_triggers_observe_mode(cfg):
    equity = 1_000_000.0
    pf = Portfolio(equity=equity,
                   realized_loss_today=equity * (cfg.risk.max_daily_loss_pct + 1) / 100.0)
    mode, _ = global_risk_state(pf, cfg)
    assert mode == "observe"


def test_correlation_concentration_blocks_third_position(cfg):
    # 兩檔與候選相關係數 ≈ 1 的持倉已在手上 → 第三檔（候選）被擋
    cand_bars = make_bars("CAND", seed=42)
    pos_a_bars = make_bars("POSA", seed=42)   # 同 seed → 報酬完全相同 → corr = 1
    pos_b_bars = make_bars("POSB", seed=42)
    positions = [
        Position("r1", "POSA", "us", "other1", 100.0, 95.0, 100, 500.0, date.today()),
        Position("r2", "POSB", "us", "other2", 100.0, 95.0, 100, 500.0, date.today()),
    ]
    pf = Portfolio(equity=1_000_000.0, positions=positions)
    universe = {"POSA": pos_a_bars, "POSB": pos_b_bars}
    ra = assess(make_plan(symbol="CAND", sector="tech"), pf, cand_bars, universe, cfg)
    assert not gate(ra, "correlation").passed
    assert "correlation_concentration" in ra.blocking_reasons


def test_correlation_uses_returns_not_prices(cfg):
    # 兩檔都長期上漲但日報酬獨立 → 用價格算 corr 會接近 1（假陽性），
    # 用報酬算則不相關 → 不該被擋（陷阱 #5）
    cand = make_bars("CAND", seed=1)
    pos = make_bars("POSX", seed=2)
    price_corr = cand.df["close"].tail(60).corr(pos.df["close"].tail(60))
    ret_corr = (cand.df["close"].pct_change().tail(60)
                .corr(pos.df["close"].pct_change().tail(60)))
    # 前提確認：本組資料確實會讓「價格相關」誤判
    assert abs(ret_corr) < cfg.risk.correlation_threshold
    positions = [
        Position("r1", "POSX", "us", "otherA", 100.0, 95.0, 100, 500.0, date.today()),
        Position("r2", "POSX2", "us", "otherB", 100.0, 95.0, 100, 500.0, date.today()),
    ]
    pf = Portfolio(equity=1_000_000.0, positions=positions)
    universe = {"POSX": pos, "POSX2": make_bars("POSX2", seed=3)}
    ra = assess(make_plan(symbol="CAND", sector="tech"), pf, cand, universe, cfg)
    assert gate(ra, "correlation").passed


def test_sector_fallback_when_no_data(cfg):
    # 持倉抓不到資料 → 退化成 sector 計數
    positions = [
        Position("r1", "NODATA1", "us", "tech", 100.0, 95.0, 100, 500.0, date.today()),
        Position("r2", "NODATA2", "us", "tech", 100.0, 95.0, 100, 500.0, date.today()),
    ]
    pf = Portfolio(equity=1_000_000.0, positions=positions)
    ra = assess(make_plan(sector="tech"), pf, make_bars(), {}, cfg)
    assert not gate(ra, "correlation").passed


def test_position_size_math(cfg):
    # equity 1M、1% 風險、每股風險 7（entry_high 102 - stop 95）→ floor(10000/7)=1428
    plan = make_plan(entry_low=100.0, entry_high=102.0, stop=95.0, rr=2.0)
    ra = assess(plan, empty_portfolio(), make_bars(), {}, cfg)
    assert ra.shares == 1428
    assert ra.risk_amount == pytest.approx(1428 * 7.0)
    assert ra.risk_pct <= cfg.risk.max_risk_per_trade_pct


def test_risk_module_never_imports_llm():
    # §8 驗收：core/risk.py 全檔不 import core/llm.py（原則 3）
    src = (Path(__file__).resolve().parents[1] / "core" / "risk.py").read_text(encoding="utf-8")
    import_lines = [l.strip() for l in src.splitlines()
                    if l.strip().startswith(("import ", "from "))]
    for line in import_lines:
        assert "llm" not in line, f"risk.py 不准 import LLM 模組: {line}"
