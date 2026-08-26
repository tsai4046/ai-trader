"""單一自包含 HTML 儀表板。CSS/JS 全部 inline、圖表用 inline SVG，不依賴外部資源。

配色與圖表規格依 dataviz skill 的 reference palette（雙主題 token）。
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

STATUS_LABEL = {"APPROVE": "核准", "WATCH": "觀察", "REJECT": "拒絕", "ABSTAIN": "需要人工確認"}
SPARK_DAYS = 120

CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --page: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --s1: #2a78d6; --s3: #1baf7a; --s7: #4a3aa7;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
  --good-text: #006300;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface: #1a1a19; --page: #0d0d0d;
    --ink: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --s1: #3987e5; --s3: #199e70; --s7: #9085e9;
    --good-text: #0ca30c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --page: #0d0d0d;
  --ink: #ffffff; --ink-2: #c3c2b7; --ink-muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
  --s1: #3987e5; --s3: #199e70; --s7: #9085e9;
  --good-text: #0ca30c;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 1280px; margin: 0 auto; padding: 16px; }
.statusbar { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 24px; align-items: center; }
.statusbar.alert { border: 2px solid var(--critical); background: color-mix(in srgb, var(--critical) 8%, var(--surface)); }
.stat { min-width: 130px; }
.stat .k { color: var(--ink-muted); font-size: 12px; }
.stat .v { font-size: 20px; font-weight: 650; }
.meter { height: 6px; background: var(--grid); border-radius: 4px; margin-top: 4px; overflow: hidden; }
.meter > i { display: block; height: 100%; border-radius: 4px; background: var(--s1); }
.meter > i.hot { background: var(--critical); }
.globalflag { font-weight: 650; }
.globalflag.bad { color: var(--critical); }
.cols { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 16px; margin-top: 16px; }
.col h2 { font-size: 15px; margin: 0 0 8px; display: flex; gap: 8px; align-items: center; }
.count { font-size: 12px; background: var(--grid); border-radius: 10px; padding: 1px 9px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px; margin-bottom: 12px; }
.card h3 { margin: 0; font-size: 15px; display: flex; justify-content: space-between; align-items: baseline; }
.badge { font-size: 11px; color: var(--ink-2); background: var(--grid); padding: 1px 8px; border-radius: 8px; }
.score { font-size: 22px; font-weight: 700; }
.scorebar { display: flex; height: 8px; border-radius: 4px; overflow: hidden; margin: 6px 0 2px; background: var(--grid); }
.scorebar .edge { background: var(--s1); }
.scorebar .setup { background: var(--s3); margin-left: 2px; }
.scorelegend { font-size: 11px; color: var(--ink-muted); display: flex; gap: 12px; }
.swatch { width: 8px; height: 8px; display: inline-block; border-radius: 2px; margin-right: 4px; }
table.mini { width: 100%; border-collapse: collapse; font-size: 12.5px; margin: 8px 0;
  font-variant-numeric: tabular-nums; }
table.mini td, table.mini th { padding: 3px 6px; border-bottom: 1px solid var(--grid); text-align: right; }
table.mini th { color: var(--ink-muted); font-weight: 500; text-align: left; }
.warnflag { color: var(--serious); font-weight: 650; font-size: 12px; }
.spark { margin: 8px 0; }
.invalid-box { border-left: 4px solid var(--s7); background: color-mix(in srgb, var(--s7) 7%, var(--surface));
  padding: 8px 10px; border-radius: 0 8px 8px 0; margin: 8px 0; font-size: 12.5px; }
.invalid-box .t { color: var(--s7); font-weight: 650; font-size: 11px; letter-spacing: .04em; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 8px 0; }
.chip { font-size: 11px; padding: 2px 9px; border-radius: 10px; border: 1px solid var(--border); }
.chip.pass { color: var(--good-text); background: color-mix(in srgb, var(--good) 10%, var(--surface)); }
.chip.fail { color: var(--critical); background: color-mix(in srgb, var(--critical) 10%, var(--surface)); font-weight: 650; }
.llmnote { font-size: 12.5px; color: var(--ink-2); border-top: 1px dashed var(--grid); padding-top: 8px; margin-top: 8px; }
.abstain { margin-top: 20px; background: var(--surface); border: 1px solid var(--serious);
  border-radius: 10px; padding: 14px; }
.abstain h2 { margin: 0 0 8px; font-size: 15px; color: var(--serious); }
.section { margin-top: 20px; background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
.section h2 { margin: 0 0 8px; font-size: 15px; }
.footer { margin-top: 20px; color: var(--ink-muted); font-size: 12px; display: flex; flex-wrap: wrap; gap: 18px; }
.empty { color: var(--ink-muted); font-size: 13px; padding: 12px; }
.reasons { font-size: 12px; color: var(--ink-2); }
"""


