import json

from translate_prep import REVIEW_DIRNAME, WORK_DIRNAME, reviewed_tags


def write(base, name, payload):
    (base / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_missing_files_are_not_an_error(tmp_path):
    assert reviewed_tags(tmp_path) == set()


def test_reads_both_value_shapes(tmp_path):
    # *_official.json 的值是 {lang: name};supplement/manual 的值是一个裸字符串。
    # 两种形状混在同一个函数里,搞错一种会让整类 tag 悄悄漏进或漏出审查名单。
    write(tmp_path, "copyright_official.json", {"a": {"zh_hans": "甲", "ja": "コウ"}})
    write(tmp_path, "character_official.json", {"b": {"zh_hant": "乙"}})
    write(tmp_path, "zh_supplement.json", {"c": "丙"})
    write(tmp_path, "zh_manual.json", {"d": "丁"})
    assert reviewed_tags(tmp_path) == {"a", "b", "c", "d"}


def test_official_entries_without_a_chinese_name_do_not_count_as_reviewed(tmp_path):
    # 选择那一遍只给了日文名 —— 中文名仍是启发式挑的,仍然需要审。
    write(tmp_path, "copyright_official.json", {"ja_only": {"ja": "コウ"}, "both": {"ja": "コウ", "zh_hans": "甲"}})
    assert reviewed_tags(tmp_path) == {"both"}


def test_empty_values_do_not_count_as_reviewed(tmp_path):
    write(tmp_path, "zh_supplement.json", {"filled": "甲", "blank": "", "nulled": None})
    write(tmp_path, "character_official.json", {"empty_dict": {}, "blank_zh": {"zh_hans": ""}})
    assert reviewed_tags(tmp_path) == {"filled"}


def test_review_and_fill_batches_do_not_share_a_directory():
    # 两遍的 out_NNNN.json 同名。共用一个目录会让后跑的那遍读到前一遍的答案,
    # 而那些答案回答的是另一个问题。
    assert REVIEW_DIRNAME != WORK_DIRNAME
