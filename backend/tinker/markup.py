"""连读/吞音标注的标记文本：自动生成与用户编辑校验。

标记语法（面向用户，也是 lines[*].marked 的唯一存储形态）：
- 连读：两词之间用 - 连接（But-I know the sun will shine for-us）
- 吞音：被吞的字母用 ^ 包裹（I wan^t^ you to kee^p^ your dream）

2026-10-23 spec-change：每句只存 marked 标记文本，废弃 liaison/swallow
结构化字段——单一事实源，前端渲染时现场解析，不存在两处漂移。
本模块只负责"规则引擎结果 → 标记文本"的生成与保存前校验；
标记文本 → 高亮渲染的解析在前端（renderLineHtml）。
"""
from __future__ import annotations

import re

# 与 phon_rules 的词形规范化保持一致：小写、只留字母数字与所有格 '
_norm = lambda w: re.sub(r"[^a-z0-9']+", "", w.lower())  # noqa: E731


def to_markup(text: str, marks: dict) -> str:
    """把规则引擎标注（{"liaison": [[a,b]], "swallow": [{word, letters}]}）
    叠回原句文本，生成标记文本。词对/词的出现匹配规则与前端渲染一致：
    取第一个未占用的同名词，保证同一文本喂回去渲染出的高亮不变。
    """
    words = text.split()

    # 吞音：记下每个词下标要包的字母
    swallow_at: dict[int, str] = {}
    for s in marks.get("swallow", []):
        for i, w in enumerate(words):
            if i not in swallow_at and _norm(w) == _norm(s["word"]):
                swallow_at[i] = s["letters"]
                break

    # 连读：记下每对相邻词的左词下标
    liaison_from: set[int] = set()
    for a, b in marks.get("liaison", []):
        for i in range(len(words) - 1):
            if (i not in liaison_from and i + 1 not in liaison_from
                    and _norm(words[i]) == _norm(a) and _norm(words[i + 1]) == _norm(b)):
                liaison_from.add(i)
                break

    def wrap(i: int) -> str:
        w = words[i]
        if i not in swallow_at:
            return w
        letters = swallow_at[i]
        pos = w.lower().find(letters.lower())
        if pos < 0:
            return w
        return w[:pos] + "^" + w[pos:pos + len(letters)] + "^" + w[pos + len(letters):]

    tokens = []
    i = 0
    while i < len(words):
        if i in liaison_from and i + 1 < len(words):
            tokens.append(wrap(i) + "-" + wrap(i + 1))
            i += 2
        else:
            tokens.append(wrap(i))
            i += 1
    return " ".join(tokens)


def _words(text: str) -> list[str]:
    """把（可能带标记的）文本展平成规范化词序列：空格分词后再按 - 拆开
    （连读标记），剥掉 ^（吞音标记），最后规范化。原句与标记文本过同一个
    函数，词序列才可比较——原文自带的连字符（merry-go-round）也因此无害。
    """
    out = []
    for tok in text.split():
        for piece in tok.split("-"):
            w = _norm(piece.replace("^", ""))
            if w:
                out.append(w)
    return out


def validate_markup(original: str, marked: str) -> str | None:
    """校验用户编辑的标记文本：剥掉 - 与 ^ 标记后，词序列必须与原句
    完全一致——只能增删标记，不能改歌词本身。通过返回 None，否则返回给用户
    看的错误原因。
    """
    if not marked.strip():
        return "标注不能清空哦——不想要标记的话，留着原句不改动就好"

    orig, mine = _words(original), _words(marked)
    if len(orig) != len(mine):
        return ("词的数量和原句对不上——只能用 - 和 ^ 加标记，不能增删歌词哦"
                f"（原句 {len(orig)} 个词，你写的是 {len(mine)} 个）")

    for i, (o, m) in enumerate(zip(orig, mine)):
        if o != m:
            return (f"第 {i + 1} 个词对不上：原句是「{o}」，你写的是「{m}」——"
                    "只能加标记，不能改歌词哦")
    return None
