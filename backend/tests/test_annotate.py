"""annotate 规则引擎标注的就地写入测试。

逐句规则细节在 test_phon_rules.py；中英混贴歌词解析在 test_aligner.py。
"""
from tinker.annotate import annotate_lines


def test_annotate_lines_writes_marks_in_place():
    lines = [
        {"index": 1, "text": "But I know the sun will shine for us"},
        {"index": 2, "text": "Twinkle twinkle little star"},
    ]
    annotate_lines(lines)
    # but‿I 是尾爆破音+元音头的连读，规则引擎必标
    assert ["but", "i"] in lines[0]["liaison"]
    for line in lines:
        assert "liaison" in line and "swallow" in line
