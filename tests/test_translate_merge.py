import json

import pytest

from translate_merge import CONFIRMED, collisions, judge, load_batches, reject


@pytest.mark.parametrize(
    "zh",
    [
        "死亡笔记",
        "光之美少女",
        "哆啦A梦",      # 通行译名里有拉丁字母是正常的
        "头文字D",
        "精灵宝可梦",    # 宝/梦 既是日文新字体又是中文简化字,不能因此判成日文
        "偶像大师灰姑娘女孩",
    ],
)
def test_good_chinese_names_pass(zh):
    assert reject("some_tag", zh, {}) is None


@pytest.mark.parametrize(
    ("zh", "why"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Isegye Idol", "no-han"),      # 拉丁字母组成的「中文名」不加信息,tag 名本身就能搜到
        ("デスノート", "no-han"),
        ("虹ヶ咲学园", "contains-kana"),
        ("黒森峰女学园", "not-simplified"),   # 日文新字体 黒
        ("四季映姫", "not-simplified"),      # OpenCC 漏掉的 姫
        ("時々", "not-simplified"),         # 日文叠字符号
        ("蔚藍檔案", "not-simplified"),      # 繁体:只收简体,繁体由程序转换
        ("死" * 61, "too-long"),
    ],
)
def test_bad_values_are_rejected_with_a_reason(zh, why):
    assert reject("some_tag", zh, {}) == why


def test_an_untranslated_japanese_title_is_still_caught_by_the_glyph_check():
    assert reject("gits", "攻殻機動隊", {"ja": "攻殻機動隊"}) == "not-simplified"
    assert reject("aot", "進撃の巨人", {"ja": "進撃の巨人"}) == "contains-kana"


def test_matching_the_japanese_anchor_is_not_by_itself_a_reason():
    # 中日共用汉字名是常态,而 ja 桶里还混着被 han_language_overrides 误判成日文的
    # 中文名。曾经有一条 same-as-japanese 检查,它为了守住「黒森峰女学园」而丢掉
    # 「赤星小梅」—— 实测 24 条正确答案被它拒掉,所以拿掉了。
    assert reject("chen", "橙", {"ja": "橙"}) is None
    assert reject("nakano_nino", "中野二乃", {"ja": "中野二乃"}) is None


def test_echoing_the_tag_name_is_rejected():
    assert reject("death note", "死亡笔记", {}) is None
    assert reject("hololive", "hololive", {}) == "no-han"


def write(work, index, name, payload):
    (work / f"{name}_{index:04d}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_load_batches_reports_missing_and_corrupt(tmp_path):
    write(tmp_path, 0, "in", {"a": {"ja": "あ"}})
    write(tmp_path, 1, "in", {"b": {"ja": "い"}})
    write(tmp_path, 2, "in", {"c": {"ja": "う"}})
    write(tmp_path, 0, "out", {"a": "甲"})
    (tmp_path / "out_0001.json").write_text("{not json", encoding="utf-8")
    # batch 2 never ran

    batch = load_batches(tmp_path)
    assert batch.expected == {0, 1, 2}
    assert batch.done == {0}
    assert batch.corrupt == [1]
    assert batch.answers == {"a": "甲"}


def test_load_batches_accepts_both_answer_shapes(tmp_path):
    # A model may answer with a bare string or wrap it in {"zh_hans": ...}; both
    # are unambiguous, so accept them rather than failing a whole batch on shape.
    write(tmp_path, 0, "in", {"a": {}, "b": {}, "c": {}})
    write(tmp_path, 0, "out", {"a": "甲", "b": {"zh_hans": "乙"}, "c": None})
    batch = load_batches(tmp_path)
    assert batch.answers == {"a": "甲", "b": "乙"}   # null means "I do not know"


# --- 审查模式 -------------------------------------------------------------------


def test_confirmation_is_not_a_write():
    # 这一遍的核心不变量。确认写进 zh_supplement 会把「别名池随手挑的」冒充成「审过
    # 的翻译」,而 prep 正是靠「不在 supplement 里」判定未审查 —— 那样下一轮就再也
    # 找不到它们,可信度却一点没提高。
    anchors = {"current_zh": "鸣潮", "ja": "鳴潮"}
    assert judge("wuthering_waves", "鸣潮", anchors, review=True) == CONFIRMED


def test_confirmation_tolerates_surrounding_whitespace():
    assert judge("x", "  鸣潮 ", {"current_zh": "鸣潮"}, review=True) == CONFIRMED


def test_a_different_answer_is_a_correction_and_gets_written():
    assert judge("akagi_(kancolle)", "赤城", {"current_zh": "赤贺"}, review=True) is None


def test_a_correction_still_has_to_pass_the_mechanical_checks():
    # 审查模式不是免检通道:模型可能把日文原样抄过来当成「修正」。
    assert judge("x", "黒森峰女学园", {"current_zh": "某名"}, review=True) == "not-simplified"
    assert judge("x", "デスノート", {"current_zh": "某名"}, review=True) == "no-han"


def test_outside_review_mode_matching_the_current_name_is_not_special():
    # 补全那一遍没有 current_zh,同一个答案必须走普通路径,不能被当成确认而丢掉。
    assert judge("x", "鸣潮", {"current_zh": "鸣潮"}, review=False) is None


def test_review_without_a_current_name_falls_through_to_the_checks():
    # 锚点缺 current_zh 时,空字符串不该和任何答案相等而误判成确认。
    assert judge("x", "鸣潮", {}, review=True) is None
    assert judge("x", "鸣潮", {"current_zh": None}, review=True) is None


def test_collision_is_reported_when_a_correction_erases_a_distinction():
    # newsboy 变体本来有自己的名字,改成本体的名字之后图例上分不开。
    inputs = {"aya": {"current_zh": "射命丸文"}, "aya_newsboy": {"current_zh": "铃奈庵文"}}
    existing = {"aya": "射命丸文", "aya_newsboy": "铃奈庵文"}
    assert collisions({"aya_newsboy": "射命丸文"}, inputs, existing) == {"aya_newsboy": ["aya"]}


def test_collision_not_reported_when_the_name_was_already_shared():
    # 本来就同名,这次没有让情况变差。
    inputs = {"a": {"current_zh": "空"}, "b": {"current_zh": "空"}}
    existing = {"a": "空", "b": "空"}
    assert collisions({"b": "空"}, inputs, existing) == {}


def test_collision_not_reported_for_a_unique_correction():
    inputs = {"a": {"current_zh": "国崩"}, "b": {"current_zh": "别的"}}
    existing = {"a": "国崩", "b": "别的"}
    assert collisions({"a": "散兵"}, inputs, existing) == {}
