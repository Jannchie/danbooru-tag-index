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
    # 作者名:别名在 artists 端点(wiki 的 other_names 对 artist 几乎为空)
    "artist": """
        SELECT t.name, a.other_names FROM tags t
        JOIN artists a ON a.name = t.name
        WHERE t.category = 1 AND t.post_count > 0
          AND a.is_deleted = 0 AND a.other_names != '[]'
    """,
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export multilingual name mappings for artist/copyright tags as JSON."
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