def _esc(x) -> str:
    return html.escape(str(x))


def _fmt(x, nd=2):
    if x is None:
        return "—"
    try:
        return f"{float(x):,.{nd}f}"
    except (TypeError, ValueError):
        return _esc(x)


def _sparkline(closes: list[float], plan: dict | None,
               width=300, height=96) -> str:
    """120 日收盤 sparkline，疊上進場區間 / 停損 / 目標三組水平線。"""
    if not closes or len(closes) < 2:
        return ""
    levels = []
    if plan:
        levels = [plan.get("entry_low"), plan.get("entry_high"),
                  plan.get("stop"), plan.get("target")]
        levels = [float(v) for v in levels if v is not None]
    lo = min(min(closes), *levels) if levels else min(closes)
    hi = max(max(closes), *levels) if levels else max(closes)
    pad = (hi - lo) * 0.06 or 1.0
    lo, hi = lo - pad, hi + pad

    def y(v):
        return round(height - (v - lo) / (hi - lo) * height, 2)

    def x(i):
        return round(i / (len(closes) - 1) * width, 2)

    pts = " ".join(f"{x(i)},{y(v)}" for i, v in enumerate(closes))
    parts = [f'<svg class="spark" width="100%" viewBox="0 0 {width} {height + 14}" '
             f'role="img" aria-label="120 日價格走勢">']
    # 進場區間帶
    if plan:
        e_lo, e_hi = float(plan["entry_low"]), float(plan["entry_high"])
        parts.append(f'<rect x="0" y="{y(e_hi)}" width="{width}" '
                     f'height="{max(2, y(e_lo) - y(e_hi)):.2f}" fill="var(--s3)" opacity="0.18"/>')
        for v, color, dash in ((float(plan["stop"]), "var(--critical)", "4 3"),
                               (float(plan["target"]), "var(--good)", "4 3")):
            parts.append(f'<line x1="0" x2="{width}" y1="{y(v)}" y2="{y(v)}" '
                         f'stroke="{color}" stroke-width="1.5" stroke-dasharray="{dash}"/>')
    parts.append(f'<polyline points="{pts}" fill="none" stroke="var(--s1)" '
                 f'stroke-width="2" stroke-linejoin="round"/>')
    if plan:
        parts.append(
            f'<text x="0" y="{height + 11}" font-size="9" fill="var(--ink-muted)">'
            f'— 價格　▨ 進場區間　╌ 停損（紅）/ 目標（綠）</text>')
    parts.append("</svg>")
    return "".join(parts)


