import json

import pytest

from build_name_map import beautify_tag, build, character_en, fill_cjk, normalize_chinese, pick_character, pick_copyright, primary, shortest


def write(tmp_path, data):
    path = tmp_path / "names.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_primary_takes_the_first_name_and_shortest_the_shortest():
    # Danbooru's other_names is an ordered, hand-maintained list: the primary name
    # leads and scraped Pixiv slang follows. Picking by length picks the slang.
    pool = ["デスノート", "DN腐", "デスノ", "デスノプラス", "デスノ夢", "死帳夢"]
    assert primary(pool) == "デスノート"
    assert shortest(pool) == "DN腐"


def test_both_pickers_clean_underscores_and_blanks():
    assert primary(["", "  ", "touhou_project"]) == "touhou project"
    assert shortest([" ", ""]) is None
    assert primary([]) is None


def test_pick_copyright_uses_primary_and_leaves_en_to_the_fallback():
    buckets = {
        "en": ["ONEPIECE", "onepiece"],
        "ja": ["ワンピース", "ワンピ", "アニワン"],
        "zh_hans": ["海贼王", "航海王"],
    }
    picked = pick_copyright("one_piece", buckets)
    assert picked["ja"] == "ワンピース"
    assert picked["zh_hans"] == "海贼王"
    # The en bucket is Pixiv abbreviations; build()'s en_fallback is more reliable.
    assert "en" not in picked


def test_official_entry_without_en_gets_the_tag_romanisation(tmp_path):
    # The LLM pass often returns only a subset of languages. Before the fallback,
    # 8.8k copyright tags had an official entry with no `en` and so no display
    # name at all -- the app fell back to showing the raw tag string.
    source = write(tmp_path, {"code_geass": {"ja": ["コードギアス"]}})
    official = {"code_geass": {"ja": "コードギアス反逆のルルーシュ", "zh_hans": "叛逆的鲁鲁修"}}
    built = build(source, pick_copyright, official, en_fallback=beautify_tag)
    assert built["code_geass"]["en"] == "Code Geass"
    assert built["code_geass"]["ja"] == "コードギアス反逆のルルーシュ"


def test_official_en_is_never_clobbered_by_the_fallback(tmp_path):
    # "Steins;Gate" is the real title; beautify_tag would give "Steins;gate".
    source = write(tmp_path, {"steins;gate": {}})
    official = {"steins;gate": {"en": "Steins;Gate", "ja": "シュタインズ・ゲート"}}
    built = build(source, pick_copyright, official, en_fallback=beautify_tag)
    assert built["steins;gate"]["en"] == "Steins;Gate"


def test_en_override_wins_over_official(tmp_path):
    # Characters keep the tag romanisation regardless of what the LLM returned.
    source = write(tmp_path, {"artoria_pendragon_(fate)": {"ja": ["アルトリア"]}})
    official = {"artoria_pendragon_(fate)": {"en": "Saber", "ja": "アルトリア・ペンドラゴン"}}
    built = build(source, pick_character, official, en_override=character_en)
    assert built["artoria_pendragon_(fate)"]["en"] == "Artoria Pendragon"


def test_heuristic_fallback_applies_when_no_official_entry(tmp_path):
    source = write(tmp_path, {"fullmetal_alchemist": {"ja": ["鋼の錬金術師", "FA腐"], "zh_hans": ["钢之炼金术师"]}})
    built = build(source, pick_copyright, {}, en_fallback=beautify_tag)
    assert built["fullmetal_alchemist"] == {
        "en": "Fullmetal Alchemist",
        "ja": "鋼の錬金術師",
        "zh_hans": "钢之炼金术师",
        "zh_hant": "鋼之鍊金術師",   # 从 zh_hans 简→繁,不是日文新字体的「錬」
    }


@pytest.mark.parametrize(
    ("chosen", "expected_zh"),
    [
        ({"ja": "デスノート"}, None),          # kana only: nothing to convert from
        ({"ja": "ブルーアーカイブ"}, None),
        ({"ja": "攻殻機動隊"}, "攻壳机动队"),  # pure Han: Chinese is a script conversion away
    ],
)
def test_chinese_is_derived_only_from_a_han_name(chosen, expected_zh):
    # This is why `death_note` has no Chinese name and cannot get one mechanically:
    # its Japanese title is katakana, so there is no Han base to convert. Guessing
    # one from the alias pool is what produced garbage like school_uniform =
    # 制服スパッツ, so the pipeline refuses to invent it.
    assert fill_cjk(dict(chosen)).get("zh_hans") == expected_zh


@pytest.mark.parametrize(
    ("chosen", "lang", "expected"),
    [
        ({"ja": "士郎正宗"}, "zh_hans", "士郎正宗"),      # not 士郞正宗
        ({"ja": "冰雨"}, "zh_hans", "冰雨"),             # not 氷雨
        ({"ja": "真琴"}, "zh_hans", "真琴"),             # not 眞琴
        ({"zh_hans": "猫屋敷"}, "zh_hant", "貓屋敷"),     # traditional keeps 貓
        ({"zh_hans": "武内崇"}, "zh_hant", "武內崇"),     # traditional keeps 內
    ],
)
def test_opencc_produces_chinese_glyphs_not_japanese_variants(chosen, lang, expected):
    # Guards the opencc pin in pyproject.toml. Version 1.4.1 rewrote 6,663 names in
    # the shipped maps with Japanese and variant forms -- 士郎 → 士郞, 冰 → 氷,
    # 真 → 眞, and 貓 → 猫 in traditional output. Nothing counts that: the bundle
    # builds to the same tag total and just renders wrong glyphs. If this fails
    # after a dependency bump, the bump is the bug.
    assert fill_cjk(dict(chosen))[lang] == expected


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("未来日記", "未来日记"),      # wiki 的 zh 桶给的「简体名」其实是繁体
        ("封神演義", "封神演义"),
        ("愛知万博", "爱知万博"),
        ("默天蕓", "默天芸"),
        ("醋酸汁", "醋酸汁"),          # 已经正确的值必须原样保留
        ("默天芸", "默天芸"),
        ("碧蓝档案", "碧蓝档案"),
    ],
)
def test_supplied_chinese_names_are_normalised_without_being_mangled(supplied, expected):
    # fill_cjk 只在派生缺失字段时转换,直接给定的值它信任 —— 但 wiki 和 official 给的
    # 「简体名」有 2493 个其实掺着繁体。归一化只用 t2s:对已经是简体的串它是恒等,
    # 而 jp2t 会把共用字当日文倒转(醋酸汁 -> 酢酸汁,默天芸 -> 默天艺)。
    assert normalize_chinese({"zh_hans": supplied})["zh_hans"] == expected


def test_normalising_leaves_the_traditional_field_alone():
    # 繁体字段本来就该是繁体:t2s 会简化掉它,jp2t 会咬它。
    assert normalize_chinese({"zh_hant": "蔚藍檔案"})["zh_hant"] == "蔚藍檔案"
