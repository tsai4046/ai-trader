"""detector 觸發與評分規則測試。"""
import pandas as pd
import pytest

from core.backtest import EdgeStats
from core.datasource import SyntheticProvider
from core.signals import (
    SAMPLE_CAP_SCORE,
    DETECTORS,
    resolve_signals,
    run_detector,
    score_signal,
)


@pytest.fixture(scope="module")
def syn():
    p = SyntheticProvider()
    return {s: p.fetch(s, 500, "1d").df
            for s in ["SYN_TREND", "SYN_BREAK", "SYN_PULLBACK", "SYN_FLAT"]}


def test_breakout_fires_on_syn_break(syn, cfg):
    res = run_detector(syn["SYN_BREAK"], "breakout", cfg)
    assert bool(res.fired.iloc[-1]) is True


def test_trend_continuation_fires_on_syn_trend(syn, cfg):
    res = run_detector(syn["SYN_TREND"], "trend_continuation", cfg)
    assert bool(res.fired.iloc[-1]) is True


def test_pullback_fires_on_syn_pullback(syn, cfg):
    res = run_detector(syn["SYN_PULLBACK"], "pullback", cfg)
    assert bool(res.fired.iloc[-1]) is True


def test_flat_market_fires_nothing(syn, cfg):
    for name in DETECTORS:
        res = run_detector(syn["SYN_FLAT"], name, cfg)
        assert bool(res.fired.iloc[-1]) is False, f"{name} 不該在盤整市觸發"


def test_detector_weights_sum_to_one(syn, cfg):
    for name in DETECTORS:
        res = run_detector(syn["SYN_TREND"], name, cfg)
        assert sum(res.weights.values()) == pytest.approx(1.0)
        assert set(res.weights) == set(res.conditions)


def test_fired_is_full_series(syn, cfg):
    res = run_detector(syn["SYN_BREAK"], "breakout", cfg)
    assert len(res.fired) == len(syn["SYN_BREAK"])   # 整條 Series，回測要逐根掃
    assert res.fired.dtype == bool


def _edge(n, expectancy, decayed=False, in_exp=None, out_exp=None):
    return EdgeStats(detector="breakout", symbol="X", n=n, win_rate=0.5,
                     expectancy_R=expectancy, median_bars_held=5,
                     in_sample_expectancy=in_exp if in_exp is not None else expectancy,
                     holdout_expectancy=out_exp if out_exp is not None else expectancy,
                     decayed=decayed)


def test_score_capped_at_45_when_insufficient_samples(syn, cfg):
    # 原則 2：樣本不足 → 分數上限 45，永遠到不了 APPROVE
    edge = _edge(n=cfg.signals.min_samples - 1, expectancy=0.9)   # 很棒的 edge 也一樣
    s = score_signal(syn["SYN_BREAK"], "breakout", edge, cfg, symbol="SYN_BREAK")
    assert s.score <= SAMPLE_CAP_SCORE
    assert s.capped_by_samples is True


def test_score_not_capped_when_samples_sufficient(syn, cfg):
    edge = _edge(n=cfg.signals.min_samples + 20, expectancy=0.5)
    s = score_signal(syn["SYN_BREAK"], "breakout", edge, cfg, symbol="SYN_BREAK")
    assert s.capped_by_samples is False
    assert s.score > SAMPLE_CAP_SCORE


def test_decay_penalty_applied(syn, cfg):
    n = cfg.signals.min_samples + 20
    good = _edge(n=n, expectancy=0.4)
    bad = _edge(n=n, expectancy=0.4, decayed=True, in_exp=0.5, out_exp=-0.1)
    s_good = score_signal(syn["SYN_BREAK"], "breakout", good, cfg)
    s_bad = score_signal(syn["SYN_BREAK"], "breakout", bad, cfg)
    assert s_bad.decayed is True
    assert s_bad.score == pytest.approx(
        round(s_good.score / 1 * cfg.backtest.decay_penalty), abs=1
    )


def test_conflict_momentum_vs_reversal_abstains(syn, cfg):
    n = cfg.signals.min_samples + 5
    a = score_signal(syn["SYN_BREAK"], "momentum", _edge(n, 0.3), cfg)
    b = score_signal(syn["SYN_BREAK"], "reversal", _edge(n, 0.3), cfg)
    best, conflict = resolve_signals([a, b])
    assert conflict is True and best is None


def test_resolve_picks_highest_score(syn, cfg):
    n = cfg.signals.min_samples + 5
    a = score_signal(syn["SYN_BREAK"], "breakout", _edge(n, 0.5), cfg)
    b = score_signal(syn["SYN_BREAK"], "trend_continuation", _edge(n, -0.1), cfg)
    best, conflict = resolve_signals([a, b])
    assert conflict is False and best.detector == "breakout"
