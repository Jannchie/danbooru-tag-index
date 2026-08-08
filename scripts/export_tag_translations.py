import argparse
import json
import re
import sqlite3
from pathlib import Path

import opencc

from _paths import DANBOORU_DB_PATH, TRANSLATIONS_DIR

# 语言分类:字符脚本可确定的(假名/谚文/拉丁)直接判;纯汉字串简繁日同形歧义,
# 优先用 overrides(LLM 预分类结果)裁决,未覆盖的用 OpenCC 字形确定性兜底:
# 含 PRC 简化字→zh_hans,含日文新字体(霊/沢/桜)→ja,纯繁体/共享字→zh_hant。
# 字形相同的 ja/zh_hant(如「一騎当千」)即使判错,build 阶段 fill_cjk 会用 OpenCC
# 把任一汉字基准重新生成三种 CJK 形式,最终输出基本一致,故确定性兜底足够。
HAN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
KANA_RE = re.compile(r"[぀-ヿㇰ-ㇿ]")
HANGUL_RE = re.compile(r"[가-힯ᄀ-ᇿ㄰-㆏]")

_s2t = opencc.OpenCC("s2t")  # 简体→繁体:含简化字则转换后不等于原串
_jp2t = opencc.OpenCC("jp2t")  # 日文新字体→繁体:含新字体则转换后不等于原串

LANG_KEYS = ("en", "ja", "ko", "zh_hans", "zh_hant")

OVERRIDES_FILENAME = "han_language_overrides.json"

# artist 不在这里:画师 tag 整个不进索引(见 build_tag_index.ARTIST_CATEGORY),
# 别名桶做出来也没有 tag 可挂。它曾经从 artists 端点取(wiki 的 other_names 对
# artist 几乎为空),要恢复的话那条 JOIN 是起点。
CATEGORY_SOURCES = {
    # 作品名:wiki 的 other_names 就是 tag 的多语言译名
    "copyright": """
        SELECT t.name, w.other_names FROM tags t
        JOIN wiki_pages w ON w.title = t.name
        WHERE t.category = 3 AND t.post_count > 0
          AND w.is_deleted = 0 AND w.other_names != '[]'
    """,
    # 角色名:同样取自 wiki 的 other_names
    "character": """
        SELECT t.name, w.other_names FROM tags t
        JOIN wiki_pages w ON w.title = t.name
        WHERE t.category = 4 AND t.post_count > 0
          AND w.is_deleted = 0 AND w.other_names != '[]'
    """,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export multilingual name mappings for copyright/character tags as JSON."
    )
    parser.add_argument("--database", type=str, default=str(DANBOORU_DB_PATH))
    parser.add_argument("--output-dir", type=str, default=str(TRANSLATIONS_DIR))
    return parser.parse_args()


def load_overrides(path: Path) -> dict[str, list[str]]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = json.load(file)
    return {name: langs for name, langs in data.items() if isinstance(langs, list)}


def detect_han_lang(name: str) -> str:
    # 纯汉字串的确定性分语言:简化字→zh_hans,日文新字体→ja,其余(繁体/共享)→zh_hant
    if _s2t.convert(name) != name:
        return "zh_hans"
    if _jp2t.convert(name) != name:
        return "ja"
    return "zh_hant"


def classify(name: str, overrides: dict[str, list[str]]) -> list[str]:
    """overrides 是替换,不是补充 —— 试过改成并集,不行。

    已知它会判错:`han_language_overrides["黑暗之魂"] == ["ja"]`,而那是中文名。被判成
    ja 的汉字名会从中文桶里消失,于是更差的候选赢下 zh_hant,简体再由它转换而来。
    Dark Souls 就是这么变成「黑暗灵魂」的(现由 zh_supplement 单点纠正)。

    自然的修法是 `overrides ∪ {detect_han_lang(name)}`,让汉字名至少留在确定性检测
    的那个桶里。实测(2026-08-07,对 36k 有别名池的 tag):2,396 个 tag-语言组合受影响,
    1,940 个「纯新增」、456 个首选被改动 —— 而两类都是一半好一半坏。
    `akagi_(kancolle)` 从「赤賀」(舰船配对,不是名字)修成「赤城」是赚的;
    `akame` 从「赤瞳」(正确译名)变成「赤目」是亏的。纯新增里混着
    `ado_(utaite) -> Ado誕生日`(生日 tag)、`18trip -> 18TRI腐`(同人黑话)、
    `aak_(arknights) -> 阿`(截断)、`adachi_sakura -> 安達`(只有姓)。
    错的名字比没有名字更糟,所以掷硬币的改动不算修复。

    并集还有个系统性偏差:新增几乎全落进 zh_hant,因为 detect_han_lang 对简繁同形的
    串一律兜底 zh_hant。「橙汁」「呆毛」「深空之眼」都不是繁体。

    真正的限制在别名池本身 —— 它是为召回率建的,不保证等价。要纠正个别显示名,用
    zh_supplement.json(它能覆盖而不只是填空);要成批纠正,得有人审。
    """
    if HANGUL_RE.search(name):
        return ["ko"]
    if KANA_RE.search(name):
        return ["ja"]
    if HAN_RE.search(name):
        return overrides.get(name) or [detect_han_lang(name)]
    return ["en"]


def export_category(
    connection: sqlite3.Connection,
    query: str,
    overrides: dict[str, list[str]],
) -> dict[str, dict[str, list[str]]]:
    mapping: dict[str, dict[str, list[str]]] = {}
    for tag_name, raw_names in connection.execute(query):
        groups: dict[str, list[str]] = {}
        seen: set[str] = set()
        for name in json.loads(raw_names):
            name = name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            for lang in classify(name, overrides):
                groups.setdefault(lang, []).append(name)
        if groups:
            mapping[tag_name] = groups
    return mapping


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(output_dir / OVERRIDES_FILENAME)

    connection = sqlite3.connect(args.database)
    try:
        for category, query in CATEGORY_SOURCES.items():
            mapping = export_category(connection, query, overrides)
            output_path = output_dir / f"{category}_names.json"
            with output_path.open("w", encoding="utf-8") as file:
                json.dump(mapping, file, ensure_ascii=False)

            counts: dict[str, int] = dict.fromkeys(LANG_KEYS, 0)
            for groups in mapping.values():
                for lang, names in groups.items():
                    counts[lang] = counts.get(lang, 0) + len(names)
            summary = ", ".join(f"{lang}={count}" for lang, count in counts.items() if count)
            print(f"{category}: {len(mapping)} tags -> {output_path} ({summary})")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
