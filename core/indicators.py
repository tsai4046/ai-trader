"""技術指標。純函式：輸入 DataFrame/Series，輸出 Series，不改動輸入。

前視偏差警告：donchian_high / donchian_low 一律「不含當根」（.shift(1)），
swing_high / swing_low 同理。改動前先看 tests/test_indicators.py。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's smoothing：ewm(alpha=1/n)，前 n 根不足時輸出 NaN。"""
    tr = _true_range(df)
    out = tr.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return out


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's smoothing RSI。"""
    delta = s.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    out = 100.0 - 100.0 / (1.0 + rs)
    # avg_loss == 0（全漲）時 rs = inf → RSI = 100
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(~(avg_gain.isna() | avg_loss.isna()))
    return out


def donchian_high(df: pd.DataFrame, n: int) -> pd.Series:
    """不含當根的前 n 根最高價。含當根的話突破條件會失真，不准拿掉 shift。"""
    return df["high"].rolling(n).max().shift(1)


def donchian_low(df: pd.DataFrame, n: int) -> pd.Series:
    return df["low"].rolling(n).min().shift(1)


def volume_ma(df: pd.DataFrame, n: int) -> pd.Series:
    return df["volume"].rolling(n).mean()


def realized_vol(s: pd.Series, n: int) -> pd.Series:
    """n 日日報酬標準差，年化。"""
    ret = s.pct_change()
    return ret.rolling(n).std() * np.sqrt(TRADING_DAYS_PER_YEAR)


def dmi(df: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder DMI：回傳 (+DI, -DI, ADX)，全部 Wilder smoothing（ewm alpha=1/n）。"""
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = ((up > dn) & (up > 0)) * up
    minus_dm = ((dn > up) & (dn > 0)) * dn
    tr = _true_range(df)

    def w(s: pd.Series) -> pd.Series:
        return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()

    tr_s = w(tr)
    plus_di = 100.0 * w(plus_dm) / tr_s
    minus_di = 100.0 * w(minus_dm) / tr_s
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx_s = w(dx)
    return plus_di, minus_di, adx_s


def bollinger(s: pd.Series, n: int = 20, mult: float = 2.0
              ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """回傳 (下軌, 中軌, 上軌)。std 用母體標準差（ddof=0，同多數平台實作）。"""
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    return mid - mult * sd, mid, mid + mult * sd


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 1.5
            ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Keltner 通道：EMA(n) ± mult * ATR(n)。回傳 (下軌, 中軌, 上軌)。"""
    mid = ema(df["close"], n)
    a = atr(df, n)
    return mid - mult * a, mid, mid + mult * a


def swing_low(df: pd.DataFrame, lookback: int) -> pd.Series:
    """不含當根的前 lookback 根最低 low。"""
    return df["low"].rolling(lookback).min().shift(1)


def swing_high(df: pd.DataFrame, lookback: int) -> pd.Series:
    """不含當根的前 lookback 根最高 high。"""
    return df["high"].rolling(lookback).max().shift(1)
