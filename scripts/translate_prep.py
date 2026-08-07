"""为「补中文名」准备 LLM 批次输入。

和 _char_prep.py 的任务不同:那个是**从 wiki 候选里挑**官方名,只在候选存在时有用。
但 Danbooru wiki 的 other_names 对多数作品只有日文别名(death_note 的候选池里
一个中文都没有),所以选择式的那一遍结构上就填不出中文 —— copyright 只有 24%
的 tag 有中文名。这一遍是**翻译**:凭已有知识给出通行中文译名。

翻译比选择更容易出错(上一遍越出候选池的 43 个里就有把 Fate/UBW 写成「命运石之门」
的),所以批次里带上尽可能多的锚点:英文名、日文名、别名池、类目、投稿量。宁缺勿错,
拿不准就返回 null,让 tag 保持英文显示 —— 错译比没有译名更糟。

输出:<work-dir>/in_NNNN.json,每文件 BATCH 个 {tag: {锚点}}。
"""

import argparse
import json
import math
from pathlib import Path

import duckdb

from _paths import INDEX_DIR, TRANSLATIONS_DIR

BATCH = 100
CATEGORY_NAME = {1: "artist", 3: "copyright", 4: "character"}
# 只有这两类值得翻译:general/meta 用英文 tag 名本身就是通行叫法,artist 是人名,
# 音译画师笔名的收益远低于出错的代价。
WANTED_CATEGORIES = (3, 4)
WORK_DIRNAME = "_zh_work"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare LLM batches for tags missing a Chinese name.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--dir", type=str, default=str(TRANSLATIONS_DIR))
    parser.add_argument("--min-post-count", type=int, default=1000, help="Only tags at least this popular.")
    parser.add_argument("--batch", type=int, default=BATCH)
    return parser.parse_args()


def load_targets(index_dir: Path, base: Path, min_post_count: int) -> list[tuple[str, dict]]:
    """Tags in a wanted category, popular enough to matter, with no Chinese name yet."""
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT name, category, post_count
        FROM read_parquet('{(index_dir / "dim_tag.parquet").as_posix()}')
        WHERE category IN {WANTED_CATEGORIES} AND post_count >= ?
        ORDER BY post_count DESC
        """,
        [min_post_count],
    ).fetchall()
    con.close()

    maps: dict[str, dict[str, str]] = {}
    pools: dict[str, dict[str, list[str]]] = {}
    for category in CATEGORY_NAME.values():
        name_map = base / f"{category}_name_map.json"
        if name_map.exists():
            maps.update(json.loads(name_map.read_text(encoding="utf-8")))
        names = base / f"{category}_names.json"
        if names.exists():
            pools.update(json.loads(names.read_text(encoding="utf-8")))

    targets: list[tuple[str, dict]] = []
    for name, category, post_count in rows:
        known = maps.get(name, {})
        if known.get("zh_hans"):
            continue
        pool = pools.get(name, {})
        aliases = [n for lang, values in pool.items() if lang != "en" for n in values]
        item = {
            "category": CATEGORY_NAME[category],
            "posts": int(post_count),
            "en": known.get("en") or "",
            "ja": known.get("ja") or "",
            "aliases": aliases[:12],
        }
        targets.append((name, {k: v for k, v in item.items() if v != "" and v != []}))
    return targets


def main() -> None:
    args = parse_args()
    base = Path(args.dir)
    targets = load_targets(Path(args.index_dir), base, args.min_post_count)

    work = base / WORK_DIRNAME
    work.mkdir(parents=True, exist_ok=True)
    # 清掉旧输入(保留 out_*.json 以便续跑)
    for stale in work.glob("in_*.json"):
        stale.unlink()

    n_batches = math.ceil(len(targets) / args.batch)
    for i in range(n_batches):
        chunk = dict(targets[i * args.batch : (i + 1) * args.batch])
        (work / f"in_{i:04d}.json").write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")

    by_category: dict[str, int] = {}
    grounded = 0
    for _, item in targets:
        by_category[item["category"]] = by_category.get(item["category"], 0) + 1
        if item.get("ja") or item.get("aliases"):
            grounded += 1
    print(f"tags missing a Chinese name (post_count >= {args.min_post_count}): {len(targets)}")
    print(f"  by category: {by_category}")
    print(f"  with a Japanese name or alias to anchor on: {grounded} ({grounded * 100 // max(len(targets), 1)}%)")
    print(f"  batches: {n_batches} (batch={args.batch}) -> {work}")


if __name__ == "__main__":
    main()
