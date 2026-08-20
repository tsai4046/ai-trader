"""走勢回測，產出 edge 統計。整個系統的信心來源。

嚴格禁止前視：
  - 進場 = 訊號「隔一根」的開盤價（訊號當根收盤才確認）
  - 停損用訊號當根（t）的 ATR，不是 t+1 的
  - 同一根 K 同時觸及停損與目標 → 一律算停損（保守假設，不准改）
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from core.indicators import atr
from core.signals import ATR_N, run_detector

EDGE_CACHE_TTL_DAYS = 7
CACHE_DIR = Path("data/cache")


@dataclass
class EdgeStats:
    detector: str
    symbol: str
    n: int
    win_rate: float
    expectancy_R: float
    median_bars_held: int
    in_sample_expectancy: float
    holdout_expectancy: float
    decayed: bool


@dataclass
class SimTrade:
    signal_idx: int
    entry: float
    stop: float
    target: float
    exit_idx: int
    r: float
    bars_held: int


def simulate_trades(df: pd.DataFrame, fired: pd.Series, cfg) -> list[SimTrade]:
    """逐根掃描 fired，模擬每筆交易。重疊訊號（前一筆未出場）跳過。"""
    horizon = cfg.backtest.horizon_bars
    rr_target = cfg.backtest.rr_target
    stop_mult = cfg.backtest.atr_stop_mult

    a = atr(df, ATR_N)
    open_ = df["open"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    fired_np = fired.to_numpy()
    atr_np = a.to_numpy()
    n_bars = len(df)

    trades: list[SimTrade] = []
    open_until = -1  # 前一筆模擬交易的出場 bar index

    for t in range(n_bars):
        if not fired_np[t]:
            continue
        if t <= open_until:          # 重疊訊號：跳過，不做金字塔加碼
            continue
        if t + horizon >= n_bars:    # 未來 K 棒不足以走完 horizon → 不納入樣本（記於 NOTES.md）
            continue
        if math.isnan(atr_np[t]) or atr_np[t] <= 0:
            continue

        entry = open_[t + 1]                      # 隔日開盤進場
        stop = entry - stop_mult * atr_np[t]      # 用 t 當根的 ATR
        if stop >= entry:
            continue
        target = entry + rr_target * (entry - stop)
        risk = entry - stop

        r_val = None
        exit_idx = t + horizon
        for j in range(t + 1, t + horizon + 1):
            if low[j] <= stop:                    # 先檢查停損：同根雙觸及一律算停損
                r_val = -1.0
                exit_idx = j
                break
            if high[j] >= target:
                r_val = rr_target
                exit_idx = j
                break
        if r_val is None:                          # 走完 horizon → 收盤出場
            exit_idx = t + horizon
            r_val = (close[exit_idx] - entry) / risk

        trades.append(SimTrade(signal_idx=t, entry=float(entry), stop=float(stop),
                               target=float(target), exit_idx=int(exit_idx),
                               r=float(r_val), bars_held=int(exit_idx - t)))
        open_until = exit_idx
    return trades


def _expectancy(rs: list[float]) -> float:
    return float(np.mean(rs)) if rs else 0.0


def backtest_detector(df: pd.DataFrame, detector_name: str, cfg,
                      symbol: str = "") -> EdgeStats:
    result = run_detector(df, detector_name, cfg)
    trades = simulate_trades(df, result.fired, cfg)

    n = len(trades)
    rs = [tr.r for tr in trades]
    win_rate = float(np.mean([r > 0 for r in rs])) if rs else 0.0
    expectancy = _expectancy(rs)
    median_held = int(np.median([tr.bars_held for tr in trades])) if trades else 0

    # 依時間排序（simulate 已按時間產出），最後 holdout_frac 為 out-of-sample
    split = int(round(n * (1.0 - cfg.backtest.holdout_frac)))
    in_rs, out_rs = rs[:split], rs[split:]
    in_exp = _expectancy(in_rs)
    out_exp = _expectancy(out_rs)
    decayed = bool(out_rs) and (in_exp > 0) and (out_exp < 0)

    return EdgeStats(detector=detector_name, symbol=symbol, n=n,
                     win_rate=round(win_rate, 4), expectancy_R=round(expectancy, 4),
                     median_bars_held=median_held,
                     in_sample_expectancy=round(in_exp, 4),
                     holdout_expectancy=round(out_exp, 4), decayed=decayed)


# --- edge 快取：TTL 7 天 --------------------------------------------------------


def _edge_cache_path(market: str, symbol: str, detector: str) -> Path:
    return CACHE_DIR / f"edge_{market}_{symbol}_{detector}.json"


def get_edge(df: pd.DataFrame, detector_name: str, cfg, symbol: str = "",
             market: str = "", use_cache: bool = True) -> EdgeStats:
    p = _edge_cache_path(market, symbol, detector_name)
    if use_cache and market and symbol and p.exists():
        age_days = (time.time() - p.stat().st_mtime) / 86400.0
        if age_days <= EDGE_CACHE_TTL_DAYS:
            try:
                return EdgeStats(**json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass
    edge = backtest_detector(df, detector_name, cfg, symbol=symbol)
    if use_cache and market and symbol:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(asdict(edge)), encoding="utf-8")
        except Exception:
            pass
    return edge
