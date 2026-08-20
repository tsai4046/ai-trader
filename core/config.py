"""載入並驗證 config.yaml / watchlist.yaml。

缺欄位、型別錯誤、數值範圍不合理 → 一律 raise ConfigError，
由 run.py 捕捉後以非 0 exit code 退出。不提供任何預設值默默帶過。
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(Exception):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCfg(StrictModel):
    equity: float = Field(gt=0)
    currency: Literal["TWD", "USD"]
    allow_odd_lot: bool


class UsMarketCfg(StrictModel):
    enabled: bool
    provider: str
    symbols_from: str


class TwMarketCfg(StrictModel):
    enabled: bool
    provider: str
    finmind_token_env: str


class MarketsCfg(StrictModel):
    us: UsMarketCfg
    tw: TwMarketCfg


class DataCfg(StrictModel):
    lookback_days: int = Field(gt=0)
    interval: Literal["1d", "60m"]
    cache_ttl_hours: float = Field(ge=0)
    min_bars_required: int = Field(gt=0)
    max_staleness_days: int = Field(ge=0)


VALID_DETECTORS = {"breakout", "pullback", "momentum", "trend_continuation", "reversal"}


class SignalsCfg(StrictModel):
    enabled_detectors: list[str]
    min_samples: int = Field(gt=0)
    approve_score: float = Field(ge=0, le=100)
    watch_score: float = Field(ge=0, le=100)
    edge_weight: float = Field(ge=0, le=1)


class BacktestCfg(StrictModel):
    horizon_bars: int = Field(gt=0)
    rr_target: float = Field(gt=0)
    atr_stop_mult: float = Field(gt=0)
    holdout_frac: float = Field(gt=0, lt=1)
    decay_penalty: float = Field(ge=0, le=1)
    shrinkage_k: float = Field(ge=0)


class RiskCfg(StrictModel):
    max_risk_per_trade_pct: float = Field(gt=0)
    max_total_risk_pct: float = Field(gt=0)
    max_position_pct: float = Field(gt=0)
    max_daily_loss_pct: float = Field(gt=0)
    max_drawdown_pct: float = Field(gt=0)
    min_risk_reward: float = Field(gt=0)
    correlation_threshold: float = Field(ge=-1, le=1)
    max_correlated_positions: int = Field(ge=0)
    atr_spike_halve: float = Field(gt=0)
    atr_spike_reject: float = Field(gt=0)


class PlanCfg(StrictModel):
    ttl_days: int = Field(gt=0)
    target_rr: float = Field(gt=0)


class LlmCfg(StrictModel):
    enabled: bool
    provider: str
    model: str
    api_key_env: str
    max_candidates: int = Field(ge=0)


class OutputCfg(StrictModel):
    html_dir: str
    journal_path: str
    outcomes_path: str


class Config(StrictModel):
    account: AccountCfg
    markets: MarketsCfg
    data: DataCfg
    signals: SignalsCfg
    backtest: BacktestCfg
    risk: RiskCfg
    plan: PlanCfg
    llm: LlmCfg
    output: OutputCfg


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config 檔不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"config YAML 解析失敗: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config 內容不是 mapping")
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"config 驗證失敗:\n{e}") from e

    unknown = set(cfg.signals.enabled_detectors) - VALID_DETECTORS
    if unknown:
        raise ConfigError(f"未知的 detector: {sorted(unknown)}")
    if cfg.signals.watch_score > cfg.signals.approve_score:
        raise ConfigError("watch_score 不得大於 approve_score")
    if cfg.risk.atr_spike_halve > cfg.risk.atr_spike_reject:
        raise ConfigError("atr_spike_halve 不得大於 atr_spike_reject")
    return cfg


def load_watchlist(path: str | Path) -> dict[str, list[dict]]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"watchlist 檔不存在: {p}")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"watchlist YAML 解析失敗: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("watchlist 內容不是 mapping")
    out: dict[str, list[dict]] = {}
    for market in ("us", "tw"):
        entries = raw.get(market) or []
        if not isinstance(entries, list):
            raise ConfigError(f"watchlist.{market} 必須是 list")
        cleaned = []
        for e in entries:
            if not isinstance(e, dict) or "symbol" not in e:
                raise ConfigError(f"watchlist.{market} 每筆必須含 symbol: {e!r}")
            cleaned.append({
                "symbol": str(e["symbol"]),
                "sector": str(e.get("sector", "unknown")),
                "name": str(e.get("name", e["symbol"])),
                "market": market,
            })
        out[market] = cleaned
    return out
