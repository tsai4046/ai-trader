"""五個 detector + 評分。

原則 1：型態判定全部是確定性 pandas 條件，不經過 LLM。
detector 回傳整條 Series（回測要逐根掃），不是只算最後一根。
指標標準週期（14/20/50/200/250/60/5）屬於 detector 定義的一部分，集中在下方常數區；
可調的門檻（min_samples、分數線、權重…）一律來自 config。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from core.indicators import atr, donchian_high, rsi, sma, swing_high, volume_ma

if TYPE_CHECKING:
    from core.backtest import EdgeStats

# --- detector 定義常數（標準週期與型態係數，屬於型態定義本身）---------------------
ATR_N = 14
RSI_N = 14
SMA_FAST, SMA_MID, SMA_SLOW = 20, 50, 200
DONCHIAN_N = 20
VOL_MA_N = 20
VOL_MA_SHORT = 5
BREAKOUT_VOL_MULT = 1.5
PULLBACK_ATR_BAND = 0.5
PULLBACK_RSI_LO, PULLBACK_RSI_HI = 35.0, 55.0
PULLBACK_SWING_N = 60
PULLBACK_PRIOR_HIGH_RATIO = 0.98
MOMENTUM_RET_N = 20
MOMENTUM_RANK_WINDOW = 250
MOMENTUM_RANK_Q = 0.8
MOMENTUM_VOL_SHRINK_RATIO = 0.8
VOL_REGIME_WINDOW = 60
GAP_ATR_MULT = 3.0
GAP_LOOKBACK = 5
REVERSAL_RSI_LEVEL = 30.0

# 評分尺度：expectancy -0.2R 對應 0 分、+0.6R 對應 100 分
EDGE_SCORE_FLOOR_R = -0.2
EDGE_SCORE_SPAN_R = 0.8
SAMPLE_CAP_SCORE = 45


@dataclass
class DetectorResult:
    fired: pd.Series                     # bool Series, index 同 df
    conditions: dict[str, pd.Series]     # 每個子條件的 bool Series
    weights: dict[str, float]            # 子條件權重，總和 = 1.0


def _b(s: pd.Series) -> pd.Series:
    """NaN 視為條件不成立（不准 fillna 硬算指標，但比較結果的 NaN 就是「未成立」）。"""
    return s.fillna(False).astype(bool)


def detect_breakout(df: pd.DataFrame, cfg) -> DetectorResult:
    close, volume = df["close"], df["volume"]
    conds = {
        "break_20d_high": _b(close > donchian_high(df, DONCHIAN_N)),
        "volume_expansion": _b(volume > BREAKOUT_VOL_MULT * volume_ma(df, VOL_MA_N)),
        "mid_term_bull": _b(close > sma(close, SMA_MID)),
        "long_term_bull": _b(sma(close, SMA_MID) > sma(close, SMA_SLOW)),
    }
    weights = {"break_20d_high": 0.35, "volume_expansion": 0.25,
               "mid_term_bull": 0.20, "long_term_bull": 0.20}
    fired = conds["break_20d_high"] & conds["volume_expansion"]
    return DetectorResult(fired=fired, conditions=conds, weights=weights)


def detect_pullback(df: pd.DataFrame, cfg) -> DetectorResult:
    close = df["close"]
    s20, s200 = sma(close, SMA_FAST), sma(close, SMA_SLOW)
    s50 = sma(close, SMA_MID)
    r = rsi(close, RSI_N)
    conds = {
        "bull_alignment": _b((s50 > s200) & (close > s200)),
        "near_sma20": _b((close - s20).abs() < PULLBACK_ATR_BAND * atr(df, ATR_N)),
        "rsi_healthy": _b((r >= PULLBACK_RSI_LO) & (r <= PULLBACK_RSI_HI)),
        "prior_high_exists": _b(close < swing_high(df, PULLBACK_SWING_N) * PULLBACK_PRIOR_HIGH_RATIO),
    }
    weights = {"bull_alignment": 0.30, "near_sma20": 0.30,
               "rsi_healthy": 0.20, "prior_high_exists": 0.20}
    fired = conds["bull_alignment"] & conds["near_sma20"]
    return DetectorResult(fired=fired, conditions=conds, weights=weights)


def detect_momentum(df: pd.DataFrame, cfg) -> DetectorResult:
    close, volume = df["close"], df["volume"]
    ret20 = close.pct_change(MOMENTUM_RET_N)
    a = atr(df, ATR_N)
    conds = {
        "momentum_top_quintile": _b(
            ret20 > ret20.rolling(MOMENTUM_RANK_WINDOW).quantile(MOMENTUM_RANK_Q)
        ),
        "volatility_expansion": _b(a > a.rolling(VOL_REGIME_WINDOW).median()),
        "above_sma20": _b(close > sma(close, SMA_FAST)),
        "volume_not_shrinking": _b(
            volume_ma(df, VOL_MA_SHORT) > volume_ma(df, VOL_MA_N) * MOMENTUM_VOL_SHRINK_RATIO
        ),
    }
    weights = {"momentum_top_quintile": 0.40, "volatility_expansion": 0.25,
               "above_sma20": 0.20, "volume_not_shrinking": 0.15}
    fired = conds["momentum_top_quintile"].copy()
    return DetectorResult(fired=fired, conditions=conds, weights=weights)


def detect_trend_continuation(df: pd.DataFrame, cfg) -> DetectorResult:
    close = df["close"]
    s20, s50, s200 = sma(close, SMA_FAST), sma(close, SMA_MID), sma(close, SMA_SLOW)
    a = atr(df, ATR_N)
    atr_pct = a / close
    gap = close.diff().abs() > GAP_ATR_MULT * a
    no_gap_recent = ~_b(gap.rolling(GAP_LOOKBACK).max())
    conds = {
        "full_bull_alignment": _b((s20 > s50) & (s50 > s200)),
        "above_sma20": _b(close > s20),
        "volatility_contraction": _b(atr_pct < atr_pct.rolling(VOL_REGIME_WINDOW).median()),
        "no_gap_risk": _b(no_gap_recent),
    }
    weights = {"full_bull_alignment": 0.35, "above_sma20": 0.25,
               "volatility_contraction": 0.25, "no_gap_risk": 0.15}
    fired = conds["full_bull_alignment"] & conds["above_sma20"]
    return DetectorResult(fired=fired, conditions=conds, weights=weights)


def detect_reversal(df: pd.DataFrame, cfg) -> DetectorResult:
    """預設關閉。RSI 由 <30 回升穿越 30，收盤收復 sma20，且 close < sma200。"""
    close = df["close"]
    r = rsi(close, RSI_N)
    conds = {
        "rsi_cross_up_30": _b((r.shift(1) < REVERSAL_RSI_LEVEL) & (r >= REVERSAL_RSI_LEVEL)),
        "reclaim_sma20": _b(close > sma(close, SMA_FAST)),
        "below_sma200": _b(close < sma(close, SMA_SLOW)),
    }
    weights = {"rsi_cross_up_30": 0.40, "reclaim_sma20": 0.30, "below_sma200": 0.30}
    fired = conds["rsi_cross_up_30"] & conds["reclaim_sma20"] & conds["below_sma200"]
    return DetectorResult(fired=fired, conditions=conds, weights=weights)


DETECTORS = {
    "breakout": detect_breakout,
    "pullback": detect_pullback,
    "momentum": detect_momentum,
    "trend_continuation": detect_trend_continuation,
    "reversal": detect_reversal,
}


def run_detector(df: pd.DataFrame, name: str, cfg) -> DetectorResult:
    return DETECTORS[name](df, cfg)


# --- 評分 ---------------------------------------------------------------------


@dataclass
class ScoredSignal:
    symbol: str
    market: str
    detector: str
    score: int
    setup_score: float
    edge_score: float
    conditions_met: dict[str, bool]      # 最後一根 K 的各子條件狀態
    edge: "EdgeStats"
    capped_by_samples: bool              # 是否因樣本不足被壓到 45
    decayed: bool


def score_signal(df: pd.DataFrame, detector_name: str, edge: "EdgeStats", cfg,
                 symbol: str = "", market: str = "") -> ScoredSignal:
    """原則 2：沒有回測就沒有分數；樣本不足時分數硬性上限 45。"""
    result = run_detector(df, detector_name, cfg)
    conditions_met = {k: bool(v.iloc[-1]) for k, v in result.conditions.items()}
    setup_score = 100.0 * sum(
        result.weights[k] * (1.0 if conditions_met[k] else 0.0) for k in result.weights
    )

    k = cfg.backtest.shrinkage_k
    expectancy_shrunk = edge.expectancy_R * edge.n / (edge.n + k) if edge.n > 0 else 0.0
    edge_score = float(np.clip(
        (expectancy_shrunk - EDGE_SCORE_FLOOR_R) / EDGE_SCORE_SPAN_R * 100.0, 0.0, 100.0
    ))

    ew = cfg.signals.edge_weight
    raw = ew * edge_score + (1.0 - ew) * setup_score
    if edge.decayed:
        raw *= cfg.backtest.decay_penalty
    capped = edge.n < cfg.signals.min_samples
    if capped:
        raw = min(raw, float(SAMPLE_CAP_SCORE))
    final = int(round(float(np.clip(raw, 0.0, 100.0))))
    return ScoredSignal(
        symbol=symbol, market=market, detector=detector_name,
        score=final, setup_score=round(setup_score, 2), edge_score=round(edge_score, 2),
        conditions_met=conditions_met, edge=edge,
        capped_by_samples=capped, decayed=edge.decayed,
    )


def resolve_signals(scored: list[ScoredSignal]) -> tuple[ScoredSignal | None, bool]:
    """同標的多 detector 同時觸發：取最高分；momentum（順勢）+ reversal（逆勢）
    同時觸發 → 衝突 → (None, True)，上層轉 ABSTAIN。
    """
    if not scored:
        return None, False
    names = {s.detector for s in scored}
    if "momentum" in names and "reversal" in names:
        return None, True
    return max(scored, key=lambda s: s.score), False
