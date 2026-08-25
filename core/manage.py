"""追蹤清單與庫存的管理邏輯（CLI 與本機網頁介面共用）。

- watchlist：讀寫 watchlist.yaml（注意：程式改寫會失去手寫註解，記於 NOTES.md）
- 庫存：寫 outcomes.jsonl。手動登記的持倉 run_id 以 manual- 開頭、
  不要求 journal 有對應紀錄；build_portfolio 直接從 outcome 欄位重建。
"""
from __future__ import annotations

import secrets
from datetime import date, datetime
from pathlib import Path

import yaml

from core.config import ConfigError, load_watchlist


def infer_market(symbol: str) -> str:
    """台股代號為純數字（可含 .TW 尾碼前的數字），其餘視為美股。"""
    return "tw" if symbol.strip().isdigit() else "us"


def normalize_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s or len(s) > 12 or any(c in s for c in " \t\n'\"\\/:"):
        raise ValueError(f"不合法的代號: {symbol!r}")
    return s


# --- watchlist -------------------------------------------------------------------


def watchlist_path(cfg) -> Path:
    return Path(cfg.markets.us.symbols_from)


def load_watchlist_raw(cfg) -> dict[str, list[dict]]:
    return load_watchlist(watchlist_path(cfg))


def _write_watchlist(cfg, wl: dict[str, list[dict]]) -> None:
    out = {}
    for market in ("us", "tw"):
        out[market] = [
            {k: v for k, v in {
                "symbol": e["symbol"], "name": e.get("name", e["symbol"]),
                "sector": e.get("sector", "unknown"),
            }.items()}
            for e in wl.get(market, [])
        ]
    watchlist_path(cfg).write_text(
        yaml.safe_dump(out, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def add_symbol(cfg, symbol: str, sector: str = "unknown", name: str = "",
               market: str = "") -> dict:
    symbol = normalize_symbol(symbol)
    market = market or infer_market(symbol)
    if market not in ("us", "tw"):
        raise ValueError(f"market 必須是 us 或 tw: {market!r}")
    wl = load_watchlist_raw(cfg)
    for m, entries in wl.items():
        if any(e["symbol"] == symbol for e in entries):
            raise ValueError(f"{symbol} 已在追蹤清單（{m}）")
    entry = {"symbol": symbol, "sector": (sector or "unknown").strip() or "unknown",
             "name": (name or symbol).strip() or symbol, "market": market}
    wl[market].append(entry)
    _write_watchlist(cfg, wl)
    return entry


def remove_symbol(cfg, symbol: str) -> bool:
    symbol = normalize_symbol(symbol)
    wl = load_watchlist_raw(cfg)
    found = False
    for market in ("us", "tw"):
        before = len(wl[market])
        wl[market] = [e for e in wl[market] if e["symbol"] != symbol]
        found = found or len(wl[market]) < before
    if found:
        _write_watchlist(cfg, wl)
    return found


# --- 庫存 -------------------------------------------------------------------------


def add_holding(cfg, symbol: str, entry_price: float, shares: int,
                stop: float, sector: str = "unknown",
                opened_at: str = "", market: str = "") -> dict:
    """登記一筆既有持倉（不需要來自系統訊號）。寫入 OPEN outcome。

    停損必填：風險金額 =（進場價 − 停損）× 股數，是 Gate 2 總曝險的依據，
    沒有停損就沒有可計算的風險，也不符合這套系統「每筆持倉先想好出場」的前提。
    """
    from core.journal import append_outcome

    symbol = normalize_symbol(symbol)
    market = market or infer_market(symbol)
    entry_price = float(entry_price)
    shares = int(shares)
    stop = float(stop)
    if entry_price <= 0:
        raise ValueError("進場價必須 > 0")
    if shares <= 0:
        raise ValueError("股數必須 > 0")
    if not (0 < stop < entry_price):
        raise ValueError("停損必填，且必須介於 0 與進場價之間（本系統只做多）")
    run_id = f"manual-{datetime.now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"
    record = {
        "run_id": run_id, "symbol": symbol, "market": market, "state": "OPEN",
        "entry_price": entry_price, "shares": shares, "stop": stop,
        "sector": (sector or "unknown").strip() or "unknown",
        "opened_at": opened_at or str(date.today()),
        "notes": "manual holding",
    }
    append_outcome(cfg, record)
    return record


def close_holding(cfg, run_id: str, exit_price: float, exit_date: str = "",
                  reason: str = "manual", notes: str = "") -> dict:
    """平倉。journal 有紀錄就用計畫欄位，沒有（手動持倉）就用 OPEN outcome 欄位。"""
    from core.journal import append_outcome, read_jsonl

    exit_price = float(exit_price)
    outcomes = read_jsonl(cfg.output.outcomes_path)
    opened = next((o for o in reversed(outcomes)
                   if o.get("run_id") == run_id and o.get("state") == "OPEN"), None)
    journal = read_jsonl(cfg.output.journal_path)
    jrec = next((r for r in journal if r.get("run_id") == run_id and r.get("plan")), None)
    if opened is None and jrec is None:
        raise ValueError(f"找不到 run_id={run_id} 的持倉或計畫")

    plan = (jrec or {}).get("plan") or {}
    entry = float((opened or {}).get("entry_price") or plan.get("entry_high") or 0.0)
    stop = float((opened or {}).get("stop") or plan.get("stop") or 0.0)
    shares = int((opened or {}).get("shares")
                 or ((jrec or {}).get("risk") or {}).get("shares") or 0)
    if entry <= 0:
        raise ValueError("持倉缺少進場價，無法計算損益")
    risk_per_share = entry - stop
    r_multiple = (exit_price - entry) / risk_per_share if risk_per_share > 0 else 0.0
    pnl = (exit_price - entry) * shares
    symbol = (opened or jrec or {}).get("symbol", "")
    record = {
        "run_id": run_id, "symbol": symbol, "state": "CLOSED",
        "entry_price": entry, "exit_price": exit_price,
        "exit_date": exit_date or str(date.today()),
        "r_multiple": round(r_multiple, 4), "pnl": round(pnl, 2),
        "exit_reason": reason or "manual", "shares": shares, "stop": stop,
        "notes": notes,
    }
    append_outcome(cfg, record)
    return record


def list_holdings(cfg) -> list:
    from core.journal import build_portfolio

    return build_portfolio(cfg).positions
