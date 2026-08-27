"""本機管理介面：追蹤清單、庫存登記/平倉、觸發掃描、看最新備忘錄。

只綁 127.0.0.1、純標準庫、無帳號驗證——僅供本機單人使用，不要對外開放。
"""
from __future__ import annotations

import html
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from core import manage
from core.config import ConfigError, load_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_FORM_BYTES = 64 * 1024

SCAN_STATE: dict = {"running": False, "market": "", "summary": "",
                    "returncode": None, "started_at": 0.0, "finished_at": 0.0}
_scan_lock = threading.Lock()


def _esc(x) -> str:
    return html.escape(str(x))


def _run_scan(market: str) -> None:
    args = [sys.executable, str(PROJECT_ROOT / "run.py"), "scan"]
    if market in ("us", "tw"):
        args += ["--market", market]
    try:
        proc = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True,
                              text=True, timeout=1800)
        tail = "\n".join((proc.stdout or "").strip().splitlines()[-4:])
        SCAN_STATE.update(returncode=proc.returncode,
                          summary=tail or (proc.stderr or "").strip()[-400:])
    except Exception as e:
        SCAN_STATE.update(returncode=-1, summary=f"掃描執行失敗: {e}")
    finally:
        SCAN_STATE["finished_at"] = time.time()
        SCAN_STATE["running"] = False


CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f9f9f7; color: #0b0b0b;
       font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 960px; margin: 0 auto; padding: 16px; display: flex;
        flex-direction: column; gap: 16px; }
.card { background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10);
        border-radius: 10px; padding: 14px; }
h1 { font-size: 18px; margin: 0; }
h2 { font-size: 15px; margin: 0 0 10px; }
table { width: 100%; border-collapse: collapse; font-size: 13px;
        font-variant-numeric: tabular-nums; }
