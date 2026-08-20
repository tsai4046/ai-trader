"""指標對照手算值。固定小資料集，值都是手算/逐步推導出來的。"""
import numpy as np
import pandas as pd
import pytest

from core.indicators import (
    atr,
    donchian_high,
    donchian_low,
    ema,
    realized_vol,
    rsi,
    sma,
    swing_high,
    swing_low,
    volume_ma,
)


def make_df(close, high=None, low=None, volume=None):
    close = pd.Series(close, dtype=float)
    n = len(close)
    idx = pd.bdate_range("2024-01-01", periods=n)
    df = pd.DataFrame(
        {
            "open": close.values,
            "high": np.asarray(high, dtype=float) if high is not None else close.values + 1.0,
            "low": np.asarray(low, dtype=float) if low is not None else close.values - 1.0,
            "close": close.values,
            "volume": np.asarray(volume, dtype=float) if volume is not None else np.full(n, 1000.0),
        },
        index=idx,
    )
    return df


def test_sma_hand_computed():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = sma(s, 3)
    assert np.isnan(out.iloc[0]) and np.isnan(out.iloc[1])
    assert out.iloc[2] == pytest.approx(2.0)   # (1+2+3)/3
    assert out.iloc[5] == pytest.approx(5.0)   # (4+5+6)/3


def test_ema_hand_computed():
    # span=3 → alpha=0.5, adjust=False: e_t = 0.5*x + 0.5*e_{t-1}, e_0 = x_0
    s = pd.Series([2.0, 4.0, 8.0])
    out = ema(s, 3)
    assert out.iloc[0] == pytest.approx(2.0)
    assert out.iloc[1] == pytest.approx(3.0)   # 0.5*4 + 0.5*2
    assert out.iloc[2] == pytest.approx(5.5)   # 0.5*8 + 0.5*3


def test_atr_wilder_hand_computed():
    # 構造 TR 恆為 2 的資料：high=close+1, low=close-1, close 不變
    df = make_df([10.0] * 20)
    out = atr(df, 14)
    assert np.isnan(out.iloc[12])          # 未滿 n 根 → NaN
    assert out.iloc[14] == pytest.approx(2.0)
    assert out.iloc[-1] == pytest.approx(2.0)


def test_atr_wilder_recursion():
    # 手推 Wilder：atr_t = atr_{t-1} + (tr_t - atr_{t-1})/n
    closes = [10.0] * 15 + [14.0]
    highs = [c + 1 for c in closes[:-1]] + [15.0]
    lows = [c - 1 for c in closes[:-1]] + [13.0]
    df = make_df(closes, high=highs, low=lows)
    out = atr(df, 14)
    # 最後一根 TR = max(15-13, |15-10|, |13-10|) = 5
    prev = out.iloc[-2]
    expected = prev + (5.0 - prev) / 14.0
    assert out.iloc[-1] == pytest.approx(expected)


def test_rsi_all_up_is_100():
    s = pd.Series(np.arange(1.0, 31.0))   # 連漲 30 根
    out = rsi(s, 14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_rsi_alternating_is_50():
    # 漲跌交替且幅度相同 → avg_gain == avg_loss → RSI ≈ 50
    vals = [100.0]
    for i in range(40):
        vals.append(vals[-1] + (1.0 if i % 2 == 0 else -1.0))
    out = rsi(pd.Series(vals), 14)
    assert out.iloc[-1] == pytest.approx(50.0, abs=1.5)


def test_rsi_hand_computed_wilder():
    # 14 根全漲 1 之後跌 7：avg_gain = 1*13/14, avg_loss = 7/14
    s = pd.Series([float(x) for x in range(100, 115)] + [107.0])
    out = rsi(s, 14)
    avg_gain = 1.0 * 13.0 / 14.0
    avg_loss = 7.0 / 14.0
    expected = 100 - 100 / (1 + avg_gain / avg_loss)
    assert out.iloc[-1] == pytest.approx(expected, abs=1e-6)


def test_donchian_high_excludes_current_bar():
    # 當根 high 是歷史最高：若 donchian 含當根，close > donchian_high 恆為 False。
    highs = [10.0, 11.0, 12.0, 11.0, 10.0, 20.0]
    closes = [9.0, 10.0, 11.0, 10.0, 9.0, 19.0]
    df = make_df(closes, high=highs, low=[c - 1 for c in closes])
    dh = donchian_high(df, 5)
    # 最後一根：前 5 根最高 = 12，收盤 19 > 12 → 突破成立
    assert dh.iloc[-1] == pytest.approx(12.0)
    assert df["close"].iloc[-1] > dh.iloc[-1]


def test_donchian_low_excludes_current_bar():
    lows = [10.0, 9.0, 8.0, 9.0, 10.0, 2.0]
    df = make_df([l + 1 for l in lows], high=[l + 2 for l in lows], low=lows)
    dl = donchian_low(df, 5)
    assert dl.iloc[-1] == pytest.approx(8.0)


def test_volume_ma():
    df = make_df([10.0] * 5, volume=[100.0, 200.0, 300.0, 400.0, 500.0])
    out = volume_ma(df, 3)
    assert out.iloc[-1] == pytest.approx(400.0)


def test_realized_vol_zero_when_flat():
    s = pd.Series([100.0] * 30)
    out = realized_vol(s, 20)
    assert out.iloc[-1] == pytest.approx(0.0)


def test_realized_vol_hand_computed():
    # 報酬率 +1%/-1% 交替
    vals = [100.0]
    for i in range(30):
        vals.append(vals[-1] * (1.01 if i % 2 == 0 else 0.99))
    s = pd.Series(vals)
    ret = s.pct_change()
    expected = ret.rolling(20).std().iloc[-1] * np.sqrt(252)
    assert realized_vol(s, 20).iloc[-1] == pytest.approx(expected)


def test_swing_low_high_exclude_current_bar():
    lows = [10.0, 8.0, 9.0, 11.0, 1.0]
    highs = [12.0, 13.0, 14.0, 15.0, 99.0]
    df = make_df([11.0] * 5, high=highs, low=lows)
    assert swing_low(df, 4).iloc[-1] == pytest.approx(8.0)     # 不含當根的 1.0
    assert swing_high(df, 4).iloc[-1] == pytest.approx(15.0)   # 不含當根的 99.0


def test_indicators_do_not_mutate_input():
    df = make_df([10.0, 11.0, 12.0, 13.0, 14.0])
    snapshot = df.copy()
    sma(df["close"], 3); atr(df, 3); rsi(df["close"], 3)
    donchian_high(df, 3); swing_low(df, 3); volume_ma(df, 3)
    pd.testing.assert_frame_equal(df, snapshot)
