import json

import pytest

from translate_merge import load_batches, not_simplified, reject


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


def test_copying_the_japanese_name_is_rejected():
    assert reject("gits", "攻殻機動隊", {"ja": "攻殻機動隊"}) == "not-simplified"
    # 纯汉字的日文标题会先被字形检查拦住,所以另用一个字形上合法的例子确认这条规则本身。
    assert reject("x", "东方", {"ja": "东方"}) == "same-as-japanese"


def test_echoing_the_tag_name_is_rejected():
    assert reject("death note", "死亡笔记", {}) is None
    assert reject("hololive", "hololive", {}) == "no-han"


def test_not_simplified_round_trips_shared_characters():
    # 这是检查的核心:简体字 jp2t->t2s 往返能还原自己,日文专用字形不能。
    assert not not_simplified("宝可梦")
    assert not_simplified("黒")
    assert not_simplified("寶可夢")


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
