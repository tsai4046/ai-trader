"""交易計畫（含失效條件）。

失效（invalidation）與停損是兩回事：停損是價格觸發的出場點；
失效是「當初的邏輯前提不成立了」，通常早於停損發生，必須分開呈現與檢查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pandas as pd

from core.backtest import EdgeStats
from core.indicators import atr, sma, swing_low, donchian_high
from core.signals import (
    ATR_N,
    DONCHIAN_N,
    SMA_FAST,
    SMA_MID,
    SMA_SLOW,
    ScoredSignal,
)

# 進場區間的 ATR 係數（屬於型態定義）
ENTRY_BAND = {
    "breakout": (0.0, 0.3),             # [close, close + 0.3*ATR]
    "pullback": (-0.2, 0.3),            # [sma20 - 0.2*ATR, sma20 + 0.3*ATR]
    "trend_continuation": (-0.2, 0.3),
    "momentum": (0.0, 0.5),
    "reversal": (0.0, 0.3),             # 規格未定義，比照 breakout（記於 NOTES.md）
}
STOP_SWING_LOOKBACK = 20
STOP_SWING_ATR_PAD = 0.5
MAX_STOP_DISTANCE_FRAC = 0.12           # 停損距離不得超過 entry 的 12%


@dataclass
class TradePlan:
    symbol: str
    market: str
    detector: str
    direction: str                      # v1 只有 'long'
    entry_low: float
    entry_high: float
    stop: float
    target: float
    invalidation: str                   # 人類可讀的失效條件描述
    invalidation_check: dict            # 機器可檢查的結構化條件
    risk_reward: float
    expected_hold_days: int
    score: int
    edge: EdgeStats
    created_at: datetime
    expires_at: datetime
    sector: str = "unknown"


def _invalidation_for(detector: str, df: pd.DataFrame) -> tuple[str, dict]:
    close = df["close"]
    if detector == "breakout":
        level = float(donchian_high(df, DONCHIAN_N).iloc[-1])   # 突破當時的箱體上緣
        return (
            f"連續 2 根收盤跌回突破前箱體上緣 {level:.2f} 以下",
            {"type": "close_below", "level": level, "consecutive": 2},
        )
    if detector == "pullback":
        level = float(swing_low(df, STOP_SWING_LOOKBACK).iloc[-1])
        return (
            f"收盤跌破回檔起算的 swing low {level:.2f}，或 SMA50 跌破 SMA200",
            {"type": "any", "conds": [
                {"type": "close_below", "level": level, "consecutive": 1},
                {"type": "ma_cross_down", "fast": SMA_MID, "slow": SMA_SLOW},
            ]},
        )
    if detector == "momentum":
        return (
            "連續 3 根收盤低於 SMA20",
            {"type": "close_below_ma", "ma": SMA_FAST, "consecutive": 3},
        )
    if detector == "trend_continuation":
        return (
            "SMA20 跌破 SMA50",
            {"type": "ma_cross_down", "fast": SMA_FAST, "slow": SMA_MID},
        )
    # reversal：規格未定義 → 最保守：連續 2 根收盤跌回 SMA20 之下（記於 NOTES.md）
    return (
        "連續 2 根收盤跌回 SMA20 之下",
        {"type": "close_below_ma", "ma": SMA_FAST, "consecutive": 2},
    )


def build_plan(scored: ScoredSignal, df: pd.DataFrame, cfg,
               sector: str = "unknown",
               now: datetime | None = None) -> tuple[TradePlan | None, str | None]:
    """回傳 (plan, None) 或 (None, reject_reason)。"""
    now = now or datetime.now().astimezone()
    close = float(df["close"].iloc[-1])
    a = float(atr(df, ATR_N).iloc[-1])
    if not (a > 0) or pd.isna(a):
        return None, "atr_unavailable"

    lo_mult, hi_mult = ENTRY_BAND[scored.detector]
    if scored.detector in ("pullback", "trend_continuation"):
        anchor = float(sma(df["close"], SMA_FAST).iloc[-1])
    else:
        anchor = close
    entry_low = anchor + lo_mult * a
    entry_high = anchor + hi_mult * a
    if entry_low <= 0 or entry_high <= entry_low:
        return None, "invalid_entry_band"

    sl = swing_low(df, STOP_SWING_LOOKBACK).iloc[-1]
    swing_stop = float(sl) - STOP_SWING_ATR_PAD * a if pd.notna(sl) else float("inf")
    atr_stop = entry_low - cfg.backtest.atr_stop_mult * a
    stop = min(swing_stop, atr_stop)
    if stop >= entry_low:
        return None, "invalid_geometry"
    # 停損距離上限：用最差進場價（entry_high）衡量，超過 12% → REJECT
    if (entry_high - stop) > entry_high * MAX_STOP_DISTANCE_FRAC:
        return None, "stop_too_wide"

    target = entry_low + cfg.plan.target_rr * (entry_low - stop)
    # RR 一律用最差進場價算（陷阱 #4）
    risk_reward = (target - entry_high) / (entry_high - stop)
    if risk_reward <= 0:
        return None, "invalid_geometry"

    invalidation, inv_check = _invalidation_for(scored.detector, df)
    plan = TradePlan(
        symbol=scored.symbol, market=scored.market, detector=scored.detector,
        direction="long",
        entry_low=round(entry_low, 4), entry_high=round(entry_high, 4),
        stop=round(stop, 4), target=round(target, 4),
        invalidation=invalidation, invalidation_check=inv_check,
        risk_reward=round(float(risk_reward), 4),
        expected_hold_days=int(scored.edge.median_bars_held),
        score=scored.score, edge=scored.edge,
        created_at=now, expires_at=now + timedelta(days=cfg.plan.ttl_days),
        sector=sector,
    )
    return plan, None


# --- 失效檢查（持續監控的核心）---------------------------------------------------


def _consecutive_below(flags: pd.Series, consecutive: int) -> bool:
    if len(flags) < consecutive:
        return False
    return bool(flags.rolling(consecutive).sum().max() >= consecutive)


def _check_condition(cond: dict, df_after: pd.DataFrame, full_df: pd.DataFrame,
                     created_at) -> tuple[bool, str]:
    ctype = cond["type"]
    if ctype == "close_below":
        flags = df_after["close"] < cond["level"]
        hit = _consecutive_below(flags, int(cond["consecutive"]))
        return hit, f"連續 {cond['consecutive']} 根收盤 < {cond['level']:.2f}"
    if ctype == "close_below_ma":
        ma = sma(full_df["close"], int(cond["ma"]))
        flags = (full_df["close"] < ma).loc[df_after.index]
        hit = _consecutive_below(flags, int(cond["consecutive"]))
        return hit, f"連續 {cond['consecutive']} 根收盤 < SMA{cond['ma']}"
    if ctype == "ma_cross_down":
        fast = sma(full_df["close"], int(cond["fast"]))
        slow = sma(full_df["close"], int(cond["slow"]))
        crossed = (fast < slow).loc[df_after.index]
        hit = bool(crossed.fillna(False).any())
        return hit, f"SMA{cond['fast']} 跌破 SMA{cond['slow']}"
    if ctype == "any":
        for c in cond["conds"]:
            hit, desc = _check_condition(c, df_after, full_df, created_at)
            if hit:
                return True, desc
        return False, "任一子條件"
    raise ValueError(f"未知的 invalidation type: {ctype}")


def check_invalidation(plan: TradePlan, df: pd.DataFrame) -> tuple[bool, str]:
    """回傳 (是否已失效, 人類可讀原因)。只用 plan.created_at 之後的 K 棒判定。"""
    created = pd.Timestamp(plan.created_at).tz_localize(None).normalize()
    df_after = df[df.index > created]
    if df_after.empty:
        return False, "計畫建立後尚無新 K 棒"
    hit, desc = _check_condition(plan.invalidation_check, df_after, df, created)
    if hit:
        return True, f"失效條件成立：{desc}"
    return False, "失效條件未成立"


def is_expired(expires_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now().astimezone()
    if expires_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    return now > expires_at