th { text-align: left; color: #898781; font-weight: 500; }
th, td { padding: 5px 8px; border-bottom: 1px solid #e1e0d9; }
td.num, th.num { text-align: right; }
form.inline { display: inline; }
.row { display: flex; flex-wrap: wrap; gap: 8px; align-items: end; margin-top: 10px; }
.field { display: flex; flex-direction: column; gap: 2px; font-size: 12px; color: #52514e; }
input, select { font: inherit; padding: 5px 8px; border: 1px solid #c3c2b7;
        border-radius: 6px; background: #fff; width: 110px; }
input.wide { width: 150px; }
button { font: inherit; padding: 6px 14px; border: 1px solid #2a78d6;
         border-radius: 6px; background: #2a78d6; color: #fff; cursor: pointer; }
button:hover { background: #1c5cab; }
button.ghost { background: transparent; color: #2a78d6; }
button.danger { border-color: #d03b3b; background: transparent; color: #d03b3b;
                padding: 3px 10px; font-size: 12px; }
.msg { border-left: 4px solid #0ca30c; background: #eef6ee; padding: 8px 12px;
       border-radius: 0 8px 8px 0; }
.err { border-left: 4px solid #d03b3b; background: #faeceb; padding: 8px 12px;
       border-radius: 0 8px 8px 0; }
.muted { color: #898781; font-size: 12px; }
.topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px;
          flex-wrap: wrap; }
a { color: #2a78d6; } a:hover { color: #1c5cab; }
button:disabled { background: #898781; border-color: #898781; cursor: progress; }
.spinner { display: inline-block; width: 11px; height: 11px; margin-right: 7px;
           vertical-align: -1px; border: 2px solid rgba(255,255,255,0.45);
           border-top-color: #fff; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
"""


def render_page(cfg, msg: str = "", err: str = "") -> str:
    wl = manage.load_watchlist_raw(cfg)
    holdings = manage.list_holdings(cfg)
    memos = sorted(Path(cfg.output.html_dir).glob("memo_*.html"))
    latest_memo = memos[-1].name if memos else None

    h = ['<!DOCTYPE html><html lang="zh-Hant"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         '<title>AI 交易員 管理台</title>', f"<style>{CSS}</style></head><body>",
         '<div class="wrap">']

    h.append('<div class="card topbar"><h1>AI 交易員 管理台</h1><div>')
    if latest_memo:
        h.append('<a href="/memo" target="_blank">開啟最新備忘錄</a>　')
    if SCAN_STATE["running"]:
        started = SCAN_STATE.get("started_at") or time.time()
        h.append(f'<button disabled id="scanbtn" data-started="{started:.0f}">'
                 f'<span class="spinner"></span>掃描中 <span id="elapsed">0:00</span></button>')
    else:
        h.append('<form class="inline" method="post" action="/scan">'
                 '<select name="market"><option value="">全市場</option>'
                 '<option value="us">只掃美股</option><option value="tw">只掃台股</option>'
                 '</select> <button>跑一輪掃描</button></form>')
    h.append("</div></div>")

    if msg:
        h.append(f'<div class="msg">{_esc(msg)}</div>')
    if err:
        h.append(f'<div class="err">{_esc(err)}</div>')
    if not SCAN_STATE["running"] and SCAN_STATE["summary"]:
        ok = SCAN_STATE["returncode"] == 0
        took = SCAN_STATE.get("finished_at", 0) - SCAN_STATE.get("started_at", 0)
        took_txt = f"（耗時 {int(took // 60)} 分 {int(took % 60)} 秒）" if took > 0 else ""
        h.append(f'<div class="{"msg" if ok else "err"}">'
                 f'<b>{"上次掃描完成" if ok else "上次掃描失敗"}</b>'
                 f'<span class="muted">{took_txt}</span><br>'
                 f'<pre style="margin:4px 0; white-space:pre-wrap">{_esc(SCAN_STATE["summary"])}</pre></div>')

    # 追蹤清單
    h.append('<div class="card"><h2>追蹤清單</h2><table>'
             '<tr><th>代號</th><th>名稱</th><th>市場</th><th>產業</th><th></th></tr>')
    for market in ("tw", "us"):
        for e in wl[market]:
            h.append(
                f'<tr><td><b>{_esc(e["symbol"])}</b></td><td>{_esc(e["name"])}</td>'
                f'<td>{market.upper()}</td><td>{_esc(e["sector"])}</td>'
                f'<td style="text-align:right"><form class="inline" method="post" action="/watchlist/remove">'
                f'<input type="hidden" name="symbol" value="{_esc(e["symbol"])}">'
                f'<button class="danger">移除</button></form></td></tr>')
    h.append('</table>'
             '<form method="post" action="/watchlist/add"><div class="row">'
             '<label class="field">代號（台股數字 / 美股字母）<input name="symbol" required placeholder="2603 或 TSLA"></label>'
             '<label class="field">名稱（選填）<input name="name" placeholder="長榮"></label>'
             '<label class="field">產業（選填）<input name="sector" placeholder="shipping"></label>'
             '<button>加入追蹤</button></div></form>'
             '<div class="muted">市場依代號自動判斷；產業用於相關性閘的 fallback，同產業持倉會互相計數。</div>'
             "</div>")

    # 庫存
    h.append('<div class="card"><h2>庫存</h2>')
    if holdings:
        h.append('<table><tr><th>代號</th><th class="num">進場價</th><th class="num">停損</th>'
                 '<th class="num">股數</th><th class="num">風險金額</th><th>進場日</th>'
                 '<th class="num">平倉價</th><th></th></tr>')
        for p in holdings:
            h.append(
                f'<tr><td><b>{_esc(p.symbol)}</b> <span class="muted">{_esc(p.sector)}</span></td>'
                f'<td class="num">{p.entry_price:,.2f}</td><td class="num">{p.stop:,.2f}</td>'
                f'<td class="num">{p.shares:,}</td><td class="num">{p.risk_amount:,.0f}</td>'
                f'<td>{p.opened_at}</td>'
                f'<td class="num" colspan="2"><form class="inline" method="post" action="/holding/close">'
                f'<input type="hidden" name="run_id" value="{_esc(p.run_id)}">'
                f'<input name="exit_price" required placeholder="價格" style="width:90px"> '
                f'<button class="danger">平倉</button></form></td></tr>')
        h.append("</table>")
    else:
        h.append('<div class="muted">目前沒有登記中的持倉。</div>')
    h.append('<form method="post" action="/holding/add"><div class="row">'
             '<label class="field">代號<input name="symbol" required placeholder="2330"></label>'
             '<label class="field">進場價<input name="entry_price" required placeholder="1050"></label>'
             '<label class="field">股數<input name="shares" required placeholder="1000"></label>'
             '<label class="field">停損（必填）<input name="stop" required placeholder="980"></label>'
             '<label class="field">產業（選填）<input name="sector" placeholder="semiconductor"></label>'
             '<label class="field">進場日（選填）<input name="opened_at" placeholder="2026-08-20"></label>'
             '<button>登記持倉</button></div></form>'
             '<div class="muted">登記後即納入風控：總曝險（Gate 2）、相關性（Gate 3）、'
             '儀表板持倉監控都會計入。平倉會自動算 R 與損益寫入 outcomes。</div>'
             "</div>")

    if SCAN_STATE["running"]:
        h.append("""<script>
(function () {
  var btn = document.getElementById('scanbtn');
  if (!btn) return;
  var started = parseInt(btn.dataset.started, 10) * 1000;
  function tick() {
    var s = Math.max(0, Math.floor((Date.now() - started) / 1000));
    var el = document.getElementById('elapsed');
    if (el) el.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  tick();
  setInterval(tick, 1000);
  setInterval(function () {
    fetch('/status', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (!j.running) location.reload(); })
      .catch(function () {});
  }, 2000);
})();
</script>""")

    h.append('<div class="muted">此介面只在本機（127.0.0.1）提供服務。'
             '追蹤清單寫入 watchlist.yaml、庫存寫入 data/outcomes.jsonl。</div>')
    h.append("</div></body></html>")
    return "".join(h)


class Handler(BaseHTTPRequestHandler):
    server_version = "AITraderManage/1.0"

    def log_message(self, fmt, *args):  # 安靜一點
        pass

    def _redirect(self, msg: str = "", err: str = ""):
        q = []
        if msg:
            q.append("msg=" + quote(msg))
        if err:
            q.append("err=" + quote(err))
        self.send_response(303)
        self.send_header("Location", "/" + ("?" + "&".join(q) if q else ""))
        self.end_headers()

    def _html(self, body: str, status: int = 200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_FORM_BYTES:
            raise ValueError("表單過大")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return {k: v[0].strip() for k, v in parse_qs(raw).items()}

    def do_GET(self):
        url = urlparse(self.path)
        cfg = load_config(PROJECT_ROOT / "config.yaml")
        if url.path == "/":
            q = parse_qs(url.query)
            self._html(render_page(cfg, msg=q.get("msg", [""])[0], err=q.get("err", [""])[0]))
        elif url.path == "/status":
            import json as _json

            body = _json.dumps({
                "running": bool(SCAN_STATE["running"]),
                "returncode": SCAN_STATE["returncode"],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif url.path == "/memo":
            memos = sorted(Path(cfg.output.html_dir).glob("memo_*.html"))
            if memos:
                self._html(memos[-1].read_text(encoding="utf-8"))
            else:
                self._html("<p>還沒有備忘錄，先跑一輪掃描。</p>", 404)
        else:
            self._html("<p>not found</p>", 404)

    def do_POST(self):
        cfg = load_config(PROJECT_ROOT / "config.yaml")
        try:
            form = self._form()
            if self.path == "/watchlist/add":
                e = manage.add_symbol(cfg, form.get("symbol", ""),
                                      sector=form.get("sector", ""),
                                      name=form.get("name", ""))
                self._redirect(msg=f'已加入追蹤：{e["symbol"]}（{e["market"].upper()}）')
            elif self.path == "/watchlist/remove":
                found = manage.remove_symbol(cfg, form.get("symbol", ""))
                self._redirect(msg="已移除" if found else "清單裡沒有這個代號")
            elif self.path == "/holding/add":
                r = manage.add_holding(cfg, form.get("symbol", ""),
                                       form.get("entry_price", "0"),
                                       form.get("shares", "0"),
                                       form.get("stop", "0"),
                                       sector=form.get("sector", ""),
                                       opened_at=form.get("opened_at", ""))
                self._redirect(msg=f'已登記持倉：{r["symbol"]} {r["shares"]} 股 @ {r["entry_price"]}')
            elif self.path == "/holding/close":
                r = manage.close_holding(cfg, form.get("run_id", ""),
                                         form.get("exit_price", "0"))
                self._redirect(msg=f'已平倉 {r["symbol"]}：{r["r_multiple"]:+.2f}R，'
                                   f'損益 {r["pnl"]:,.0f}')
            elif self.path == "/scan":
                with _scan_lock:
                    if SCAN_STATE["running"]:
                        self._redirect(err="掃描已在進行中")
                        return
                    SCAN_STATE.update(running=True, summary="", returncode=None,
                                      market=form.get("market", ""),
                                      started_at=time.time(), finished_at=0.0)
                threading.Thread(target=_run_scan, args=(form.get("market", ""),),
                                 daemon=True).start()
                self._redirect(msg="掃描已啟動，完成後重新整理本頁即可看到摘要")
            else:
                self._html("<p>not found</p>", 404)
        except (ValueError, ConfigError) as e:
            self._redirect(err=str(e))
        except Exception as e:
            self._redirect(err=f"操作失敗：{e}")


def serve(port: int = 8787, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"管理台已啟動：{url}（Ctrl+C 結束）")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
