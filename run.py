#!/usr/bin/env python3
"""CLI 入口。

python run.py scan [--market us|tw] [--offline] [--no-llm]
python run.py backtest --symbol NVDA [--offline]
python run.py monitor
python run.py close --run-id ... --exit-price ... --exit-date ... [--reason ...]
python run.py open --run-id ... --entry-price ... [--shares ...]
python run.py skip --run-id ... [--notes ...]
python run.py stats
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pandas as pd

# 排程器（cron / schtasks）啟動時的工作目錄不一定在專案根目錄；
# 目前目錄沒有 config.yaml 時才退回 run.py 所在目錄（保留「在任意工作副本目錄執行」的用法）。
if not Path("config.yaml").exists():
    os.chdir(Path(__file__).resolve().parent)

log = logging.getLogger("ai-trader")

ABSTAIN_STATUSES = ("ABSTAIN",)
PENDING_STATUSES = ("APPROVE", "WATCH")


def _load_cfg():
    from core.config import ConfigError, load_config

    try:
        return load_config("config.yaml")
    except ConfigError as e:
        print(f"[config 錯誤] {e}", file=sys.stderr)
        sys.exit(1)


def _get_provider_for(market: str, cfg, offline: bool):
    from core.datasource import SyntheticProvider, get_provider

    if offline:
        return SyntheticProvider(cfg, market=market)
    name = cfg.markets.us.provider if market == "us" else cfg.markets.tw.provider
    return get_provider(name, cfg)


def _fetch(entry: dict, cfg, offline: bool):
    provider = _get_provider_for(entry["market"], cfg, offline)
    return provider.fetch(entry["symbol"], cfg.data.lookback_days, cfg.data.interval)


def _data_dict(bars) -> dict:
    return {
        "source": bars.source,
        "last_bar": str(bars.df.index[-1].date()) if not bars.empty else None,
        "stale_days": int(bars.stale_days) if not bars.empty else None,
        "bars": int(len(bars.df)),
    }


def _plan_to_dict(plan) -> dict:
    return {
        "entry_low": plan.entry_low, "entry_high": plan.entry_high,
        "stop": plan.stop, "target": plan.target,
        "risk_reward": plan.risk_reward,
        "expected_hold_days": plan.expected_hold_days,
        "invalidation": plan.invalidation,
        "invalidation_check": plan.invalidation_check,
        "direction": plan.direction,
        "created_at": plan.created_at.isoformat(timespec="seconds"),
        "expires_at": plan.expires_at.isoformat(timespec="seconds"),
        "sector": plan.sector,
    }


# --- 持續監控：舊計畫的失效與過期檢查 ----------------------------------------------


def monitor_pending_plans(cfg, run_id: str, offline: bool = False) -> list[dict]:
    """檢查 journal 裡仍在 APPROVE/WATCH 的舊計畫是否已失效/過期，
    是則寫入 INVALIDATED / EXPIRED 紀錄。回傳監控快照（給儀表板持倉監控區）。
    """
    from core.journal import make_journal_record, read_jsonl, write_journal
    from core.plan import check_invalidation, is_expired

    journal = read_jsonl(cfg.output.journal_path)
    outcomes = read_jsonl(cfg.output.outcomes_path)
    resolved_runs = {o.get("run_id") for o in outcomes}
    # 同一 run_id 的最新狀態
    latest: dict[str, dict] = {}
    for rec in journal:
        latest[rec["run_id"]] = rec

    snapshots = []
    for rid, rec in latest.items():
        if rec.get("status") not in PENDING_STATUSES or rid in resolved_runs:
            continue
        plan_d = rec.get("plan") or {}
        if not plan_d.get("invalidation_check"):
            continue
        try:
            expires = datetime.fromisoformat(plan_d["expires_at"])
        except (KeyError, ValueError):
            continue

        new_status, reason = None, ""
        if is_expired(expires):
            new_status, reason = "EXPIRED", f"計畫已逾期（expires_at={plan_d['expires_at']}）"
        else:
            entry = {"symbol": rec["symbol"], "market": rec["market"]}
            bars = _fetch(entry, cfg, offline)
            if not bars.empty:
                from core.plan import TradePlan
                from core.backtest import EdgeStats

                edge_d = rec.get("edge_stats") or {}
                stub = TradePlan(
                    symbol=rec["symbol"], market=rec["market"],
                    detector=rec.get("detector") or "", direction="long",
                    entry_low=plan_d.get("entry_low", 0), entry_high=plan_d.get("entry_high", 0),
                    stop=plan_d.get("stop", 0), target=plan_d.get("target", 0),
                    invalidation=plan_d.get("invalidation", ""),
                    invalidation_check=plan_d["invalidation_check"],
                    risk_reward=plan_d.get("risk_reward", 0),
                    expected_hold_days=plan_d.get("expected_hold_days", 0),
                    score=rec.get("score") or 0,
                    edge=EdgeStats(rec.get("detector") or "", rec["symbol"],
                                   edge_d.get("n", 0), edge_d.get("win_rate", 0),
                                   edge_d.get("expectancy_R", 0),
                                   edge_d.get("median_bars_held", 0),
                                   edge_d.get("in_sample_expectancy", 0),
                                   edge_d.get("holdout_expectancy", 0),
                                   edge_d.get("decayed", False)),
                    created_at=datetime.fromisoformat(plan_d["created_at"]),
                    expires_at=expires,
                )
                invalid, why = check_invalidation(stub, bars.df)
                if invalid:
                    new_status, reason = "INVALIDATED", why
                snapshots.append({"record": rec, "invalidated": invalid, "reason": why})

        if new_status:
            out = make_journal_record(
                run_id=rid, symbol=rec["symbol"], market=rec["market"],
                detector=rec.get("detector"), status=new_status, needs_human=False,
                score=rec.get("score"), plan=plan_d,
                data={"note": reason},
                blocking_reasons=[reason],
            )
            write_journal(cfg, out)
            log.info("%s %s → %s（%s）", rec["symbol"], rid, new_status, reason)
    return snapshots


# --- scan 主迴圈 -----------------------------------------------------------------


def cmd_scan(args) -> int:
    from core.backtest import get_edge
    from core.datasource import load_universe
    from core.journal import (build_portfolio, make_journal_record, new_run_id,
                              risk_to_dict, write_journal)
    from core.llm import apply_llm_verdict, qualitative_check
    from core.plan import build_plan
    from core.risk import assess, global_risk_state
    from core.signals import resolve_signals, run_detector, score_signal

    cfg = _load_cfg()
    offline = args.offline
    run_id = new_run_id()
    llm_enabled = cfg.llm.enabled and not args.no_llm

    # 2. 全域風控
    portfolio = build_portfolio(cfg)
    mode, mode_reasons = global_risk_state(portfolio, cfg)
    if mode == "observe":
        log.warning("全域觀察模式：%s", "; ".join(mode_reasons))

    # 3. 舊計畫失效/過期檢查
    monitor_snapshots = monitor_pending_plans(cfg, run_id, offline=offline)

    # 4-11. 逐標的處理
    universe = load_universe(cfg, offline=offline, market=args.market)
    if not universe:
        print("watchlist 為空（或該市場未啟用）", file=sys.stderr)
        return 1

    # 現有持倉的歷史資料（Gate 3 相關性用）
    universe_bars = {}
    for pos in portfolio.positions:
        b = _fetch({"symbol": pos.symbol, "market": pos.market}, cfg, offline)
        if not b.empty:
            universe_bars[pos.symbol] = b

    results = []          # (record, bars, plan, assessment) 給 report 用
    llm_calls = 0
    candidates_for_llm = []

    for entry in universe:
        symbol, market, sector = entry["symbol"], entry["market"], entry["sector"]
        try:
            rec = _scan_symbol(entry, cfg, offline, run_id, portfolio,
                               universe_bars, results)
        except Exception as e:   # 任何單一標的失敗都不得中斷整輪
            log.exception("%s 掃描失敗", symbol)
            rec = make_journal_record(
                run_id=run_id, symbol=symbol, market=market, detector=None,
                status="ABSTAIN", needs_human=True,
                data={"source": "n/a", "error": str(e)},
                blocking_reasons=[f"scan_error: {e}"],
            )
            results.append({"record": rec, "bars": None, "plan": None, "risk": None})

    # 10-11. LLM 定性層（只對已通過規則層與風控層的候選；只有否決權）
    scanned = [r for r in results if r["record"]["status"] in ("APPROVE", "WATCH")
               and r.get("risk") is not None and r["risk"].all_passed]
    scanned.sort(key=lambda r: -(r["record"]["score"] or 0))
    for r in scanned[: cfg.llm.max_candidates]:
        rec = r["record"]
        if not llm_enabled:
            rec["llm"] = {"enabled": False, "verdict": None,
                          "concerns": [], "unresolved_questions": []}
            continue
        note = qualitative_check(r["plan"], r["bars"], [], cfg,
                                 risk_summary=str(rec.get("risk", {})))
        llm_calls += 1
        if note is None:
            rec["llm"] = {"enabled": True, "verdict": None, "llm_unavailable": True,
                          "concerns": [], "unresolved_questions": []}
        else:
            new_status = apply_llm_verdict(rec["status"], note)
            rec["llm"] = {"enabled": True, "verdict": note.verdict,
                          "concerns": note.concerns,
                          "unresolved_questions": note.unresolved_questions,
                          "memo_text": note.memo_text}
            if new_status != rec["status"]:
                rec["blocking_reasons"] = rec["blocking_reasons"] + [f"llm_{note.verdict}"]
            rec["status"] = new_status

    # 全域觀察模式：所有 APPROVE 一律降為 WATCH
    if mode == "observe":
        for r in results:
            if r["record"]["status"] == "APPROVE":
                r["record"]["status"] = "WATCH"
                r["record"]["blocking_reasons"] = (
                    r["record"]["blocking_reasons"] + ["global_risk_observe_mode"]
                )

    # 12. 寫 journal
    for r in results:
        write_journal(cfg, r["record"])

    # 13. HTML 備忘錄
    from core.report import render_report

    html_path = render_report(
        cfg=cfg, run_id=run_id, results=results, portfolio=portfolio,
        mode=mode, mode_reasons=mode_reasons,
        monitor_snapshots=monitor_snapshots,
        universe_count=len(universe), llm_calls=llm_calls,
        llm_enabled=llm_enabled, universe_bars=universe_bars,
    )

    # 14. stdout 摘要
    counts = {"APPROVE": 0, "WATCH": 0, "REJECT": 0, "ABSTAIN": 0}
    for r in results:
        counts[r["record"]["status"]] = counts.get(r["record"]["status"], 0) + 1
    needs_human = sum(1 for r in results if r["record"]["needs_human"])
    print(f"run_id: {run_id}")
    print(f"{counts['APPROVE']} 核准 / {counts['WATCH']} 觀察 / "
          f"{counts['REJECT']} 拒絕 / {needs_human} 需人工確認")
    if mode == "observe":
        print(f"⚠ 全域觀察模式：{'; '.join(mode_reasons)}")
    print(f"HTML: {html_path}")
    return 0


def _scan_symbol(entry, cfg, offline, run_id, portfolio, universe_bars, results):
    """單一標的的規則層 → 回測 → 計畫 → 風控。將結果 append 進 results 並回傳 record。"""
    from core.backtest import get_edge
    from core.journal import make_journal_record, risk_to_dict
    from core.plan import build_plan
    from core.risk import assess
    from core.signals import resolve_signals, run_detector, score_signal

    symbol, market, sector = entry["symbol"], entry["market"], entry["sector"]

    def emit(record, bars=None, plan=None, risk=None, scored=None):
        results.append({"record": record, "bars": bars, "plan": plan,
                        "risk": risk, "scored": scored, "entry": entry})
        return record

    bars = _fetch(entry, cfg, offline)

    # 資料品質檢查 → ABSTAIN（原則 6）
    def abstain(reason):
        return emit(make_journal_record(
            run_id=run_id, symbol=symbol, market=market, detector=None,
            status="ABSTAIN", needs_human=True, data=_data_dict(bars),
            blocking_reasons=[reason]), bars=bars)

    if bars.empty:
        return abstain("data_source_failure")
    if len(bars.df) < cfg.data.min_bars_required:
        return abstain("insufficient_history")
    if bars.stale_days > cfg.data.max_staleness_days:
        return abstain("stale_data")
    if bars.df[["open", "high", "low", "close", "volume"]].isna().any().any():
        return abstain("data_gaps")

    # 5. 規則層：全部啟用的 detector（純 pandas，零成本）
    fired_names = []
    for name in cfg.signals.enabled_detectors:
        res = run_detector(bars.df, name, cfg)
        if bool(res.fired.iloc[-1]):
            fired_names.append(name)
    if not fired_names:
        return None   # 無訊號：不寫 journal（避免灌水，記於 NOTES.md）

    # 6-7. 回測 edge + 評分
    scored_list = []
    for name in fired_names:
        edge = get_edge(bars.df, name, cfg, symbol=symbol, market=market,
                        use_cache=not offline)
        scored_list.append(score_signal(bars.df, name, edge, cfg,
                                        symbol=symbol, market=market))
    best, conflict = resolve_signals(scored_list)
    if conflict:
        return emit(make_journal_record(
            run_id=run_id, symbol=symbol, market=market, detector=None,
            status="ABSTAIN", needs_human=True, data=_data_dict(bars),
            blocking_reasons=["conflicting_signals"]), bars=bars)

    edge_d = asdict(best.edge)
    breakdown = {"edge": best.edge_score, "setup": best.setup_score,
                 "edge_weight": cfg.signals.edge_weight,
                 "decayed": best.decayed, "capped_by_samples": best.capped_by_samples}

    if best.score < cfg.signals.watch_score:
        return emit(make_journal_record(
            run_id=run_id, symbol=symbol, market=market, detector=best.detector,
            status="REJECT", needs_human=False, score=best.score,
            score_breakdown=breakdown, edge_stats=edge_d, data=_data_dict(bars),
            blocking_reasons=["below_watch_score"]), bars=bars, scored=best)

    # 8. 交易計畫
    plan, reject_reason = build_plan(best, bars.df, cfg, sector=sector)
    if plan is None:
        return emit(make_journal_record(
            run_id=run_id, symbol=symbol, market=market, detector=best.detector,
            status="REJECT", needs_human=False, score=best.score,
            score_breakdown=breakdown, edge_stats=edge_d, data=_data_dict(bars),
            blocking_reasons=[reject_reason]), bars=bars, scored=best)

    # 9. 六道風控閘
    ra = assess(plan, portfolio, bars, universe_bars, cfg)
    gate_names_failed = [g.name for g in ra.gates if not g.passed]
    if not ra.all_passed and gate_names_failed != ["drawdown"]:
        status = "REJECT"       # 原則 3：任一閘失敗 → REJECT（全域 drawdown 閘除外，降 WATCH）
    elif best.score >= cfg.signals.approve_score:
        status = "APPROVE" if ra.all_passed else "WATCH"
    else:
        status = "WATCH"

    return emit(make_journal_record(
        run_id=run_id, symbol=symbol, market=market, detector=best.detector,
        status=status, needs_human=False, score=best.score,
        score_breakdown=breakdown, edge_stats=edge_d,
        plan=_plan_to_dict(plan), risk=risk_to_dict(ra), data=_data_dict(bars),
        blocking_reasons=ra.blocking_reasons),
        bars=bars, plan=plan, risk=ra, scored=best)


# --- 其他子指令 -------------------------------------------------------------------


def cmd_backtest(args) -> int:
    from core.backtest import backtest_detector

    cfg = _load_cfg()
    market = args.market or ("tw" if args.symbol.isdigit() else "us")
    bars = _fetch({"symbol": args.symbol, "market": market}, cfg, args.offline)
    if bars.empty:
        print(f"抓不到 {args.symbol} 的資料", file=sys.stderr)
        return 1
    print(f"{args.symbol}（{market}）: {len(bars.df)} 根 K，最後 {bars.df.index[-1].date()}")
    print(f"{'detector':<22}{'n':>4}{'win%':>8}{'expR':>8}{'in':>8}{'hold':>8}  decayed")
    for name in cfg.signals.enabled_detectors:
        e = backtest_detector(bars.df, name, cfg, symbol=args.symbol)
        print(f"{name:<22}{e.n:>4}{e.win_rate*100:>7.1f}%{e.expectancy_R:>8.2f}"
              f"{e.in_sample_expectancy:>8.2f}{e.holdout_expectancy:>8.2f}  {e.decayed}")
    return 0


def cmd_monitor(args) -> int:
    from core.journal import new_run_id

    cfg = _load_cfg()
    snaps = monitor_pending_plans(cfg, new_run_id(), offline=args.offline)
    if not snaps:
        print("沒有待監控的計畫")
    for s in snaps:
        rec = s["record"]
        print(f"{rec['symbol']} [{rec['run_id']}] "
              f"{'已失效' if s['invalidated'] else '有效'}：{s['reason']}")
    return 0


def cmd_close(args) -> int:
    from core.manage import close_holding

    cfg = _load_cfg()
    try:
        r = close_holding(cfg, args.run_id, args.exit_price,
                          exit_date=args.exit_date, reason=args.reason,
                          notes=args.notes)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"已回填 {r['symbol']}: exit {r['exit_price']} → "
          f"{r['r_multiple']:+.2f}R, pnl {r['pnl']:,.0f}")
    return 0


def cmd_serve(args) -> int:
    from core.webui import serve

    _load_cfg()   # 先驗證 config，錯誤直接退出
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_open(args) -> int:
    from core.journal import append_outcome, read_jsonl

    cfg = _load_cfg()
    journal = read_jsonl(cfg.output.journal_path)
    rec = next((r for r in journal if r["run_id"] == args.run_id and r.get("plan")), None)
    if rec is None:
        print(f"journal 找不到 run_id={args.run_id}", file=sys.stderr)
        return 1
    shares = args.shares or int((rec.get("risk") or {}).get("shares") or 0)
    append_outcome(cfg, {
        "run_id": args.run_id, "symbol": rec["symbol"], "state": "OPEN",
        "entry_price": args.entry_price, "shares": shares,
        "stop": float(rec["plan"]["stop"]),
        "sector": (rec.get("plan") or {}).get("sector", "unknown"),
        "opened_at": args.date or str(pd.Timestamp.today().date()),
    })
    print(f"已記錄 OPEN {rec['symbol']} @ {args.entry_price} x {shares}")
    return 0


def cmd_skip(args) -> int:
    from core.journal import append_outcome, read_jsonl

    cfg = _load_cfg()
    journal = read_jsonl(cfg.output.journal_path)
    rec = next((r for r in journal if r["run_id"] == args.run_id), None)
    if rec is None:
        print(f"journal 找不到 run_id={args.run_id}", file=sys.stderr)
        return 1
    append_outcome(cfg, {"run_id": args.run_id, "symbol": rec["symbol"],
                         "state": "SKIPPED", "notes": args.notes or ""})
    print(f"已記錄 SKIPPED {rec['symbol']}（事後可檢驗人為否決是否正確）")
    return 0


def cmd_stats(args) -> int:
    from core.journal import build_portfolio, read_jsonl

    cfg = _load_cfg()
    journal = read_jsonl(cfg.output.journal_path)
    outcomes = read_jsonl(cfg.output.outcomes_path)
    counts: dict[str, int] = {}
    for r in journal:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("journal 決策統計:", counts or "（空）")

    latest = {}
    for o in outcomes:
        latest[o.get("run_id")] = o
    closed = [o for o in latest.values() if o.get("state") == "CLOSED"]
    skipped = [o for o in latest.values() if o.get("state") == "SKIPPED"]
    opened = [o for o in latest.values() if o.get("state") == "OPEN"]
    print(f"outcomes: {len(closed)} 已平倉 / {len(opened)} 持倉中 / {len(skipped)} 核准未執行")
    if closed:
        rs = [float(o.get("r_multiple") or 0.0) for o in closed]
        pnls = [float(o.get("pnl") or 0.0) for o in closed]
        win = sum(1 for r in rs if r > 0) / len(rs)
        print(f"實際勝率 {win*100:.1f}% | 平均 {sum(rs)/len(rs):+.2f}R | "
              f"總損益 {sum(pnls):+,.0f}")
    pf = build_portfolio(cfg)
    print(f"權益曲線重建: 目前回撤 {pf.current_drawdown_pct:.2f}% | "
          f"當日已實現虧損 {pf.realized_loss_today:,.0f}")
    return 0


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="全天候 AI 交易員（不自動下單）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="跑一輪完整迴圈，產出 HTML + journal")
    sp.add_argument("--market", choices=["us", "tw"])
    sp.add_argument("--offline", action="store_true", help="用 synthetic provider")
    sp.add_argument("--no-llm", action="store_true", help="強制關閉定性層")
    sp.set_defaults(fn=cmd_scan)

    bp = sub.add_parser("backtest", help="單獨看某檔的 edge 統計")
    bp.add_argument("--symbol", required=True)
    bp.add_argument("--market", choices=["us", "tw"])
    bp.add_argument("--offline", action="store_true")
    bp.set_defaults(fn=cmd_backtest)

    mp = sub.add_parser("monitor", help="只檢查現有計畫是否失效/過期")
    mp.add_argument("--offline", action="store_true")
    mp.set_defaults(fn=cmd_monitor)

    cp = sub.add_parser("close", help="回填實際平倉結果")
    cp.add_argument("--run-id", required=True)
    cp.add_argument("--exit-price", type=float, required=True)
    cp.add_argument("--exit-date", required=True)
    cp.add_argument("--reason", default="")
    cp.add_argument("--notes", default="")
    cp.set_defaults(fn=cmd_close)

    op = sub.add_parser("open", help="記錄實際進場（建立 OPEN 持倉）")
    op.add_argument("--run-id", required=True)
    op.add_argument("--entry-price", type=float, required=True)
    op.add_argument("--shares", type=int)
    op.add_argument("--date")
    op.set_defaults(fn=cmd_open)

    kp = sub.add_parser("skip", help="記錄「系統核准但人未執行」")
    kp.add_argument("--run-id", required=True)
    kp.add_argument("--notes", default="")
    kp.set_defaults(fn=cmd_skip)

    st = sub.add_parser("stats", help="從 journal + outcomes 產出績效統計")
    st.set_defaults(fn=cmd_stats)

    sv = sub.add_parser("serve", help="啟動本機管理台（追蹤清單 / 庫存 / 掃描）")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--no-browser", action="store_true", help="不自動開瀏覽器")
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