def _card(r, cfg) -> str:
    rec = r["record"]
    plan = rec.get("plan")
    risk = rec.get("risk")
    edge = rec.get("edge_stats") or {}
    bd = rec.get("score_breakdown") or {}
    ew = bd.get("edge_weight", cfg.signals.edge_weight)
    edge_part = ew * (bd.get("edge") or 0)
    setup_part = (1 - ew) * (bd.get("setup") or 0)

    out = [f'<div class="card">']
    out.append(
        f'<h3><span>{_esc(rec["symbol"])} <span class="badge">{_esc(rec["market"]).upper()}'
        f'</span> <span class="badge">{_esc(rec.get("detector") or "—")}</span></span>'
        f'<span class="score">{rec.get("score") if rec.get("score") is not None else "—"}</span></h3>')
    # 分數堆疊條：edge vs setup 貢獻
    out.append(
        f'<div class="scorebar"><span class="edge" style="width:{edge_part:.0f}%"></span>'
        f'<span class="setup" style="width:{setup_part:.0f}%"></span></div>'
        f'<div class="scorelegend"><span><i class="swatch" style="background:var(--s1)"></i>'
        f'edge {_fmt(bd.get("edge"), 0)}×{ew:.1f}</span>'
        f'<span><i class="swatch" style="background:var(--s3)"></i>'
        f'setup {_fmt(bd.get("setup"), 0)}×{1 - ew:.1f}</span></div>')

    if edge:
        warn = ""
        if edge.get("n", 0) < cfg.signals.min_samples:
            warn += f' <span class="warnflag">⚠ 樣本不足 n&lt;{cfg.signals.min_samples}（上限45分）</span>'
        if edge.get("decayed"):
            warn += ' <span class="warnflag">⚠ holdout 衰退</span>'
        out.append(
            f'<table class="mini"><tr><th>回測</th><th>n</th><th>勝率</th><th>期望值</th>'
            f'<th>in-sample</th><th>holdout</th></tr>'
            f'<tr><td style="text-align:left">{_esc(edge.get("detector", ""))}</td>'
            f'<td>{edge.get("n", 0)}</td><td>{_fmt((edge.get("win_rate") or 0) * 100, 1)}%</td>'
            f'<td>{_fmt(edge.get("expectancy_R"))}R</td>'
            f'<td>{_fmt(edge.get("in_sample_expectancy"))}R</td>'
            f'<td>{_fmt(edge.get("holdout_expectancy"))}R</td></tr></table>'
            + (f'<div>{warn}</div>' if warn else ""))

    closes = []
    if r.get("bars") is not None and not getattr(r["bars"], "empty", True):
        closes = [float(v) for v in r["bars"].df["close"].tail(SPARK_DAYS)]
    if closes:
        out.append(_sparkline(closes, plan))

    if plan:
        shares = (risk or {}).get("shares")
        out.append(
            f'<table class="mini">'
            f'<tr><th>進場區間</th><td>{_fmt(plan["entry_low"])} ~ {_fmt(plan["entry_high"])}</td>'
            f'<th>停損</th><td>{_fmt(plan["stop"])}</td></tr>'
            f'<tr><th>目標</th><td>{_fmt(plan["target"])}</td>'
            f'<th>RR</th><td>{_fmt(plan["risk_reward"])}</td></tr>'
            f'<tr><th>建議股數</th><td>{shares if shares is not None else "—"}</td>'
            f'<th>部位市值</th><td>{_fmt((risk or {}).get("position_value"), 0)}</td></tr>'
            f'<tr><th>風險金額</th><td>{_fmt((risk or {}).get("risk_amount"), 0)}</td>'
            f'<th>預計持有</th><td>{plan.get("expected_hold_days", "—")} 天</td></tr>'
            f'<tr><th>計畫失效日</th><td colspan="3">{_esc(str(plan.get("expires_at", ""))[:10])}</td></tr>'
            f'</table>')
        out.append(
            f'<div class="invalid-box"><div class="t">失效條件（≠ 停損：邏輯前提不成立就出場）</div>'
            f'{_esc(plan.get("invalidation", ""))}</div>')

    if risk and risk.get("gates"):
        chips = []
        for g in risk["gates"]:
            cls = "pass" if g["passed"] else "fail"
            label = _esc(g["name"])
            if not g["passed"]:
                label += f'：{_fmt(g.get("value"))} / 限 {_fmt(g.get("limit"))}'
            chips.append(f'<span class="chip {cls}" title="{_esc(g.get("detail", ""))}">'
                         f'{"✓" if g["passed"] else "✗"} {label}</span>')
        out.append(f'<div class="chips">{"".join(chips)}</div>')

    if rec.get("blocking_reasons"):
        out.append(f'<div class="reasons">阻擋原因：{_esc("、".join(rec["blocking_reasons"]))}</div>')

    llm = rec.get("llm") or {}
    if not llm.get("enabled"):
        out.append('<div class="llmnote">定性層未啟用</div>')
    elif llm.get("llm_unavailable"):
        out.append('<div class="llmnote">⚠ LLM 呼叫失敗（llm_unavailable），維持規則層判定</div>')
    elif llm.get("verdict"):
        concerns = "".join(f"<li>{_esc(c)}</li>" for c in llm.get("concerns", []))
        questions = "".join(f"<li>{_esc(q)}</li>" for q in llm.get("unresolved_questions", []))
        out.append(
            f'<div class="llmnote"><b>定性層判定：{_esc(llm["verdict"])}</b>'
            f'<p>{_esc(llm.get("memo_text", ""))}</p>'
            + (f'<div>疑慮：<ul>{concerns}</ul></div>' if concerns else "")
            + (f'<div>未確認問題：<ul>{questions}</ul></div>' if questions else "")
            + "</div>")
    out.append("</div>")
    return "".join(out)


