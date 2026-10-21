"""annotate 解析与校验测试（不触碰真实 LLM）。"""
import json

import pytest

from tinker.annotate import AnnotateError, build_prompt, parse_response

TEXTS = ["Twinkle, twinkle, little star", "How I wonder what you are"]


def make_response(lines):
    return json.dumps({"lines": lines}, ensure_ascii=False)


def test_parse_happy_path():
    resp = make_response([
        {"index": 1, "zh": "一闪一闪小星星",
         "liaison": [["twinkle", "twinkle"]],
         "swallow": [{"word": "little", "letters": "t"}]},
        {"index": 2, "zh": "我多么想知道你是什么",
         "liaison": [["How", "I"]],
         "swallow": [{"word": "what", "letters": "t"}]},
    ])
    got = parse_response(resp, TEXTS)
    assert got[1]["zh"] == "一闪一闪小星星"
    assert got[1]["liaison"] == [["twinkle", "twinkle"]]
    assert got[1]["swallow"] == [{"word": "little", "letters": "t"}]
    assert got[2]["liaison"] == [["How", "I"]]


def test_parse_drops_annotations_not_in_line():
    resp = make_response([
        {"index": 1, "zh": "一闪一闪小星星",
         # "little star" 相邻 ✓；"star twinkle" 不相邻 ✗；"banana" 不在句中 ✗
         "liaison": [["little", "star"], ["star", "twinkle"], ["banana", "split"], "junk"],
         "swallow": [{"word": "little", "letters": "t"},
                     {"word": "banana", "letters": "b"},      # 词不在句中
                     {"word": "twinkle", "letters": "xyz"},   # 字母不在词中
                     {"word": "twinkle"}],                     # 缺 letters
        },
        {"index": 2, "zh": "我多么想知道你是什么", "liaison": [], "swallow": []},
    ])
    got = parse_response(resp, TEXTS)
    assert got[1]["liaison"] == [["little", "star"]]
    assert got[1]["swallow"] == [{"word": "little", "letters": "t"}]


def test_parse_rejects_non_json():
    with pytest.raises(AnnotateError):
        parse_response("这不是 JSON", TEXTS)


def test_parse_rejects_too_many_missing_lines():
    resp = make_response([{"index": 1, "zh": "一闪一闪小星星", "liaison": [], "swallow": []}])
    with pytest.raises(AnnotateError, match="缺句"):
        parse_response(resp, ["one", "two", "three", "four"])


def test_build_prompt_numbers_lines():
    prompt = build_prompt(TEXTS)
    assert "1. Twinkle, twinkle, little star" in prompt
    assert "2. How I wonder what you are" in prompt
    assert "共 2 句" in prompt
