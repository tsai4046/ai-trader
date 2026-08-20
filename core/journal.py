"""append-only JSONL 決策日誌 + outcomes 回填 + 組合重建。

journal.jsonl：每次決策一筆（系統長期價值最高的產物）。
outcomes.jsonl：人工回填的實際結果（OPEN / CLOSED / SKIPPED）。
"""
from __future__ import annotations

import json
import secrets
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from core.risk import Portfolio, Position, RiskAssessment

SCHEMA_VERSION = 1

JOURNAL_SCHEMA = {
    "type": "object",
    "required": [
        "schema_version", "run_id", "ts", "symbol", "market", "detector",
        "status", "needs_human", "score", "score_breakdown", "edge_stats",
        "plan", "risk", "llm", "data", "blocking_reasons",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "run_id": {"type": "string", "minLength": 1},
        "ts": {"type": "string"},
        "symbol": {"type": "string"},
        "market": {"type": "string", "enum": ["us", "tw"]},
        "detector": {"type": ["string", "null"]},
        "status": {"enum": ["APPROVE", "WATCH", "REJECT", "ABSTAIN",
                            "INVALIDATED", "EXPIRED"]},
        "needs_human": {"type": "boolean"},
        "score": {"type": ["integer", "null"]},
        "score_breakdown": {"type": ["object", "null"]},
        "edge_stats": {"type": ["object", "null"]},
        "plan": {"type": ["object", "null"]},
        "risk": {"type": ["object", "null"]},
        "llm": {"type": "object"},
        "data": {"type": "object"},
        "blocking_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

OUTCOME_STATES = ("OPEN", "CLOSED", "SKIPPED")


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return f"{now.strftime('%Y%m%dT%H%M')}Z-{secrets.token_hex(2)}"


def validate_journal_record(record: dict) -> None:
    import jsonschema

    jsonschema.validate(record, JOURNAL_SCHEMA)


def append_jsonl(path: str | Path, record: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 壞行跳過，append-only 檔不可修改
    return out


def write_journal(cfg, record: dict) -> None:
    validate_journal_record(record)
    append_jsonl(cfg.output.journal_path, record)


def make_journal_record(*, run_id: str, symbol: str, market: str,
                        detector: str | None, status: str, needs_human: bool,
                        score: int | None = None, score_breakdown: dict | None = None,
                        edge_stats: dict | None = None, plan: dict | None = None,
                        risk: dict | None = None, llm: dict | None = None,
                        data: dict | None = None,
                        blocking_reasons: list[str] | None = None,
                        ts: datetime | None = None) -> dict:
    ts = ts or datetime.now().astimezone()
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "ts": ts.isoformat(timespec="seconds"),
        "symbol": symbol,
        "market": market,
        "detector": detector,
        "status": status,
        "needs_human": needs_human,
        "score": score,
        "score_breakdown": score_breakdown,
        "edge_stats": edge_stats,
        "plan": plan,
        "risk": risk,
        "llm": llm or {"enabled": False, "verdict": None,
                       "concerns": [], "unresolved_questions": []},
        "data": data or {},
        "blocking_reasons": blocking_reasons or [],
    }


def risk_to_dict(ra: RiskAssessment) -> dict:
    return {
        "shares": ra.shares,
        "position_value": ra.position_value,
        "risk_amount": ra.risk_amount,
        "risk_pct": ra.risk_pct,
        "gates": [asdict(g) for g in ra.gates],
    }


# --- outcomes 回填與組合重建 ------------------------------------------------------


def append_outcome(cfg, record: dict) -> None:
    if record.get("state") not in OUTCOME_STATES:
        raise ValueError(f"state 必須是 {OUTCOME_STATES}")
    append_jsonl(cfg.output.outcomes_path, record)


def _latest_outcomes_by_run(outcomes: list[dict]) -> dict[str, dict]:
    """同 run_id 多筆時取最後一筆（append-only：後面的是最新狀態）。"""
    latest: dict[str, dict] = {}
    for o in outcomes:
        rid = o.get("run_id")
        if rid:
            latest[rid] = o
    return latest


def build_portfolio(cfg) -> Portfolio:
    """從 outcomes.jsonl 的 OPEN 紀錄重建持倉；權益曲線依 exit_date 排序重建。
    缺漏資料一律保守假設（pnl 缺 → 當作 0，沒獲利）。陷阱 #7。
    """
    outcomes = read_jsonl(cfg.output.outcomes_path)
    journal = read_jsonl(cfg.output.journal_path)
    journal_by_run = {}
    for rec in journal:
        journal_by_run.setdefault(rec.get("run_id"), rec)

    latest = _latest_outcomes_by_run(outcomes)

    positions: list[Position] = []
    for rid, o in latest.items():
        if o.get("state") != "OPEN":
            continue
        j = journal_by_run.get(rid, {})
        jplan = j.get("plan") or {}
        jrisk = j.get("risk") or {}
        entry = float(o.get("entry_price") or jplan.get("entry_high") or 0.0)
        stop = float(o.get("stop") or jplan.get("stop") or 0.0)
        shares = int(o.get("shares") or jrisk.get("shares") or 0)
        risk_amount = (entry - stop) * shares if entry > stop and shares > 0 else 0.0
        opened = o.get("opened_at") or o.get("entry_date") or str(date.today())
        positions.append(Position(
            run_id=rid, symbol=str(o.get("symbol", "")),
            market=str(j.get("market", o.get("market", "us"))),
            sector=str(o.get("sector") or j.get("sector") or "unknown"),
            entry_price=entry, stop=stop, shares=shares,
            risk_amount=risk_amount,
            opened_at=pd.Timestamp(opened).date(),
        ))

    closed = [o for o in latest.values() if o.get("state") == "CLOSED"]
    closed.sort(key=lambda o: str(o.get("exit_date") or ""))   # 亂序防禦：按 exit_date 排序
    base = cfg.account.equity
    curve_vals, curve_idx = [], []
    running = base
    for o in closed:
        pnl = o.get("pnl")
        running += float(pnl) if pnl is not None else 0.0   # 缺 pnl → 保守當 0
        curve_vals.append(running)
        curve_idx.append(pd.Timestamp(o.get("exit_date") or date.today()))
    curve = pd.Series(curve_vals, index=curve_idx, dtype=float)

    if len(curve) > 0:
        peak = max(base, float(curve.cummax().iloc[-1]))
        current = float(curve.iloc[-1])
        dd_pct = (peak - current) / peak * 100.0 if peak > 0 else 0.0
    else:
        dd_pct = 0.0

    today = str(date.today())
    pnl_today = sum(float(o.get("pnl") or 0.0) for o in closed
                    if str(o.get("exit_date") or "") == today)
    realized_loss_today = max(0.0, -pnl_today)

    return Portfolio(
        equity=base, positions=positions, equity_curve=curve,
        current_drawdown_pct=round(dd_pct, 4),
        realized_loss_today=round(realized_loss_today, 2),
    )
