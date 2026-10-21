"""连读/吞音标注：本地音素级规则引擎（见 phon_rules.py）。

2026-10-21 spec-change：翻译改随歌词粘贴导入（中英混贴，见 aligner.parse_lyrics），
LLM 接入整体移除；本模块只剩规则引擎标注——毫秒级纯本地计算，同步调用即可。
"""
from __future__ import annotations

from . import phon_rules


def annotate_lines(lines: list[dict]) -> None:
    """对逐句列表做连读/吞音标注，就地写入每行的 liaison / swallow 字段。"""
    for line in lines:
        marks = phon_rules.annotate_line(line["text"])
        line["liaison"] = marks["liaison"]
        line["swallow"] = marks["swallow"]
