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
CATEGORY_NAME = {3: "copyright", 4: "character"}
# 索引里的另外两类不在这:general/meta 用英文 tag 名本身就是通行叫法,由
# import_general_names.py 单独处理。(artist 根本不进索引,见 build_tag_index.py。)
WANTED_CATEGORIES = (3, 4)
WORK_DIRNAME = "_zh_work"
# 审查的批次输出必须和补全的分开存放。两遍的 out_NNNN.json 同名,共用一个目录会让
# 后跑的那遍读到前一遍的答案,而那些答案回答的是另一个问题。
REVIEW_DIRNAME = "_zh_review"

# 已经过某一遍审查、不需要再看的来源。*_official.json 是 LLM 从 wiki 候选里挑的,
# zh_supplement/zh_manual 是翻译或人工核过的。剩下的中文名全是启发式从别名池里取的
# 第一个候选 —— 没有任何人或模型看过它们对不对。
REVIEWED_SOURCES = ("copyright_official.json", "character_official.json", "zh_supplement.json", "zh_manual.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare LLM batches for tags missing a Chinese name.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--dir", type=str, default=str(TRANSLATIONS_DIR))
    parser.add_argument("--min-post-count", type=int, default=1000, help="Only tags at least this popular.")
    parser.add_argument("--batch", type=int, default=BATCH)
    parser.add_argument(
        "--review",
        action="store_true",
        help="改为审查已有但未经审查的中文名(启发式从别名池取的),而不是补缺失的。",
    )
    return parser.parse_args()


def reviewed_tags(base: Path) -> set[str]:
    out: set[str] = set()
    for name in REVIEWED_SOURCES:
        path = base / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for tag, value in data.items():
            # *_official.json 的值是 {lang: name};supplement/manual 的值是一个字符串。
            if isinstance(value, dict):
                if value.get("zh_hans") or value.get("zh_hant"):
                    out.add(tag)
            elif value:
                out.add(tag)
    return out


def load_targets(index_dir: Path, base: Path, min_post_count: int, review: bool = False) -> list[tuple[str, dict]]:
    """符合类目、够热门的 tag:默认取「还没有中文名」的,--review 取「有但没人看过」的。"""
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

    already_reviewed = reviewed_tags(base) if review else set()

    targets: list[tuple[str, dict]] = []
    for name, category, post_count in rows:
        known = maps.get(name, {})
        if review:
            # 有中文名、且不来自任何审查过的来源 —— 也就是启发式从别名池挑的那批。
            if not known.get("zh_hans") or name in already_reviewed:
                continue
        elif known.get("zh_hans"):
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
        if review:
            # 待判定的那个名字。放进去才能问「这个对不对」而不是「这个叫什么」——
            # 后者会让模型重新翻一遍,把本来对的也换掉。
            item["current_zh"] = known.get("zh_hans", "")
        targets.append((name, {k: v for k, v in item.items() if v != "" and v != []}))
    return targets


def main() -> None:
    args = parse_args()
    base = Path(args.dir)
    targets = load_targets(Path(args.index_dir), base, args.min_post_count, args.review)

    work = base / (REVIEW_DIRNAME if args.review else WORK_DIRNAME)
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
    what = "unreviewed Chinese names" if args.review else "tags missing a Chinese name"
    print(f"{what} (post_count >= {args.min_post_count}): {len(targets)}")
    print(f"  by category: {by_category}")
    print(f"  with a Japanese name or alias to anchor on: {grounded} ({grounded * 100 // max(len(targets), 1)}%)")
    print(f"  batches: {n_batches} (batch={args.batch}) -> {work}")


if __name__ == "__main__":
    main()
