import pytest

from _hanzi import SEMANTIC_QUIRKS, is_simplified, repair_to_simplified, to_simplified


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("寶可夢", "宝可梦"),
        ("蔚藍檔案", "蔚蓝档案"),
        ("死亡笔记", "死亡笔记"),   # 对简体是恒等
    ],
)
def test_converts_traditional_to_simplified(text, expected):
    assert to_simplified(text) == expected


@pytest.mark.parametrize("text", ["哪吒之魔童降世", "武动乾坤", "於菟", "蒜薹", "徵弦", "白石紬"])
def test_semantic_replacements_are_undone(text):
    # OpenCC 的 t2s 会按意思替换这几个字(吒->咤、乾->干),而它们本身就是规范简体。
    # 不还原的话「哪吒」会变成「哪咤」—— 实测已发布名字里有 17 个被这么改坏过。
    assert to_simplified(text) == text


def test_every_quirk_is_actually_a_quirk():
    # 这张表是实测出来的。若某天 OpenCC 修好了某个字,这条会失败,提醒把它从表里删掉 ——
    # 否则那个字的还原就成了永久的、无人知晓的例外。
    import opencc

    t2s = opencc.OpenCC("t2s")
    for right, wrong in SEMANTIC_QUIRKS.items():
        assert t2s.convert(right) == wrong, f"{right} 已不再被 t2s 改成 {wrong},可以从表里去掉"


@pytest.mark.parametrize("text", ["死亡笔记", "精灵宝可梦", "光之美少女", "哪吒之魔童降世", "乾纱寿叶"])
def test_simplified_text_is_recognised(text):
    assert is_simplified(text)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("黒森峰女学园", "日文新字体 黒"),
        ("剣", "日文新字体 剣"),
        ("四季映姫", "OpenCC 漏掉的 姫"),
        ("時々", "日文叠字符号"),
        ("蔚藍檔案", "繁体"),
    ],
)
def test_non_simplified_text_is_rejected(text, why):
    assert not is_simplified(text), why


def test_shared_glyphs_are_not_mistaken_for_japanese():
    # 宝/梦 既是日文新字体又是中文简化字。只看「jp2t 有没有变化」会把这串判成日文,
    # 所以判据是 jp2t->t2s 往返能否还原。
    assert is_simplified("精灵宝可梦")


# --- repair_to_simplified -------------------------------------------------------


@pytest.mark.parametrize(
    ("broken", "fixed"),
    [
        ("月姫", "月姬"),                    # OpenCC 的 jp2t 漏掉 姫
        ("西行寺幽々子", "西行寺幽幽子"),        # 叠字符号展开
        ("佐々木千枝", "佐佐木千枝"),
        ("戦斗潮流", "战斗潮流"),
        ("绚瀬絵里", "绚濑绘里"),
        ("桜内梨子", "樱内梨子"),
        ("旧作霊梦", "旧作灵梦"),
        ("多人数戦闘用衣装", "多人数战斗用衣装"),   # 戦/闘 修掉,装 受保护
        ("蔚藍檔案", "蔚蓝档案"),
    ],
)
def test_repairs_japanese_and_traditional(broken, fixed):
    assert repair_to_simplified(broken) == fixed


@pytest.mark.parametrize(
    "text",
    [
        "醋酸汁",      # jp2t 双向映射 酢⇄醋,跑一遍就变回日文
        "默天芸",      # jp2t 把 芸 读成 藝 的新字体
        "予愿安洁莉娜",  # t2s 把 予 换成 豫
        "缺月得疏桐",   # jp2t 把 疏 换成日文形 疎
        "爱与欠灌",    # t2s 把 欠 换成 缺
        "哪吒之魔童降世",
        "死亡笔记",
    ],
)
def test_never_bites_a_name_that_is_already_correct(text):
    # 这是整个函数能安全存在的前提:已合格的串一律不进转换。
    assert repair_to_simplified(text) == text


@pytest.mark.parametrize("stubborn", ["々", "々子"])
def test_unrepairable_names_are_left_alone_not_guessed_at(stubborn):
    # 叠字符号在开头就没有可重复的对象,转写不出合格结果。这时返回原值,让
    # is_simplified 拦下来报告,而不是悄悄换成一个错的。
    assert not is_simplified(stubborn)
    assert repair_to_simplified(stubborn) == stubborn


def test_iteration_mark_at_the_start_has_nothing_to_repeat():
    from _hanzi import strip_japanese_glyphs

    assert strip_japanese_glyphs("々木") == "々木"
    assert strip_japanese_glyphs("佐々木") == "佐佐木"


@pytest.mark.parametrize(
    ("broken", "fixed"),
    [
        ("戦鬥潮流", "戰鬥潮流"),
        ("桜內梨子", "櫻內梨子"),
        ("月姫", "月姬"),
        ("西行寺幽々子", "西行寺幽幽子"),
        ("絢瀬絵裏", "絢瀨繪裏"),
    ],
)
def test_traditional_names_lose_their_japanese_glyphs(broken, fixed):
    from _hanzi import repair_to_traditional

    assert repair_to_traditional(broken) == fixed


@pytest.mark.parametrize("text", ["蔚藍檔案", "黑暗之魂", "神奈川衝浪裏", "死亡筆記"])
def test_traditional_repair_leaves_real_traditional_alone(text):
    # 不判简繁 —— 拿简体标准衡量繁体字段会把它全部推翻。裏 不是日文字形,神奈川衝浪裏
    # 本来就该长这样。
    from _hanzi import repair_to_traditional

    assert repair_to_traditional(text) == text
