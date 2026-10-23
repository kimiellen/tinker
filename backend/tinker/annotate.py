"""连读/吞音标注：本地音素级规则引擎（见 phon_rules.py）。

2026-10-21 spec-change：翻译改随歌词粘贴导入（中英混贴，见 aligner.parse_lyrics），
LLM 接入整体移除；本模块只剩规则引擎标注——毫秒级纯本地计算，同步调用即可。

2026-10-23 spec-change：标注落盘形态改为标记文本（marked 字段，语法见 markup.py），
废弃 liaison/swallow 结构化字段；用户可在学习页逐句手动修正（edit-markup）。
"""
from __future__ import annotations

from . import markup, phon_rules


def annotate_line(line: dict) -> None:
    """对单行歌词做连读/吞音标注，就地写入 marked 标记文本。"""
    line["marked"] = markup.to_markup(line["text"], phon_rules.annotate_line(line["text"]))


def annotate_lines(lines: list[dict]) -> None:
    """对逐句列表做连读/吞音标注，就地写入每行的 marked 标记文本。"""
    for line in lines:
        annotate_line(line)
