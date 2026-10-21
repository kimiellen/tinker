"""phon_rules 音素级规则引擎测试：每条规则的正反例（不触碰 LLM）。"""
from tinker.phon_rules import annotate_line


def liaison_of(text):
    return annotate_line(text)["liaison"]


def swallow_of(text):
    return annotate_line(text)["swallow"]


# ---------- 连读：尾辅音 + 头元音 ----------

def test_liaison_consonant_to_vowel():
    assert ["each", "other"] in liaison_of("We need each other in California")
    assert ["other", "in"] in liaison_of("We need each other in California")
    assert ["but", "i"] in liaison_of("But I know the sun will shine for us")
    assert ["for", "us"] in liaison_of("But I know the sun will shine for us")


def test_liaison_final_r_links_to_vowel():
    # for /r/ + us 元音：规则 1 天然覆盖（美音词尾 r 是辅音）
    assert ["for", "us"] in liaison_of("But I know the sun will shine for us")


# ---------- 连读：元音 + 元音滑音 ----------

def test_liaison_front_vowel_glide():
    assert ["my", "age"] in liaison_of("Then I think my age how old skyline how far")
    assert ["i", "am"] in liaison_of("I am a dreamer")


def test_liaison_back_vowel_glide():
    assert ["how", "old"] in liaison_of("Then I think my age how old skyline how far")
    assert ["you", "are"] in liaison_of("How I wonder what you are")


# ---------- 连读反例：尾元音 + 头辅音、辅音 + 辅音都不是连读 ----------

def test_no_liaison_vowel_to_consonant():
    # LLM 版曾错标 wanna‿drive：wanna 尾是元音，drive 头是辅音
    assert liaison_of("I wanna drive my car") == []


def test_no_liaison_consonant_to_consonant():
    # LLM 版曾错标 said‿the、apartment‿with、shine‿for
    assert liaison_of("Forecaster said the weathers may be rainy hard") == []
    assert liaison_of("To your apartment with a present like a star") == [
        ["your", "apartment"], ["with", "a"], ["like", "a"]]
    assert liaison_of("But I know the sun will shine for us") == [["but", "i"], ["for", "us"]]


# ---------- 吞音：尾爆破音 + 辅音头（失去爆破） ----------

def test_swallow_unreleased_stop_before_consonant():
    assert {"word": "did", "letters": "d"} in swallow_of("If you did not twinkle so")
    assert {"word": "take", "letters": "k"} in swallow_of("Please take my hand")
    assert {"word": "that", "letters": "t"} in swallow_of("It seems that the passion's never gone")


def test_no_swallow_when_stop_links_to_vowel():
    # 爆破音后接元音是连读而非吞音（take‿it 的 k 递过去读）
    assert swallow_of("Please take it easy") == []
    assert ["take", "it"] in liaison_of("Please take it easy")


# ---------- 吞音：元音间非重读 t 闪音化 ----------

def test_swallow_flap_t():
    assert {"word": "little", "letters": "tt"} in swallow_of("Twinkle twinkle little star")
    assert {"word": "water", "letters": "t"} in swallow_of("water is good")


def test_no_flap_t_when_following_vowel_stressed():
    # retain 的 t 后随重读元音，不闪音化；party 的 t 前是 r 不是元音，也不闪
    assert swallow_of("please retain this") == []
    assert swallow_of("we party all night") == []


# ---------- 兜底：词典查不到的词不猜 ----------

def test_unknown_words_are_skipped():
    assert annotate_line("xyzzyplugh apple banana") == {"liaison": [], "swallow": []}


def test_possessive_falls_back_to_stem():
    # Sunday's 词典没有，回落 sunday + /Z/；Z+K 不连读不吞音
    assert annotate_line("Sunday's coming I wanna drive my car")["liaison"] == [["coming", "i"]]
