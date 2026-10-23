"""标记文本的生成与校验测试（语法约定见 tinker/markup.py 模块头）。"""
from tinker.markup import to_markup, validate_markup
from tinker.phon_rules import annotate_line

TEXT = "But I know the sun will shine for us"


def test_to_markup_liaison():
    marks = {"liaison": [["but", "i"], ["for", "us"]], "swallow": []}
    assert to_markup(TEXT, marks) == "But-I know the sun will shine for-us"


def test_to_markup_swallow():
    marks = {"liaison": [], "swallow": [{"word": "want", "letters": "t"},
                                        {"word": "keep", "letters": "p"}]}
    assert to_markup("I want you to keep your dream", marks) == \
        "I wan^t^ you to kee^p^ your dream"


def test_to_markup_mixed_and_swallow_inside_liaison():
    # 连读对里的词同时带吞音：两种标记叠在同一个 - 单元里
    marks = {"liaison": [["take", "it"]], "swallow": [{"word": "take", "letters": "k"}]}
    assert to_markup("Please take it easy", marks) == "Please ta^k^e-it easy"


def test_to_markup_repeated_words_use_first_unused():
    # 同名词出现多次时取第一个未占用的，与前端渲染的匹配规则一致
    marks = {"liaison": [], "swallow": [{"word": "what", "letters": "t"}]}
    assert to_markup("what is what", marks) == "wha^t^ is what"


def test_to_markup_unknown_pair_silently_skipped():
    marks = {"liaison": [["zzz", "qqq"]], "swallow": [{"word": "zzz", "letters": "z"}]}
    assert to_markup(TEXT, marks) == TEXT


def test_to_markup_roundtrip_from_rule_engine():
    # 规则引擎真实输出 → 标记文本，应得到用户示例同款写法
    marks = annotate_line(TEXT)
    assert to_markup(TEXT, marks) == "But-I know the sun will shine for-us"


# ---------- 校验 ----------

def test_validate_accepts_unmodified_generated_text():
    marked = to_markup(TEXT, annotate_line(TEXT))
    assert validate_markup(TEXT, marked) is None


def test_validate_accepts_plain_text_all_marks_removed():
    assert validate_markup(TEXT, TEXT) is None


def test_validate_rejects_word_typo():
    err = validate_markup(TEXT, "But-I know the san will shine for-us")
    assert err and "san" in err


def test_validate_rejects_extra_word():
    err = validate_markup(TEXT, "But-I know the sun will shine for us all")
    assert err and "数量" in err


def test_validate_rejects_empty():
    assert validate_markup(TEXT, "   ") is not None


def test_validate_ignores_case_and_punctuation():
    assert validate_markup(TEXT, "but-i know, the sun will shine for us") is None


def test_validate_original_with_hyphenated_word():
    # 原文自带连字符：剥掉 - 后与原文一致即通过（渲染成连读是语法固有歧义，可接受）
    assert validate_markup("a merry-go-round", "a merry-go-round") is None
