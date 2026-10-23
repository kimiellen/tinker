"""annotate 规则引擎标注的就地写入测试。

逐句规则细节在 test_phon_rules.py；中英混贴歌词解析在 test_aligner.py；
标记文本的生成与校验在 test_markup.py。
"""
from tinker.annotate import annotate_lines


def test_annotate_lines_writes_marked_in_place():
    lines = [
        {"index": 1, "text": "But I know the sun will shine for us"},
        {"index": 2, "text": "Twinkle twinkle little star"},
    ]
    annotate_lines(lines)
    # but‿I / for‿us 是尾辅音+元音头的连读，规则引擎必标；lit‿tle 的 tt 闪音化必吞
    assert lines[0]["marked"] == "But-I know the sun will shine for-us"
    assert lines[1]["marked"] == "Twinkle twinkle li^tt^le star"
