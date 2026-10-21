"""连读/吞音的音素级规则标注：CMUdict 发音词典 + 英语语流规则。

为什么不用 LLM：连读/吞音是确定性规则——知道相邻两词的首尾音素即可
判定。LLM 不做音素推理，实测准确率仅三四成且大量漏标（2026-10-21
spec-change）；本地规则准确、可单测、零 API 成本。

规则集（美音，面向唱歌教学，宁少勿滥——标错比少标更误导孩子）：

连读 liaison：
1. 尾辅音 + 头元音：辅音递过去（take‿it、each‿other、for‿us）
2. 尾前元音 + 头元音：插 /j/ 滑音（my‿age、I‿am）
3. 尾后元音 + 头元音：插 /w/ 滑音（how‿old、you‿are）

吞音 swallow：
1. 尾爆破音 + 下词辅音头：失去爆破（did‿not 的 d、take‿my 的 k）
2. 元音间且后随非重读元音的 t：美音闪音化（little 的 tt、water 的 t）

词典查不到的词不猜、不标。只处理句内相邻词对，不跨句。
"""
from __future__ import annotations

import re

import cmudict

# ARPABET 音素集（去掉重音数字后比较）
_VOWELS = {"AA", "AE", "AH", "AO", "EH", "EY", "IH", "IY", "ER", "UH", "UW", "AY", "AW", "OY", "OW"}
_FRONT_VOWELS = {"IY", "IH", "EY", "AY", "OY"}  # 后跟元音时插 /j/
_BACK_VOWELS = {"UW", "UH", "OW", "AW"}          # 后跟元音时插 /w/
_STOPS = {"P", "B", "T", "D", "K", "G"}
# 清辅音后所有格 's 读 /S/，其余读 /Z/
_VOICELESS = {"P", "T", "K", "F", "TH", "S", "SH", "CH", "HH"}
_STOP_LETTERS = {"P": "p", "B": "b", "T": "t", "D": "d", "K": "kc", "G": "g"}

_DICT = cmudict.dict()


def _phones(word: str) -> list[str] | None:
    """查词的音素序列（取首个发音）。所有格 's 词典一般没有，回落词干再补 S/Z。"""
    hit = _DICT.get(word)
    if hit:
        return hit[0]
    if word.endswith("'s"):
        base = _DICT.get(word[:-2])
        if base:
            phones = base[0]
            return phones + (["S"] if _base(phones[-1]) in _VOICELESS else ["Z"])
    return None


def _base(phone: str) -> str:
    return phone.rstrip("012")


def _stress(phone: str) -> int:
    m = re.search(r"[012]$", phone)
    return int(m.group()) if m else -1


def _is_vowel(phone: str) -> bool:
    return _base(phone) in _VOWELS


def _stop_letters(word: str, stop: str) -> str | None:
    """把尾爆破音素映射回拼写字母（take→k 先剥词尾哑 e）。映射不回去就不标。"""
    w = word[:-1] if word.endswith("e") and len(word) > 1 else word
    return w[-1] if w and w[-1] in _STOP_LETTERS[stop] else None


def _flap_t_letters(word: str, phones: list[str]) -> str | None:
    """元音间非重读 t（little、water）→ 返回拼写里的 tt/t；不满足返回 None。"""
    for i in range(1, len(phones) - 1):
        if (_base(phones[i]) == "T" and _is_vowel(phones[i - 1])
                and _stress(phones[i - 1]) > 0
                and _is_vowel(phones[i + 1]) and _stress(phones[i + 1]) == 0):
            # 拼写定位：t/tt 前面是元音字母即可（后面可能是 l，如 little）
            m = re.search(r"[aeiouy](tt|t)", word)
            return m.group(1) if m else None
    return None


def annotate_line(text: str) -> dict:
    """对一句歌词标注连读/吞音，返回 {"liaison": [[a, b]], "swallow": [{"word", "letters"}]}。

    输出词形为规范化小写（与前端 _normWord 对齐）；标注保证落在句内相邻词上。
    """
    tokens = [t for t in (re.sub(r"[^a-z0-9']+", "", w.lower()) for w in text.split()) if t]
    liaison: list[list[str]] = []
    swallow: list[dict] = []
    swallowed_idx: set[int] = set()

    for i in range(len(tokens) - 1):
        a, b = tokens[i], tokens[i + 1]
        pa, pb = _phones(a), _phones(b)
        if pa is None or pb is None:
            continue
        last, first = pa[-1], pb[0]
        last_base = _base(last)
        if _is_vowel(last) and _is_vowel(first):
            # ER 是 r 化元音，词尾 ER + 元音即 r 连读（other‿in）；
            # 前后元音有明确滑音的才标，其余元音相连不标（宁少勿滥）
            if last_base == "ER" or last_base in _FRONT_VOWELS or last_base in _BACK_VOWELS:
                liaison.append([a, b])
        elif not _is_vowel(last) and _is_vowel(first):
            liaison.append([a, b])
        elif last_base in _STOPS and i not in swallowed_idx:
            # 尾爆破音 + 辅音头 → 失去爆破
            letters = _stop_letters(a, last_base)
            if letters:
                swallow.append({"word": a, "letters": letters})
                swallowed_idx.add(i)

    for i, tok in enumerate(tokens):
        if i in swallowed_idx:
            continue
        phones = _phones(tok)
        if phones:
            letters = _flap_t_letters(tok, phones)
            if letters:
                swallow.append({"word": tok, "letters": letters})

    return {"liaison": liaison, "swallow": swallow}
