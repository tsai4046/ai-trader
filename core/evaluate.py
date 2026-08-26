"""策略評估：多輪（walk-forward 時間分段）× 跨標的回測驗證，產出 detector 排行。

刻意的設計邊界（對應規格 §11 的過擬合警告）：
  - 只評估 detector 的「典型參數」，不做參數網格搜尋——多輪驗證是用來
    淘汰不穩健的策略，不是用來調出好看的參數。
  - 穩健（robust）門檻是硬性的：樣本夠、整體期望值為正、多數時間分段為正、
    holdout 未衰退。四者缺一即不推薦。
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from core.backtest import backtest_detector, simulate_trades
from core.signals import DETECTORS, run_detector

EVALUATION_JSON = Path("data/evaluation.json")


@dataclass
class FoldStats:
    fold: int
    n: int
    expectancy: float
    win_rate: float


@dataclass
class SymbolEvaluation:
    detector: str
    symbol: str
    market: str
    n: int
    expectancy: float
    win_rate: float
    folds: list[FoldStats]
    positive_folds: int
    active_folds: int              # 有交易的分段數
    decayed: bool
    robust: bool
    reasons: list[str]             # 不 robust 的原因


@dataclass
class DetectorSummary:
    detector: str
    symbols_evaluated: int
    symbols_robust: int
    pooled_n: int
    pooled_expectancy: float
    pooled_win_rate: float
    median_symbol_expectancy: float
    stability: float               # 所有標的分段中為正的比例
    score: float                   # 排行用：收縮後期望值 × 穩定度
    recommended: bool


def evaluate_symbol(df, detector_name: str, cfg, symbol: str = "",
                    market: str = "") -> SymbolEvaluation:
    result = run_detector(df, detector_name, cfg)
    trades = simulate_trades(df, result.fired, cfg)
    n_folds = cfg.evaluate.n_folds
    rs = [t.r for t in trades]
    n = len(trades)
    expectancy = float(np.mean(rs)) if rs else 0.0
    win_rate = float(np.mean([r > 0 for r in rs])) if rs else 0.0

    # 依訊號時間切成 K 段（walk-forward 檢驗：edge 是否跨時期存在）
    folds: list[FoldStats] = []
    if n > 0:
        bounds = np.linspace(0, n, n_folds + 1).astype(int)
        for k in range(n_folds):
            seg = rs[bounds[k]:bounds[k + 1]]
            folds.append(FoldStats(
                fold=k + 1, n=len(seg),
                expectancy=round(float(np.mean(seg)), 4) if seg else 0.0,
                win_rate=round(float(np.mean([r > 0 for r in seg])), 4) if seg else 0.0,
            ))
    active = [f for f in folds if f.n > 0]
    positive = [f for f in active if f.expectancy > 0]

    edge = backtest_detector(df, detector_name, cfg, symbol=symbol)

    reasons = []
    if n < cfg.signals.min_samples:
        reasons.append(f"樣本不足 n={n}<{cfg.signals.min_samples}")
    if expectancy <= 0:
        reasons.append(f"期望值非正 {expectancy:.2f}R")
    need = math.ceil(cfg.evaluate.min_positive_fold_ratio * len(active)) if active else 1
    if len(positive) < need:
        reasons.append(f"僅 {len(positive)}/{len(active)} 個時間分段為正（需 {need}）")
    if edge.decayed:
        reasons.append("holdout 衰退")

    return SymbolEvaluation(
        detector=detector_name, symbol=symbol, market=market,
        n=n, expectancy=round(expectancy, 4), win_rate=round(win_rate, 4),
        folds=folds, positive_folds=len(positive), active_folds=len(active),
        decayed=edge.decayed, robust=not reasons, reasons=reasons,
    )


def summarize_detector(evals: list[SymbolEvaluation], cfg) -> DetectorSummary:
    detector = evals[0].detector
    all_ns = sum(e.n for e in evals)
    pooled_rs_mean = (
        sum(e.expectancy * e.n for e in evals) / all_ns if all_ns else 0.0
    )
    pooled_wins = (
        sum(e.win_rate * e.n for e in evals) / all_ns if all_ns else 0.0
    )
    active = sum(e.active_folds for e in evals)
    positive = sum(e.positive_folds for e in evals)
    stability = positive / active if active else 0.0
    per_symbol = [e.expectancy for e in evals if e.n > 0]
    median_exp = float(np.median(per_symbol)) if per_symbol else 0.0
    shrunk = pooled_rs_mean * all_ns / (all_ns + cfg.backtest.shrinkage_k)
    score = shrunk * stability
    robust_count = sum(1 for e in evals if e.robust)
    recommended = (
        all_ns >= cfg.signals.min_samples
        and pooled_rs_mean > 0
        and stability >= cfg.evaluate.min_positive_fold_ratio
        and robust_count >= 1
    )
    return DetectorSummary(
        detector=detector, symbols_evaluated=len(evals),
        symbols_robust=robust_count, pooled_n=all_ns,
        pooled_expectancy=round(pooled_rs_mean, 4),
        pooled_win_rate=round(pooled_wins, 4),
        median_symbol_expectancy=round(median_exp, 4),
        stability=round(stability, 4), score=round(score, 4),
        recommended=recommended,
    )


def evaluate_universe(cfg, universe: list[dict], fetch_fn,
                      detectors: list[str] | None = None) -> dict:
    """對 watchlist × 所有 detector 跑多輪驗證。fetch_fn(entry) -> Bars。
    單一標的失敗不中斷（與 scan 相同原則）。
    """
    detectors = detectors or sorted(DETECTORS.keys())
    per_symbol: list[SymbolEvaluation] = []
    skipped: list[dict] = []
    for entry in universe:
        bars = fetch_fn(entry)
        if bars.empty or len(bars.df) < cfg.data.min_bars_required:
            skipped.append({"symbol": entry["symbol"],
                            "reason": "資料不足或抓取失敗"})
            continue
        for name in detectors:
            try:
                per_symbol.append(evaluate_symbol(
                    bars.df, name, cfg,
                    symbol=entry["symbol"], market=entry["market"]))
            except Exception as e:
                skipped.append({"symbol": entry["symbol"],
                                "reason": f"{name}: {e}"})

    summaries = []
    for name in detectors:
        evals = [e for e in per_symbol if e.detector == name]
        if evals:
            summaries.append(summarize_detector(evals, cfg))
    summaries.sort(key=lambda s: -s.score)

    report = {
        "evaluated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "n_folds": cfg.evaluate.n_folds,
        "universe": [e["symbol"] for e in universe],
        "detectors": [asdict(s) for s in summaries],
        "per_symbol": [asdict(e) for e in per_symbol],
        "skipped": skipped,
        "recommended": [s.detector for s in summaries if s.recommended],
    }
    return report


def save_evaluation(report: dict, path: Path = EVALUATION_JSON) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                    encoding="utf-8")


def load_evaluation(path: Path = EVALUATION_JSON) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
