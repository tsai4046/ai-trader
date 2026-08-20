"""端到端：run.py scan --offline 完整跑通 + journal schema + HTML 自包含。"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def workdir(tmp_path_factory):
    """把專案跑在隔離目錄，避免污染 repo 的 data/ 與 out/。"""
    wd = tmp_path_factory.mktemp("e2e")
    shutil.copy(PROJECT_ROOT / "config.yaml", wd / "config.yaml")
    shutil.copy(PROJECT_ROOT / "watchlist.yaml", wd / "watchlist.yaml")
    return wd


def run_cli(workdir, *args):
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "run.py"), *args],
        cwd=workdir, capture_output=True, text=True, env=env,
    )


@pytest.fixture(scope="module")
def scan_result(workdir):
    return run_cli(workdir, "scan", "--offline")


def test_offline_scan_exits_zero(scan_result):
    assert scan_result.returncode == 0, scan_result.stderr


def test_scan_prints_summary(scan_result):
    assert re.search(r"\d+ 核准 / \d+ 觀察 / \d+ 拒絕 / \d+ 需人工確認",
                     scan_result.stdout)


def test_journal_written_and_schema_valid(workdir, scan_result):
    from core.journal import validate_journal_record

    jpath = workdir / "data" / "journal.jsonl"
    assert jpath.exists()
    records = [json.loads(l) for l in jpath.read_text(encoding="utf-8").splitlines() if l]
    assert records, "journal 至少要有一筆"
    for rec in records:
        validate_journal_record(rec)   # jsonschema 驗證
    statuses = {r["status"] for r in records}
    assert "ABSTAIN" in statuses       # SYN_SHORT → insufficient_history
    abstain = next(r for r in records if r["status"] == "ABSTAIN")
    assert abstain["needs_human"] is True


def test_html_written_and_self_contained(workdir, scan_result):
    html_files = sorted((workdir / "out").glob("memo_*.html"))
    assert html_files, "必須產出 HTML 備忘錄"
    html = html_files[-1].read_text(encoding="utf-8")
    # 可被解析
    from html.parser import HTMLParser

    class P(HTMLParser):
        tags = 0
        def handle_starttag(self, tag, attrs):
            self.tags += 1
            for k, v in attrs:
                if k in ("src", "href") and v and v.startswith(("http:", "https:", "//")):
                    raise AssertionError(f"外部資源引用: {tag} {k}={v}")

    p = P()
    p.feed(html)
    assert p.tags > 50
    assert "http://" not in html and "https://" not in html
    # 三欄 + ABSTAIN 區塊齊全
    for token in ("核准", "觀察", "拒絕", "需要人工確認", "失效條件", "run_id"):
        assert token in html


def test_scan_capped_signal_stays_watch(workdir, scan_result):
    # SYN_PULLBACK n<min_samples → 分數上限 45 → 最多 WATCH，不可能 APPROVE
    jpath = workdir / "data" / "journal.jsonl"
    records = [json.loads(l) for l in jpath.read_text(encoding="utf-8").splitlines() if l]
    pull = [r for r in records if r["symbol"] == "SYN_PULLBACK"]
    assert pull
    assert all(r["status"] != "APPROVE" for r in pull)
    assert all((r.get("score") or 0) <= 45 for r in pull)


def test_open_close_stats_roundtrip(workdir, scan_result):
    # checkpoint 15：回填 outcomes 並重建權益曲線
    jpath = workdir / "data" / "journal.jsonl"
    records = [json.loads(l) for l in jpath.read_text(encoding="utf-8").splitlines() if l]
    rec = next(r for r in records if r.get("plan") and r["status"] in ("APPROVE", "WATCH"))
    rid = rec["run_id"]

    r1 = run_cli(workdir, "open", "--run-id", rid, "--entry-price",
                 str(rec["plan"]["entry_high"]))
    assert r1.returncode == 0, r1.stderr

    exit_price = rec["plan"]["target"]
    r2 = run_cli(workdir, "close", "--run-id", rid, "--exit-price", str(exit_price),
                 "--exit-date", "2026-09-01", "--reason", "target")
    assert r2.returncode == 0, r2.stderr

    r3 = run_cli(workdir, "stats")
    assert r3.returncode == 0, r3.stderr
    assert "1 已平倉" in r3.stdout
    assert "權益曲線重建" in r3.stdout

    outcomes = [json.loads(l) for l in
                (workdir / "data" / "outcomes.jsonl").read_text(encoding="utf-8").splitlines() if l]
    closed = [o for o in outcomes if o["state"] == "CLOSED"]
    assert closed and closed[-1]["r_multiple"] > 0


def test_monitor_command_runs(workdir, scan_result):
    r = run_cli(workdir, "monitor", "--offline")
    assert r.returncode == 0, r.stderr


def test_missing_config_field_exits_nonzero(workdir, tmp_path):
    import yaml

    broken_dir = tmp_path / "broken"
    broken_dir.mkdir()
    raw = yaml.safe_load((PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8"))
    del raw["risk"]["max_drawdown_pct"]
    (broken_dir / "config.yaml").write_text(yaml.dump(raw), encoding="utf-8")
    shutil.copy(PROJECT_ROOT / "watchlist.yaml", broken_dir / "watchlist.yaml")
    r = run_cli(broken_dir, "scan", "--offline")
    assert r.returncode != 0
    assert "config" in (r.stderr + r.stdout)
