"""資料源邏輯測試（網路以 mock 取代；synthetic 為離線骨幹）。"""
import sys
import types
from datetime import date

import numpy as np
import pandas as pd
import pytest

import core.datasource as ds
from core.datasource import (
    Bars,
    FinMindProvider,
    SyntheticProvider,
    YFinanceProvider,
    _stable_seed,
    _stale_days,
    load_universe,
)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ds, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(ds, "RETRY_BACKOFF_SECONDS", [0.0, 0.0])   # 測試不真的睡


def test_synthetic_deterministic_across_instances():
    a = SyntheticProvider().fetch("SYN_TREND", 500, "1d").df
    b = SyntheticProvider().fetch("SYN_TREND", 500, "1d").df
    pd.testing.assert_frame_equal(a, b)


def test_stable_seed_is_process_independent():
    # 不用內建 hash()（有隨機鹽）；md5 種子固定
    assert _stable_seed("SYN_TREND") == _stable_seed("SYN_TREND")
    assert 0 <= _stable_seed("2330") < 2**32
    assert _stable_seed("2330") != _stable_seed("2317")


def test_synthetic_short_has_100_bars():
    b = SyntheticProvider().fetch("SYN_SHORT", 500, "1d")
    assert len(b.df) == 100


def test_synthetic_ohlc_sane():
    df = SyntheticProvider().fetch("SYN_BREAK", 500, "1d").df
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert (df["volume"] > 0).all()
    assert not df.isna().any().any()


def test_stale_days_excludes_weekends():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-14")])   # 週五
    df = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0],
                       "close": [1.0], "volume": [1.0]}, index=idx)
    # 到下週三 8/19：跨週末，工作日差 = 3（週一二三）
    assert _stale_days(df, today=date(2026, 8, 19)) == 3


def _fake_yf_multiindex():
    idx = pd.bdate_range("2025-01-01", periods=5)
    cols = pd.MultiIndex.from_product(
        [["Open", "High", "Low", "Close", "Volume"], ["NVDA"]])
    data = np.column_stack([
        [100.0] * 5, [101.0] * 5, [99.0] * 5, [100.5] * 5, [1e6] * 5])
    return pd.DataFrame(data, index=idx, columns=cols)


def test_yfinance_flattens_multiindex(cfg, monkeypatch):
    fake = types.ModuleType("yfinance")
    fake.download = lambda *a, **k: _fake_yf_multiindex()
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    bars = YFinanceProvider(cfg).fetch("NVDA", 500, "1d")
    assert list(bars.df.columns) == ["open", "high", "low", "close", "volume"]
    assert not bars.df.isna().any().any()           # 沒攤平會全 NaN（陷阱 #2）
    assert bars.df["close"].iloc[-1] == 100.5


def test_finmind_column_mapping(cfg, monkeypatch):
    rows = [{"date": "2025-01-02", "stock_id": "2330", "open": 1000.0,
             "max": 1010.0, "min": 995.0, "close": 1005.0,
             "Trading_Volume": 25000000, "Trading_money": 1, "spread": 5,
             "Trading_turnover": 1}]

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"data": rows}

    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(ds, "FINMIND_MIN_CALL_INTERVAL", 0.0)
    bars = FinMindProvider(cfg).fetch("2330", 500, "1d")
    row = bars.df.iloc[0]
    assert row["high"] == 1010.0 and row["low"] == 995.0    # max→high, min→low
    assert row["volume"] == 25000000.0                       # Trading_Volume→volume


def test_fetch_failure_returns_empty_bars_not_raise(cfg, monkeypatch):
    fake = types.ModuleType("yfinance")
    def boom(*a, **k):
        raise ConnectionError("network down")
    fake.download = boom
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    bars = YFinanceProvider(cfg).fetch("NVDA", 500, "1d")   # 不准 raise 中斷整輪
    assert bars.empty
    assert bars.stale_days > 10**5


def test_cache_roundtrip(cfg, monkeypatch):
    calls = {"n": 0}
    fake = types.ModuleType("yfinance")
    def dl(*a, **k):
        calls["n"] += 1
        return _fake_yf_multiindex()
    fake.download = dl
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    p = YFinanceProvider(cfg)
    p.fetch("NVDA", 500, "1d")
    p.fetch("NVDA", 500, "1d")     # 第二次走快取
    assert calls["n"] == 1


def test_load_universe_offline_uses_syn_symbols(cfg):
    entries = load_universe(cfg, offline=True)
    syms = [e["symbol"] for e in entries]
    assert all(s.startswith("SYN_") for s in syms)
    assert load_universe(cfg, offline=True, market="tw")
