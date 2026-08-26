"""策略評估（多輪驗證）測試。"""
import pytest

from core.datasource import SyntheticProvider
from core.evaluate import (
    SymbolEvaluation,
    evaluate_symbol,
    evaluate_universe,
    summarize_detector,
)


def _sev(detector="x", n=20, expectancy=0.5, positive=4, active=4, robust=True):
    return SymbolEvaluation(
        detector=detector, symbol="T", market="us", n=n,
        expectancy=expectancy, win_rate=0.5, folds=[],
        positive_folds=positive, active_folds=active,
        decayed=False, robust=robust, reasons=[] if robust else ["x"],
    )


def test_evaluate_symbol_trend_on_syn_trend(cfg):
    df = SyntheticProvider().fetch("SYN_TREND", 500, "1d").df
    ev = evaluate_symbol(df, "trend_continuation", cfg, symbol="SYN_TREND")
    assert ev.n >= cfg.signals.min_samples
    assert ev.expectancy > 0
    assert len(ev.folds) == cfg.evaluate.n_folds
    assert sum(f.n for f in ev.folds) == ev.n          # 分段涵蓋全部樣本
    assert ev.robust is True and ev.reasons == []


def test_evaluate_symbol_flags_insufficient_samples(cfg):
    df = SyntheticProvider().fetch("SYN_BREAK", 500, "1d").df
    ev = evaluate_symbol(df, "breakout", cfg, symbol="SYN_BREAK")   # n=6 < 15
    assert ev.robust is False
    assert any("樣本不足" in r for r in ev.reasons)


def test_summary_requires_positive_expectancy(cfg):
    s = summarize_detector([_sev(expectancy=-0.2, robust=False)], cfg)
    assert s.recommended is False
    assert s.score <= 0


def test_summary_requires_stability(cfg):
    # 期望值正但只有一半分段為正 → 低於 0.6 門檻 → 不推薦
    s = summarize_detector([_sev(positive=2, active=4)], cfg)
    assert s.stability == pytest.approx(0.5)
    assert s.recommended is False


def test_summary_requires_at_least_one_robust_symbol(cfg):
    s = summarize_detector(
        [_sev(robust=False), _sev(robust=False)], cfg)
    assert s.recommended is False


def test_summary_recommends_when_all_gates_pass(cfg):
    s = summarize_detector([_sev(), _sev()], cfg)
    assert s.recommended is True
    assert s.score > 0


def test_evaluate_universe_offline_smoke(cfg):
    provider = SyntheticProvider()
    universe = [
        {"symbol": "SYN_TREND", "market": "us", "sector": "a", "name": "t"},
        {"symbol": "SYN_SHORT", "market": "tw", "sector": "b", "name": "s"},
    ]
    report = evaluate_universe(
        cfg, universe,
        lambda e: provider.fetch(e["symbol"], 500, "1d"),
        detectors=["trend_continuation"])
    assert report["universe"] == ["SYN_TREND", "SYN_SHORT"]
    assert any(s["symbol"] == "SYN_SHORT" for s in report["skipped"])   # 100 根 → 略過
    assert report["detectors"][0]["detector"] == "trend_continuation"
    assert "trend_continuation" in report["recommended"]
