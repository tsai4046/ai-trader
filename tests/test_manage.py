"""追蹤清單與庫存管理邏輯測試（含網頁 render 冒煙測試）。"""
import shutil
from pathlib import Path

import pytest

from core import manage
from core.journal import build_portfolio, read_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mcfg(cfg, tmp_path):
    """隔離的 config：watchlist 與 outcomes/journal 都指到暫存目錄。"""
    c = cfg.model_copy(deep=True)
    wl = tmp_path / "watchlist.yaml"
    shutil.copy(PROJECT_ROOT / "watchlist.yaml", wl)
    c.markets.us.symbols_from = str(wl)
    c.output.journal_path = str(tmp_path / "journal.jsonl")
    c.output.outcomes_path = str(tmp_path / "outcomes.jsonl")
    return c


def test_add_symbol_infers_market_and_persists(mcfg):
    e = manage.add_symbol(mcfg, "2603", sector="shipping", name="長榮")
    assert e["market"] == "tw"
    e2 = manage.add_symbol(mcfg, "tsla")
    assert e2["market"] == "us" and e2["symbol"] == "TSLA"
    wl = manage.load_watchlist_raw(mcfg)
    assert any(x["symbol"] == "2603" and x["name"] == "長榮" for x in wl["tw"])
    assert any(x["symbol"] == "TSLA" for x in wl["us"])


def test_add_duplicate_symbol_rejected(mcfg):
    with pytest.raises(ValueError):
        manage.add_symbol(mcfg, "2330")   # 已在預設 watchlist


def test_bad_symbol_rejected(mcfg):
    for bad in ["", "A B", "x" * 20, "a/b"]:
        with pytest.raises(ValueError):
            manage.add_symbol(mcfg, bad)


def test_remove_symbol(mcfg):
    assert manage.remove_symbol(mcfg, "2330") is True
    assert manage.remove_symbol(mcfg, "2330") is False
    assert all(e["symbol"] != "2330" for e in manage.load_watchlist_raw(mcfg)["tw"])


def test_add_holding_requires_valid_stop(mcfg):
    with pytest.raises(ValueError):
        manage.add_holding(mcfg, "2330", 1000, 1000, stop=1000)   # stop >= entry
    with pytest.raises(ValueError):
        manage.add_holding(mcfg, "2330", 1000, 1000, stop=0)
    with pytest.raises(ValueError):
        manage.add_holding(mcfg, "2330", 1000, 0, stop=950)       # 0 股


def test_manual_holding_enters_portfolio(mcfg):
    r = manage.add_holding(mcfg, "2330", 1050, 2000, stop=980,
                           sector="semiconductor", opened_at="2026-08-20")
    assert r["run_id"].startswith("manual-")
    pf = build_portfolio(mcfg)
    assert len(pf.positions) == 1
    p = pf.positions[0]
    assert p.symbol == "2330" and p.market == "tw"
    assert p.risk_amount == pytest.approx((1050 - 980) * 2000)
    assert p.sector == "semiconductor"


def test_close_manual_holding_computes_r_and_pnl(mcfg):
    r = manage.add_holding(mcfg, "2330", 1000, 1000, stop=950)
    closed = manage.close_holding(mcfg, r["run_id"], 1100, exit_date="2026-09-01")
    assert closed["r_multiple"] == pytest.approx(2.0)     # (1100-1000)/(1000-950)
    assert closed["pnl"] == pytest.approx(100000)
    assert build_portfolio(mcfg).positions == []          # 平倉後移出持倉
    states = [o["state"] for o in read_jsonl(mcfg.output.outcomes_path)]
    assert states == ["OPEN", "CLOSED"]


def test_close_unknown_run_id_raises(mcfg):
    with pytest.raises(ValueError):
        manage.close_holding(mcfg, "no-such-id", 100)


def test_render_page_smoke(mcfg):
    from core.webui import render_page

    manage.add_holding(mcfg, "2330", 1050, 2000, stop=980)
    html = render_page(mcfg, msg="測試訊息")
    for token in ("追蹤清單", "庫存", "2330", "NVDA", "測試訊息", "跑一輪掃描"):
        assert token in html
    assert "http://" not in html and "https://" not in html.replace(
        "https://127.0.0.1", "")   # 頁面不引用外部資源
