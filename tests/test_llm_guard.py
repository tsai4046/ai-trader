"""原則 4：LLM 只有否決權，沒有升級權。這個檔案的測試不准刪。"""
import pytest

from core.llm import QualitativeNote, _parse_response, apply_llm_verdict


def note(verdict):
    return QualitativeNote(verdict=verdict, concerns=[], unresolved_questions=[],
                           memo_text="")


def test_ok_cannot_upgrade_watch_to_approve():
    # LLM 回 ok 而規則層是 WATCH → 結果仍是 WATCH（不得升級）
    assert apply_llm_verdict("WATCH", note("ok")) == "WATCH"


def test_ok_keeps_approve():
    assert apply_llm_verdict("APPROVE", note("ok")) == "APPROVE"


def test_reject_overrides_approve():
    assert apply_llm_verdict("APPROVE", note("reject")) == "REJECT"


def test_downgrade_applies_only_to_approve():
    assert apply_llm_verdict("APPROVE", note("downgrade_to_watch")) == "WATCH"
    assert apply_llm_verdict("WATCH", note("downgrade_to_watch")) == "WATCH"
    assert apply_llm_verdict("REJECT", note("downgrade_to_watch")) == "REJECT"


def test_none_keeps_rule_status():
    # LLM 失敗 / 未啟用 → 維持規則層判定
    for status in ("APPROVE", "WATCH", "REJECT"):
        assert apply_llm_verdict(status, None) == status


def test_parse_valid_json():
    n = _parse_response('{"verdict": "ok", "concerns": ["a"], '
                        '"unresolved_questions": [], "memo_text": "x"}')
    assert n is not None and n.verdict == "ok" and n.concerns == ["a"]


def test_parse_garbage_returns_none():
    assert _parse_response("not json at all") is None
    assert _parse_response('{"verdict": "approve_harder"}') is None   # 非法 verdict
    assert _parse_response('{"no_verdict": true}') is None


def test_qualitative_check_disabled_returns_none(cfg):
    from core.llm import qualitative_check

    assert cfg.llm.enabled is False
    assert qualitative_check(None, None, [], cfg) is None