def render_report(*, cfg, run_id: str, results: list[dict], portfolio,
                  mode: str, mode_reasons: list[str], monitor_snapshots: list[dict],
                  universe_count: int, llm_calls: int, llm_enabled: bool,
                  universe_bars: dict | None = None) -> str:
    now = datetime.now().astimezone()
    by_status: dict[str, list] = {"APPROVE": [], "WATCH": [], "REJECT": [], "ABSTAIN": []}
    for r in results:
        by_status.setdefault(r["record"]["status"], []).append(r)

    exposure = sum(p.risk_amount for p in portfolio.positions)
    exposure_pct = exposure / portfolio.equity * 100 if portfolio.equity else 0.0
    exposure_frac = min(1.0, exposure_pct / cfg.risk.max_total_risk_pct)
    alert = mode == "observe"

    h = ['<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f'<title>AI 交易員決策備忘錄 {_esc(run_id)}</title>',
         f"<style>{CSS}</style></head><body><div class='wrap'>"]

    # 頂部狀態列
    state_txt = ("觀察模式（暫停新開倉）" if alert else "正常")
    h.append(f'<div class="statusbar{" alert" if alert else ""}">')
    h.append(f'<div class="stat"><div class="k">執行時間</div>'
             f'<div class="v" style="font-size:15px">{now.strftime("%Y-%m-%d %H:%M %Z")}</div></div>')
    h.append(f'<div class="stat"><div class="k">帳戶淨值（{_esc(cfg.account.currency)}）</div>'
             f'<div class="v">{portfolio.equity:,.0f}</div></div>')
    h.append(f'<div class="stat"><div class="k">總曝險 / 上限 {cfg.risk.max_total_risk_pct}%</div>'
             f'<div class="v">{exposure_pct:.1f}%</div>'
             f'<div class="meter"><i class="{"hot" if exposure_frac > 0.8 else ""}" '
             f'style="width:{exposure_frac * 100:.0f}%"></i></div></div>')
    h.append(f'<div class="stat"><div class="k">當前回撤</div>'
             f'<div class="v">{portfolio.current_drawdown_pct:.1f}%</div></div>')
    h.append(f'<div class="stat"><div class="k">全域狀態</div>'
             f'<div class="v globalflag{" bad" if alert else ""}">{state_txt}</div>'
             + (f'<div class="reasons">{_esc("; ".join(mode_reasons))}</div>' if mode_reasons else "")
             + "</div>")
    h.append("</div>")

    # 三欄卡片
    dot = {"APPROVE": "var(--good)", "WATCH": "var(--warning)", "REJECT": "var(--critical)"}
    h.append('<div class="cols">')
    for st in ("APPROVE", "WATCH", "REJECT"):
        items = by_status.get(st, [])
        h.append(f'<div class="col"><h2><span class="dot" style="background:{dot[st]}"></span>'
                 f'{STATUS_LABEL[st]} <span class="count">{len(items)}</span></h2>')
        if items:
            for r in sorted(items, key=lambda r: -(r["record"].get("score") or 0)):
                h.append(_card(r, cfg))
        else:
            h.append('<div class="card empty">（本輪無）</div>')
        h.append("</div>")
    h.append("</div>")

    # ABSTAIN 區塊
    abstains = by_status.get("ABSTAIN", [])
    h.append('<div class="abstain"><h2>⚠ 需要人工確認（ABSTAIN）</h2>')
    if abstains:
        h.append('<table class="mini"><tr><th>標的</th><th>市場</th><th>原因</th><th>資料源</th></tr>')
        for r in abstains:
            rec = r["record"]
            h.append(f'<tr><th>{_esc(rec["symbol"])}</th>'
                     f'<td>{_esc(rec["market"]).upper()}</td>'
                     f'<td style="text-align:left">{_esc("、".join(rec["blocking_reasons"]))}</td>'
                     f'<td>{_esc((rec.get("data") or {}).get("source", "—"))}</td></tr>')
        h.append("</table>")
    else:
        h.append('<div class="empty">本輪無資料不足 / 訊號衝突 / 資料源故障的標的。</div>')
    h.append("</div>")

    # 持倉監控區
    h.append('<div class="section"><h2>持倉與待執行計畫監控</h2>')
    rows = []
    ub = universe_bars or {}
    for p in portfolio.positions:
        last = None
        b = ub.get(p.symbol)
        if b is not None and not b.empty:
            last = float(b.df["close"].iloc[-1])
        dist = (f"{(last - p.stop) / last * 100:.1f}%" if last and last > 0 else "—")
        held = (datetime.now().date() - p.opened_at).days
        rows.append(f'<tr><th>{_esc(p.symbol)}（持倉）</th><td>{_fmt(p.entry_price)}</td>'
                    f'<td>{_fmt(p.stop)}</td><td>{dist}</td><td>{held} 天</td></tr>')
    for s in monitor_snapshots:
        rec = s["record"]
        plan = rec.get("plan") or {}
        badge = "已失效" if s["invalidated"] else "有效"
        rows.append(f'<tr><th>{_esc(rec["symbol"])}（計畫 {_esc(rec["status"])}）</th>'
                    f'<td>{_fmt(plan.get("entry_low"))}~{_fmt(plan.get("entry_high"))}</td>'
                    f'<td>{_fmt(plan.get("stop"))}</td>'
                    f'<td colspan="2" style="text-align:left">{badge}：{_esc(s["reason"])}</td></tr>')
    if rows:
        h.append('<table class="mini"><tr><th>標的</th><th>進場</th><th>停損</th>'
                 '<th>距停損</th><th>已持有 / 狀態</th></tr>' + "".join(rows) + "</table>")
    else:
        h.append('<div class="empty">目前無持倉、也無待執行計畫。</div>')
    h.append("</div>")

    # 策略評估排行（有跑過 evaluate 才顯示）
    from core.evaluate import load_evaluation

    evaluation = load_evaluation()
    if evaluation and evaluation.get("detectors"):
        h.append('<div class="section"><h2>策略評估排行'
                 f'<span style="font-weight:400; font-size:12px; color:var(--ink-muted)">'
                 f'　walk-forward {evaluation.get("n_folds", "?")} 段驗證 · '
                 f'{_esc(str(evaluation.get("evaluated_at", ""))[:16])}</span></h2>')
        h.append('<table class="mini"><tr><th>detector</th><th>標的/穩健</th><th>n</th>'
                 '<th>期望值</th><th>勝率</th><th>時段穩定度</th><th>分數</th><th>推薦</th></tr>')
        for d in evaluation["detectors"]:
            badge = ('<span style="color:var(--good-text); font-weight:650">✔ 推薦</span>'
                     if d.get("recommended") else
                     '<span style="color:var(--ink-muted)">未過門檻</span>')
            h.append(
                f'<tr><th>{_esc(d["detector"])}</th>'
                f'<td>{d["symbols_evaluated"]} / {d["symbols_robust"]}</td>'
                f'<td>{d["pooled_n"]}</td><td>{d["pooled_expectancy"]:.2f}R</td>'
                f'<td>{d["pooled_win_rate"]*100:.1f}%</td>'
                f'<td>{d["stability"]*100:.0f}%</td><td>{d["score"]:.3f}</td>'
                f'<td>{badge}</td></tr>')
        h.append('</table><div class="reasons">分數 = 收縮後期望值 × 時段穩定度；'
                 '「推薦」需同時滿足樣本數、正期望、多數時段為正、holdout 未衰退。'
                 '啟用與否由 config 的 enabled_detectors 決定，不會自動改變。</div></div>')

    # 底部
    signals_n = sum(1 for r in results if r["record"].get("detector"))
    sources = sorted({(r["record"].get("data") or {}).get("source") or "n/a" for r in results})
    h.append(f'<div class="footer"><span>掃描標的 {universe_count}</span>'
             f'<span>觸發訊號 {signals_n}</span>'
             f'<span>LLM 呼叫 {llm_calls} 次（定性層{"啟用" if llm_enabled else "未啟用"}）</span>'
             f'<span>資料源：{_esc(", ".join(sources))}</span>'
             f'<span>run_id: {_esc(run_id)}</span></div>')
    h.append("</div></body></html>")

    out_dir = Path(cfg.output.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"memo_{now.strftime('%Y%m%d_%H%M')}.html"
    path.write_text("".join(h), encoding="utf-8")
    return str(path)


def render_evaluation_report(cfg, evaluation: dict) -> str:
    """獨立的策略評估報告：detector 排行 + 每檔標的的分段細節。"""
    now = datetime.now().astimezone()
    h = ['<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         '<title>策略評估報告</title>', f"<style>{CSS}</style></head><body>",
         "<div class='wrap'>"]
    h.append('<div class="statusbar"><div class="stat"><div class="k">策略評估報告</div>'
             f'<div class="v" style="font-size:15px">{_esc(str(evaluation.get("evaluated_at", ""))[:16])}'
             f'　walk-forward {evaluation.get("n_folds")} 段</div></div>'
             f'<div class="stat"><div class="k">評估範圍</div>'
             f'<div class="v" style="font-size:15px">{_esc(", ".join(evaluation.get("universe", [])))}</div></div></div>')

    h.append('<div class="section"><h2>Detector 排行</h2>'
             '<table class="mini"><tr><th>detector</th><th>標的/穩健</th><th>n</th>'
             '<th>期望值</th><th>勝率</th><th>每檔中位數</th><th>時段穩定度</th>'
             '<th>分數</th><th>推薦</th></tr>')
    for d in evaluation.get("detectors", []):
        badge = ('<span style="color:var(--good-text); font-weight:650">✔</span>'
                 if d.get("recommended") else '—')
        h.append(f'<tr><th>{_esc(d["detector"])}</th>'
                 f'<td>{d["symbols_evaluated"]} / {d["symbols_robust"]}</td>'
                 f'<td>{d["pooled_n"]}</td><td>{d["pooled_expectancy"]:.2f}R</td>'
                 f'<td>{d["pooled_win_rate"]*100:.1f}%</td>'
                 f'<td>{d["median_symbol_expectancy"]:.2f}R</td>'
                 f'<td>{d["stability"]*100:.0f}%</td><td>{d["score"]:.3f}</td>'
                 f'<td>{badge}</td></tr>')
    h.append('</table></div>')

    h.append('<div class="section"><h2>每檔明細</h2>'
             '<table class="mini"><tr><th>標的</th><th>detector</th><th>n</th>'
             '<th>期望值</th><th>勝率</th><th>正分段</th><th>穩健</th>'
             '<th style="text-align:left">未過門檻原因</th></tr>')
    per = sorted(evaluation.get("per_symbol", []),
                 key=lambda e: (-int(e.get("robust", False)), -e.get("expectancy", 0)))
    for e in per:
        h.append(f'<tr><th>{_esc(e["symbol"])}</th><td>{_esc(e["detector"])}</td>'
                 f'<td>{e["n"]}</td><td>{e["expectancy"]:.2f}R</td>'
                 f'<td>{e["win_rate"]*100:.1f}%</td>'
                 f'<td>{e["positive_folds"]}/{e["active_folds"]}</td>'
                 f'<td>{"✔" if e["robust"] else "—"}</td>'
                 f'<td style="text-align:left">{_esc("、".join(e.get("reasons", [])) or "—")}</td></tr>')
    h.append('</table></div>')
    if evaluation.get("skipped"):
        h.append('<div class="abstain"><h2>略過</h2><table class="mini">')
        for s in evaluation["skipped"]:
            h.append(f'<tr><th>{_esc(s["symbol"])}</th>'
                     f'<td style="text-align:left">{_esc(s["reason"])}</td></tr>')
        h.append('</table></div>')
    h.append('<div class="footer"><span>回測皆為單標的、無成本、無滑價的簡化模擬；'
             '評估用於淘汰不穩健策略，不保證未來有效。</span></div>')
    h.append("</div></body></html>")

    out_dir = Path(cfg.output.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"evaluate_{now.strftime('%Y%m%d_%H%M')}.html"
    path.write_text("".join(h), encoding="utf-8")
    return str(path)
