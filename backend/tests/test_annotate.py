"""annotate 解析测试（不触碰真实 LLM）。连读/吞音规则引擎的测试在 test_phon_rules.py。"""
import json

import pytest

from tinker.annotate import AnnotateError, build_prompt, parse_response

TEXTS = ["Twinkle, twinkle, little star", "How I wonder what you are"]


def make_response(lines):
    return json.dumps({"lines": lines}, ensure_ascii=False)


def test_parse_happy_path():
    resp = make_response([
        {"index": 1, "zh": "一闪一闪小星星"},
        {"index": 2, "zh": "我多么想知道你是什么"},
    ])
    got = parse_response(resp, len(TEXTS))
    assert got == {1: "一闪一闪小星星", 2: "我多么想知道你是什么"}


def test_parse_drops_bad_items():
    resp = make_response([
        {"index": 1, "zh": "一闪一闪小星星"},
        {"index": 2, "zh": ""},                    # 空翻译
        {"index": 99, "zh": "越界"},               # 行号越界
        {"zh": "缺 index"},                          # 缺字段
        "junk",
        {"index": 2, "zh": "我多么想知道你是什么"},  # 合法项兜底
    ])
    got = parse_response(resp, len(TEXTS))
    assert got == {1: "一闪一闪小星星", 2: "我多么想知道你是什么"}


def test_parse_rejects_non_json():
    with pytest.raises(AnnotateError):
        parse_response("这不是 JSON", len(TEXTS))


def test_parse_rejects_too_many_missing_lines():
    resp = make_response([{"index": 1, "zh": "一闪一闪小星星"}])
    with pytest.raises(AnnotateError, match="缺句"):
        parse_response(resp, 4)


def test_build_prompt_numbers_lines():
    prompt = build_prompt(TEXTS)
    assert "1. Twinkle, twinkle, little star" in prompt
    assert "2. How I wonder what you are" in prompt
    assert "共 2 句" in prompt
