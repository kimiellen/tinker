"""逐句翻译 + 连读/吞音标注：整首歌一次 LLM 调用。

为什么是整歌一次而不是逐句调用：儿歌几十句，逐句调用慢且贵；整歌一次
还能让模型看到上下文。结果（含翻译和标注）直接写进歌曲的 lines 里，
加上 llm.py 的磁盘缓存，同一首歌不会重复调 API。

标注是"按规则预标注"（spec 决策）：让 LLM 按英语发音规则从文本推断，
不做音频实测。规则上宁少勿滥——标错比少标更误导孩子。
"""
from __future__ import annotations

import json
import re

from . import llm


class AnnotateError(Exception):
    """标注失败（LLM 返回缺句等），message 面向用户。"""


PROMPT_TEMPLATE = """你是儿童英语唱歌课的老师。下面是一首英文歌的歌词，共 {n} 句，已按顺序编号。

请对每一句做三件事：
1. 翻译成中文：给一年级小朋友看，要简短、口语化。
2. 标注连读 liaison：相邻两个词在自然语流里应该连起来读（如前词尾辅音接后词头元音）。只标最典型、对孩子学唱最有帮助的。
3. 标注吞音 swallow：单词里在自然语流中几乎不发音的字母（如 little 里的 t）。

只输出 JSON，不要输出任何其他文字。格式：
{{"lines": [{{"index": 1, "zh": "中文翻译", "liaison": [["词1", "词2"]], "swallow": [{{"word": "little", "letters": "t"}}]}}]}}

要求：
- 每一句都要有，index 与输入编号一致；不漏句、不加句。
- liaison 每项是两个相邻词的原文（去掉标点），必须在这句里相邻出现。
- swallow 的 word 是句中原词，letters 是被吞掉的字母，必须出现在 word 里。
- 拿不准就不标，宁少勿滥。

歌词：
{numbered_lyrics}"""


def _norm_word(w: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", w.lower())


def build_prompt(texts: list[str]) -> str:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts, 1))
    return PROMPT_TEMPLATE.format(n=len(texts), numbered_lyrics=numbered)


def parse_response(text: str, texts: list[str]) -> dict[int, dict]:
    """解析 LLM 返回，校验标注确实落在原句里（LLM 会数错词，错的标注直接丢弃）。

    返回 {index: {"zh", "liaison", "swallow"}}；缺句超过一半视为整体失败。
    """
    try:
        data = json.loads(text)
        items = data["lines"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise AnnotateError(f"LLM 返回的不是预期 JSON：{e}") from e

    tokens_by_line = {i: [_norm_word(w) for w in t.split()] for i, t in enumerate(texts, 1)}
    result: dict[int, dict] = {}
    for item in items:
        try:
            idx = int(item["index"])
            zh = str(item["zh"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if idx not in tokens_by_line or not zh:
            continue
        tokens = tokens_by_line[idx]
        liaison = []
        for pair in item.get("liaison") or []:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            a, b = str(pair[0]), str(pair[1])
            if _adjacent(tokens, _norm_word(a), _norm_word(b)):
                liaison.append([a, b])
        swallow = []
        for s in item.get("swallow") or []:
            if not isinstance(s, dict):
                continue
            word, letters = str(s.get("word", "")), str(s.get("letters", ""))
            if word and letters and _norm_word(word) in tokens and letters.lower() in word.lower():
                swallow.append({"word": word, "letters": letters})
        result[idx] = {"zh": zh, "liaison": liaison, "swallow": swallow}

    if len(result) * 2 < len(texts):
        raise AnnotateError(f"LLM 翻译缺句太多（{len(texts)} 句只译出 {len(result)} 句），请重试")
    return result


def _adjacent(tokens: list[str], a: str, b: str) -> bool:
    return any(tokens[i] == a and tokens[i + 1] == b for i in range(len(tokens) - 1))


def annotate_lines(lines: list[dict]) -> None:
    """对歌曲的逐句列表做翻译+标注，就地写入每行的 zh / liaison / swallow 字段。"""
    texts = [l["text"] for l in lines]
    prompt = build_prompt(texts)
    parsed = None
    last_error = None
    for attempt_prompt in (prompt, prompt + "\n\n注意：你上一次的输出不符合要求，这次请严格只输出规定的 JSON。"):
        try:
            parsed = parse_response(llm.chat(attempt_prompt), texts)
            break
        except AnnotateError as e:
            last_error = e
    if parsed is None:
        raise last_error

    for line in lines:
        got = parsed.get(line["index"])
        line["zh"] = got["zh"] if got else ""
        line["liaison"] = got["liaison"] if got else []
        line["swallow"] = got["swallow"] if got else []
