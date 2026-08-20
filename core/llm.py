"""定性層（可選，預設關閉）。

原則 4：LLM 只有否決權，沒有升級權 — apply_llm_verdict 寫死此規則，
test_llm_guard.py 守住，不准刪。
呼叫失敗 / 逾時 / 格式錯 → 回傳 None，維持規則層判定（llm_unavailable）。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

LLM_TIMEOUT_SECONDS = 60
VALID_VERDICTS = ("ok", "downgrade_to_watch", "reject")


@dataclass
class QualitativeNote:
    verdict: Literal["ok", "downgrade_to_watch", "reject"]
    concerns: list[str]
    unresolved_questions: list[str]
    memo_text: str


def apply_llm_verdict(rule_status: str, note: QualitativeNote | None) -> str:
    """LLM 只准降級（APPROVE→WATCH / 任何→REJECT），不准升級。"""
    if note is None:
        return rule_status
    if note.verdict == "reject":
        return "REJECT"
    if note.verdict == "downgrade_to_watch":
        return "WATCH" if rule_status == "APPROVE" else rule_status
    return rule_status        # 'ok' 不做任何提升


def _build_prompt(plan, bars, news: list, risk_summary: str) -> str:
    news_txt = "\n".join(f"- {n}" for n in news) if news else "（v1 未接新聞源，無新聞輸入）"
    return f"""你是交易計畫的定性審查員。你只能否決或放行，不能調整任何數值。

計畫摘要：
- 標的: {plan.symbol}（{plan.market}），型態: {plan.detector}，方向: long
- 進場區間: {plan.entry_low} ~ {plan.entry_high}，停損: {plan.stop}，目標: {plan.target}
- RR: {plan.risk_reward}，預計持有: {plan.expected_hold_days} 天，總分: {plan.score}
- 失效條件: {plan.invalidation}
- 回測: n={plan.edge.n}, 勝率={plan.edge.win_rate}, 期望值={plan.edge.expectancy_R}R, decayed={plan.edge.decayed}

風控結果：{risk_summary}

近期新聞標題：
{news_txt}

請檢視這份計畫本身有沒有內部矛盾、有沒有明顯被忽略的風險。
只回傳一個 JSON object（不要 markdown code fence）：
{{"verdict": "ok" | "downgrade_to_watch" | "reject",
  "concerns": ["..."],
  "unresolved_questions": ["..."],
  "memo_text": "給人看的備忘錄段落（繁體中文，3-5 句）"}}"""


def _parse_response(text: str) -> QualitativeNote | None:
    """嚴格解析 + fallback None。不准 eval()（陷阱 #8）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("verdict") not in VALID_VERDICTS:
        return None
    return QualitativeNote(
        verdict=obj["verdict"],
        concerns=[str(c) for c in obj.get("concerns") or []],
        unresolved_questions=[str(q) for q in obj.get("unresolved_questions") or []],
        memo_text=str(obj.get("memo_text") or ""),
    )


def qualitative_check(plan, bars, news: list, cfg,
                      risk_summary: str = "") -> QualitativeNote | None:
    """只對已通過規則層與風控層的候選呼叫。任何失敗 → None（維持規則層判定）。"""
    if not cfg.llm.enabled:
        return None
    api_key = os.environ.get(cfg.llm.api_key_env, "")
    if not api_key:
        log.warning("LLM 已啟用但缺少 %s，跳過定性層", cfg.llm.api_key_env)
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key, timeout=LLM_TIMEOUT_SECONDS)
        resp = client.messages.create(
            model=cfg.llm.model,
            max_tokens=1024,
            messages=[{"role": "user",
                       "content": _build_prompt(plan, bars, news, risk_summary)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse_response(text)
    except Exception as e:
        log.error("LLM 呼叫失敗（%s），維持規則層判定", e)
        return None
