"""逐句翻译（LLM）+ 连读/吞音标注（本地规则引擎）。

翻译仍走 LLM：整首歌一次调用（儿歌几十句，逐句调用慢且贵；整歌一次还能
让模型看到上下文），结果落盘缓存，同一首歌不重复调 API。

连读/吞音已改本地音素级规则引擎（见 phon_rules.py，2026-10-21 spec-change）：
LLM 不做音素推理，实测准确率仅三四成；规则是确定性的，准确、可测、零成本。
没配 API key 时仍产出标注，翻译留空。
"""
from __future__ import annotations

import json

from . import llm, phon_rules


class AnnotateError(Exception):
    """标注失败（LLM 返回缺句等），message 面向用户。"""


PROMPT_TEMPLATE = """你是儿童英语唱歌课的老师。下面是一首英文歌的歌词，共 {n} 句，已按顺序编号。

请把每一句翻译成中文：给一年级小朋友看，要简短、口语化。

只输出 JSON，不要输出任何其他文字。格式：
{{"lines": [{{"index": 1, "zh": "中文翻译"}}]}}

要求：每一句都要有，index 与输入编号一致；不漏句、不加句。

歌词：
{numbered_lyrics}"""


def build_prompt(texts: list[str]) -> str:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
    return PROMPT_TEMPLATE.format(n=len(texts), numbered_lyrics=numbered)


def parse_response(text: str, n_lines: int) -> dict[int, str]:
    """解析 LLM 翻译返回 {index: zh}；缺句超过一半视为整体失败。"""
    try:
        items = json.loads(text)["lines"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise AnnotateError(f"LLM 返回的不是预期 JSON：{e}") from e

    result: dict[int, str] = {}
    for item in items:
        try:
            idx, zh = int(item["index"]), str(item["zh"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= idx <= n_lines and zh:
            result[idx] = zh

    if len(result) * 2 < n_lines:
        raise AnnotateError(f"LLM 翻译缺句太多（{n_lines} 句只译出 {len(result)} 句），请重试")
    return result


def _translate(texts: list[str]) -> dict[int, str]:
    """整歌一次调用拿逐句翻译；一次格式重试后仍失败则报错。"""
    prompt = build_prompt(texts)
    last_error = None
    for attempt_prompt in (prompt, prompt + "\n\n注意：你上一次的输出不符合要求，这次请严格只输出规定的 JSON。"):
        try:
            return parse_response(llm.chat(attempt_prompt), len(texts))
        except AnnotateError as e:
            last_error = e
    raise last_error


def annotate_lines(lines: list[dict]) -> None:
    """对歌曲的逐句列表做翻译+标注，就地写入每行的 zh / liaison / swallow 字段。

    连读/吞音永远由本地规则引擎产出；翻译仅在有 API key 时请求 LLM，
    否则保留行内已有翻译（没有则留空）。
    """
    texts = [l["text"] for l in lines]
    translations = _translate(texts) if llm.configured() else {}
    for line in lines:
        line["zh"] = translations.get(line["index"], line.get("zh", ""))
        marks = phon_rules.annotate_line(line["text"])
        line["liaison"] = marks["liaison"]
        line["swallow"] = marks["swallow"]
