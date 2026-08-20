"""六道硬性風控閘。全部是純數值檢查。

原則 3：風控是程式碼，不是提示詞。本檔不引用定性層模組，
也不接受任何來自定性層的參數；任一閘失敗 → REJECT，不因訊號強而放寬。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from core.datasource import Bars
from core.indicators import atr
from core.plan import TradePlan
from core.signals import ATR_N, VOL_REGIME_WINDOW

TW_LOT_SIZE = 1000
CORRELATION_RETURN_WINDOW = 60      # 60 日日報酬相關係數
MIN_CORR_OVERLAP = 30               # 報酬序列重疊少於此數 → 視為資料不足，用 sector fallback
ATR_SPIKE_HALVE_FACTOR = 0.5        # 「砍半」的定義（觸發門檻在 config: atr_spike_halve）


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str
    value: float
    limit: float


@dataclass
class RiskAssessment:
    gates: list[GateResult]
    shares: int
    position_value: float
    risk_amount: float
    risk_pct: float
    all_passed: bool
    blocking_reasons: list[str]


@dataclass
class Position:
    run_id: str
    symbol: str
    market: str
    sector: str
    entry_price: float
    stop: float
    shares: int
    risk_amount: float                 # (entry - stop) * shares
    opened_at: date


@dataclass
class Portfolio:
    equity: float
    positions: list[Position] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    current_drawdown_pct: float = 0.0
    realized_loss_today: float = 0.0


def atr_spike_ratio(df: pd.DataFrame) -> float:
    """ATR(14) / 其 60 日中位數。算不出來回傳 NaN。"""
    a = atr(df, ATR_N)
    med = a.rolling(VOL_REGIME_WINDOW).median()
    last_a, last_med = a.iloc[-1], med.iloc[-1]
    if pd.isna(last_a) or pd.isna(last_med) or last_med <= 0:
        return float("nan")
    return float(last_a / last_med)


def global_risk_state(portfolio: Portfolio, cfg) -> tuple[str, list[str]]:
    """回傳 ('normal' | 'observe', reasons)。observe = 全域觀察模式：
    回撤超限或當日已實現虧損超限 → 所有候選一律降為 WATCH。
    """
    reasons = []
    if portfolio.current_drawdown_pct > cfg.risk.max_drawdown_pct:
        reasons.append(
            f"drawdown {portfolio.current_drawdown_pct:.1f}% > {cfg.risk.max_drawdown_pct}%"
        )
    loss_pct = (portfolio.realized_loss_today / portfolio.equity * 100.0
                if portfolio.equity > 0 else 0.0)
    if loss_pct > cfg.risk.max_daily_loss_pct:
        reasons.append(f"daily loss {loss_pct:.1f}% > {cfg.risk.max_daily_loss_pct}%")
    return ("observe" if reasons else "normal"), reasons


def _returns(df: pd.DataFrame, n: int = CORRELATION_RETURN_WINDOW) -> pd.Series:
    return df["close"].pct_change().dropna().tail(n)


def assess(plan: TradePlan, portfolio: Portfolio, bars: Bars,
           universe_bars: dict[str, Bars], cfg) -> RiskAssessment:
    gates: list[GateResult] = []
    blocking: list[str] = []
    equity = portfolio.equity
    df = bars.df

    # --- Gate 5 素材：波動比（也影響 Gate 1 的部位大小）---
    ratio = atr_spike_ratio(df)
    vol_extreme = (not math.isnan(ratio)) and ratio > cfg.risk.atr_spike_reject
    vol_halve = (not math.isnan(ratio)) and ratio > cfg.risk.atr_spike_halve

    # --- Gate 1：部位大小 ---
    risk_per_share = plan.entry_high - plan.stop
    if risk_per_share <= 0:
        shares = 0
        g1_detail = "entry_high <= stop，無法計算部位"
    else:
        raw_shares = math.floor(
            equity * cfg.risk.max_risk_per_trade_pct / 100.0 / risk_per_share
        )
        if vol_extreme:
            g1_detail = f"atr_ratio {ratio:.2f} > {cfg.risk.atr_spike_reject}（volatility_extreme）"
            shares = 0
        else:
            if vol_halve:
                raw_shares = math.floor(raw_shares * ATR_SPIKE_HALVE_FACTOR)
            cap_shares = math.floor(equity * cfg.risk.max_position_pct / 100.0 / plan.entry_high)
            shares = min(raw_shares, cap_shares)
            if plan.market == "tw" and not cfg.account.allow_odd_lot:
                shares = (shares // TW_LOT_SIZE) * TW_LOT_SIZE
            g1_detail = (f"raw={raw_shares}, cap={cap_shares}"
                         + (f", ATR spike {ratio:.2f} → 砍半" if vol_halve else ""))

    risk_amount = shares * risk_per_share if shares > 0 else 0.0
    position_value = shares * plan.entry_high
    risk_pct = risk_amount / equity * 100.0 if equity > 0 else 0.0

    if vol_extreme:
        g1_pass = False
        blocking.append("volatility_extreme")
    elif shares <= 0:
        g1_pass = False
        blocking.append("position_too_small")
        g1_detail += "；股數為 0（高價股 + 整股限制時應明確拒絕，不默默放行 1 股）"
    else:
        g1_pass = True
    gates.append(GateResult("position_size", g1_pass, g1_detail,
                            value=round(risk_pct, 4), limit=cfg.risk.max_risk_per_trade_pct))

    # --- Gate 2：總曝險上限 ---
    existing_risk = sum(p.risk_amount for p in portfolio.positions)
    total_risk = existing_risk + risk_amount
    limit_amt = equity * cfg.risk.max_total_risk_pct / 100.0
    g2_pass = total_risk <= limit_amt
    gates.append(GateResult(
        "total_risk", g2_pass,
        f"既有 {existing_risk:,.0f} + 本筆 {risk_amount:,.0f} vs 上限 {limit_amt:,.0f}",
        value=round(total_risk / equity * 100.0, 4) if equity > 0 else 0.0,
        limit=cfg.risk.max_total_risk_pct,
    ))
    if not g2_pass:
        blocking.append("max_total_risk")

    # --- Gate 3：相關性集中度（一定要用日報酬率算，不准用價格。陷阱 #5）---
    cand_ret = _returns(df)
    corr_count = 0
    fallback_notes = []
    sector_count = sum(1 for p in portfolio.positions if p.sector == plan.sector)
    for pos in portfolio.positions:
        pos_bars = universe_bars.get(pos.symbol)
        corr = float("nan")
        if pos_bars is not None and not pos_bars.empty:
            pos_ret = _returns(pos_bars.df)
            joined = pd.concat([cand_ret, pos_ret], axis=1, join="inner").dropna()
            if len(joined) >= MIN_CORR_OVERLAP:
                corr = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
        if math.isnan(corr):
            # 資料不足 → sector fallback：同 sector 視為相關
            if pos.sector == plan.sector:
                corr_count += 1
                fallback_notes.append(f"{pos.symbol}(sector fallback)")
        elif corr > cfg.risk.correlation_threshold:
            corr_count += 1
    g3_pass = corr_count < cfg.risk.max_correlated_positions
    g3_detail = f"高相關持倉 {corr_count} 檔（門檻 corr>{cfg.risk.correlation_threshold}）"
    if fallback_notes:
        g3_detail += f"；資料不足改用 sector 計數: {', '.join(fallback_notes)}"
    # 同 sector 持倉數同樣套用上限
    if sector_count >= cfg.risk.max_correlated_positions:
        g3_pass = False
        g3_detail += f"；同 sector 持倉已達 {sector_count} 檔"
    gates.append(GateResult("correlation", g3_pass, g3_detail,
                            value=float(max(corr_count, sector_count)),
                            limit=float(cfg.risk.max_correlated_positions)))
    if not g3_pass:
        blocking.append("correlation_concentration")

    # --- Gate 4：回撤 / 當日虧損（全域）---
    mode, reasons = global_risk_state(portfolio, cfg)
    g4_pass = mode == "normal"
    gates.append(GateResult(
        "drawdown", g4_pass,
        "; ".join(reasons) if reasons else
        f"回撤 {portfolio.current_drawdown_pct:.1f}%、當日虧損 {portfolio.realized_loss_today:,.0f}",
        value=round(portfolio.current_drawdown_pct, 4), limit=cfg.risk.max_drawdown_pct,
    ))
    if not g4_pass:
        blocking.append("global_risk_observe_mode")

    # --- Gate 5：波動檢查（獨立輸出讓儀表板顯示）---
    if math.isnan(ratio):
        g5_pass, g5_detail = False, "ATR ratio 算不出來（資料不足）"
        blocking.append("volatility_unavailable")
    elif vol_extreme:
        g5_pass, g5_detail = False, f"atr_ratio {ratio:.2f} > reject {cfg.risk.atr_spike_reject}"
    else:
        g5_pass = True
        g5_detail = (f"atr_ratio {ratio:.2f}"
                     + (f" > halve {cfg.risk.atr_spike_halve} → 部位砍半" if vol_halve else " 正常"))
    gates.append(GateResult("volatility", g5_pass, g5_detail,
                            value=round(ratio, 4) if not math.isnan(ratio) else -1.0,
                            limit=cfg.risk.atr_spike_reject))

    # --- Gate 6：最小風險報酬比 ---
    g6_pass = plan.risk_reward >= cfg.risk.min_risk_reward
    gates.append(GateResult("min_risk_reward", g6_pass,
                            f"RR {plan.risk_reward:.2f} vs 下限 {cfg.risk.min_risk_reward}",
                            value=plan.risk_reward, limit=cfg.risk.min_risk_reward))
    if not g6_pass:
        blocking.append("min_risk_reward")

    all_passed = all(g.passed for g in gates)
    return RiskAssessment(
        gates=gates, shares=int(shares), position_value=round(position_value, 2),
        risk_amount=round(risk_amount, 2), risk_pct=round(risk_pct, 4),
        all_passed=all_passed, blocking_reasons=blocking,
    )
