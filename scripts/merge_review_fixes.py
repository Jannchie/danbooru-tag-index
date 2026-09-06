"""Validate and merge the sharded translation review into general_manual.json.

    uv run python scripts/merge_review_fixes.py [--apply]

The review ran as five agents over five alphabetical shards of the general/meta
vocabulary. Alphabetical because the errors are systematic rather than random --
one word sense chosen wrong propagates through every tag sharing that root, and
adjacency is what makes the pattern visible: `cum_on_*` was uniformly 「暨」, the
Latin "and", across a dozen tags; every `*_horns` tag was a brass instrument.

Nothing is trusted on arrival. A proposal is dropped unless:

  - its tag was actually in that shard -- an agent that invents a plausible tag
    name produces an entry that never matches anything and never gets noticed;
  - the tag is general or meta. Proper nouns are a different problem with a
    different failure mode, and import_general_names.py already refuses them
    for the same reason;
  - the value is a non-empty string that differs from what is already there;
  - the value is canonical simplified Chinese. The corpus this review is fixing
    is full of Japanese glyphs that survived a translation pass (黒下着, 仮面,
    断面図), and a fix that reintroduces them is not a fix.

Collisions between shards are impossible by construction and checked anyway:
the shards partition the vocabulary, so a tag appearing in two of them means
the sharding, not the review, is broken.
"""

import argparse
import csv
import json
import re
import sqlite3

DELIMITER = chr(9)

from _hanzi import is_simplified
from _paths import DANBOORU_DB_PATH, TRANSLATIONS_DIR

REVIEW_DIRS = tuple(TRANSLATIONS_DIR / name for name in ("_review_general", "_review2", "_review3", "_review4", "_review_char", "_review5", "_review6", "_review7"))

# Two vocabularies, two destinations. Ordinary words and proper nouns fail
# differently -- a mistranslated adjective reads oddly, a mistranslated character
# name is confidently wrong -- so they are reviewed separately and merged
# separately, and neither file can receive the other's category.
#
# Each target lists its rounds in ascending order of authority, because a round
# sees only what its sharding shows it. A single-entry pass judges one row at a
# time; the word-family pass gets every tag sharing a root laid out together,
# which is the only way to see `dress_tug` translated as a tugboat while
# `skirt_tug` was not; a deep pass outranks the shallow one it repeats (the tail
# round was run by a weaker model that sampled rather than read -- 5,839 rows for
# 84 findings against the deep pass's ten times that); and a recheck exists
# specifically to overturn the round it re-reads.
#
# Not filename order: that had put the low-frequency pass above the family pass
# and let it replace 射在地板上 with 精在地板上.
TARGETS = {
    "general": (TRANSLATIONS_DIR / "general_manual.json", TRANSLATIONS_DIR / "general_zh.json", (0, 5), ("fix[0-9]*.json", "fix_tail*.json", "fix_lowfreq*.json", "fix_deep*.json", "fix_family*.json", "fix_recheck*.json", "fix_dup*.json")),
    # Two rounds, unpadded then padded: the second covers 4,800 names against the
    # first's 753, and where they overlap the wider view is the later word. Glob
    # order alone would not say that -- `_review5` sorts before `_review_char`.
    "character": (TRANSLATIONS_DIR / "character_manual.json", None, (4,), ("fix_char[0-9].json", "fix_char[0-9][0-9].json", "fix_cdup*.json")),
    # Franchise titles. No bulk file and no earlier round: this vocabulary had
    # never been reviewed at all, which is why 4,744 names shipped with a mix of
    # official titles, literal translations, and one entry standing in for its
    # whole series (`atelier_(series)` as 莱莎的炼金工房).
    "copyright": (TRANSLATIONS_DIR / "copyright_manual.json", None, (3,), ("fix_copy*.json", "fix_pdup*.json")),
    # 不是名字,是名字的零件:角色标签括号里的限定词,消歧时接到名字后面。没有分类可查
    # (限定词不是标签),所以那道闸门关掉 —— 分片成员检查还在,而它才是拦编造键的那道。
    "variant": (TRANSLATIONS_DIR / "character_variants.json", None, None, ("fix_q*.json",)),
    # 补空缺,不是改错误。前面那些目标面对的是「这个名字翻错了」,这个面对的是
    # 「这个标签根本没有中文名」—— ≥300 投稿的标签里有 6,321 个是这样,多数不是
    # 漏翻,而是从来没进过任何一轮。写进 zh_supplement 是因为它们是*翻译*出来的,
    # 不是从别名池里*挑*出来的,两种来源的可信度不同,也该分开回滚。
    "supplement": (TRANSLATIONS_DIR / "zh_supplement.json", None, (0, 3, 4, 5), ("fix_cp[0-9].json", "fix_gm[0-9].json", "fix_ch[0-9].json", "fix_lg[0-9].json")),
}


# Formats settled in earlier rounds. A reviewer shown 573 rows cannot see that
# `bad_pixiv_id` reads 失效Pixiv ID twelve shards away, so it proposes 劣质Daum ID
# and the family splits again. Reported, never auto-applied: these are the shapes
# the vocabulary settled on, not laws, and a real exception should be visible.
# 「lift」不总是掀起衣物:在身体部位上是抬起或托起(抬腿、乳房上托、托臀、撩发),
# 而 wind_lift 掀的是衣服,主语才是风。
LIFT_EXCEPTIONS = frozenset({"leg_lift", "breast_lift", "ass_lift", "pectoral_lift", "hair_lift", "chin_lift", "arm_lift", "wind_lift"})

FAMILY_FORMATS = (
    (lambda t: t.startswith("bad_") and t.endswith("_id"), lambda v: v.startswith("失效"), "bad_*_id 用「失效X」"),
    # cord_pull 是例外:`cord` 本身叫「拉绳」,照规则拼出来是「拉拉绳」,读不通。
    (lambda t: t.endswith("_pull") and t != "cord_pull", lambda v: v.startswith("拉"), "*_pull 用「拉X」"),
    (lambda t: t.endswith("_tug"), lambda v: v.startswith("拉扯"), "*_tug 用「拉扯X」"),
    # 例外见 LIFT_EXCEPTIONS。原先没有这道例外,于是把 6 个正确的名字报成违规 ——
    # 而它当时是死代码,没人看见。
    (lambda t: t.endswith("_lift") and t not in LIFT_EXCEPTIONS, lambda v: v.startswith("掀起"), "*_lift 用「掀起X」"),
    (lambda t: t.startswith("spoken_"), lambda v: v.startswith("对话框"), "spoken_* 用「对话框X」"),
    (lambda t: t.endswith("_(medium)"), lambda v: v.endswith("（媒介）"), "*_(medium) 用「X（媒介）」"),
    (lambda t: t.startswith("cum_on_"), lambda v: v.startswith("射在"), "cum_on_* 用「射在X上」"),
    (lambda t: t.startswith("unworn_"), lambda v: v.startswith(("未穿", "未戴")), "unworn_* 用「未穿/未戴的X」"),
    (lambda t: t.endswith("_censor") and t != "bar_censor", lambda v: "打码" in v or "遮挡" in v or v == "圣光", "*_censor 用「X打码」"),
)


# 消歧层自己会给撞名的角色接上「（限定词）」。审查看到的是接好之后的值,于是提案往往
# 把那个后缀一并抄了回来 —— 而人工层跑在消歧之前,原样收下就会接第二次,得到
# 「白子（泳装）（泳装）」。带括号限定词的标签只收基础名,后缀交回给消歧层。
# 作品名不走这条:那边没有消歧层,而「（系列）」「（动画）」本来就是名字的一部分。
BRACKETED = re.compile(r"\(([^()]+)\)")
TRAILING_QUALIFIER = re.compile(r"（([^（）]+)）$")
_VARIANTS = json.loads((TRANSLATIONS_DIR / "character_variants.json").read_text(encoding="utf-8"))
_COPYRIGHT_ZH = json.loads((TRANSLATIONS_DIR / "copyright_name_map.json").read_text(encoding="utf-8"))


def pipeline_labels(tag: str) -> set[str]:
    """消歧层给这个标签接得出来的括注,穷举一遍。

    读的是消歧层自己读的那两张表,所以它和产出永远一致 —— 换句话说,这里判定
    「能重现」的,就是剥掉之后一定会被原样接回来的。
    """
    labels = set()
    for qualifier in BRACKETED.findall(tag):
        for table in (_VARIANTS, _COPYRIGHT_ZH):
            if qualifier in table:
                labels.add(table[qualifier] if isinstance(table[qualifier], str) else table[qualifier].get("zh_hans"))
    return {label for label in labels if label}


def strip_qualifier(tag: str, value: str) -> str:
    """剥掉审查抄回来的括注,只剥流水线自己会接回去的那一份。

    审查看到的是消歧接好之后的值,所以提案常把后缀一并抄回来;原样收下就会接第二次,
    得到「白子（泳装）（泳装）」。但不能见括号就剥:`yorck_(azur_lane)` 和
    `york_(azur_lane)` 都叫约克,审查按阵营分成了「约克（铁血）」和「约克（皇家）」,
    而消歧接得出来的只有「碧蓝航线」—— 剥掉那两个后缀,两条又并到一起了,
    审查白做。所以只剥能重现的。
    """
    match = TRAILING_QUALIFIER.search(value)
    if not match:
        return value
    labels = {part for part in match.group(1).split("·")}
    return TRAILING_QUALIFIER.sub("", value) if labels <= pipeline_labels(tag) else value


def format_warnings(accepted: dict[str, str]) -> list[str]:
    out = []
    for tag, value in sorted(accepted.items()):
        for matches, ok, rule in FAMILY_FORMATS:
            if matches(tag) and not ok(value):
                out.append(f"{tag}: {value}  ({rule})")
    return out


def load_reviewed_tags() -> set[str]:
    """Every tag any shard actually put in front of a reviewer.

    The guard this backs is against invented tag names: an agent returning a
    plausible-looking key produces an entry that matches nothing and is never
    noticed again. Shard layouts differ between rounds -- the first keyed on
    `current_zh`, the recheck rounds carry `before`/`after`, the family round
    groups by word root -- so only the tag column is read.
    """
    tags: set[str] = set()
    for directory in REVIEW_DIRS:
        for path in sorted(directory.glob("*.tsv")):
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle, delimiter=DELIMITER):
                    # 限定词分片的第一列叫 qualifier,不是 tag。同一道防线,同一个理由:
                    # 编出来的键匹配不上任何东西,谁也不会再注意到它。
                    key = row.get("tag") or row.get("qualifier")
                    if key:
                        tags.add(key)
    return tags


def categories() -> dict[str, int]:
    con = sqlite3.connect(f"file:{DANBOORU_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT name, category FROM tags"))
    finally:
        con.close()


def collect(
    reviewed: set[str],
    category: dict[str, int],
    current: dict[str, str],
    wanted_categories: tuple[int, ...] | None,
    rounds: tuple[str, ...],
    strip: bool = False,
    gap_only: bool = False,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Every accepted proposal, plus why each rejected one was dropped.

    A later round overrules an earlier one for the same tag; see TARGETS for
    what "later" means and why it is not filename order.
    """
    accepted: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    seen: dict[str, str] = {}

    def reject(reason: str, detail: str) -> None:
        rejected.setdefault(reason, []).append(detail)

    for pattern in rounds:
        for path in sorted(q for directory in REVIEW_DIRS for q in directory.glob(pattern)):
            data = json.loads(path.read_text(encoding="utf-8"))
            for tag, raw in data.get("fixes", {}).items():
                value = raw.strip() if isinstance(raw, str) else ""
                if value and strip:
                    value = strip_qualifier(tag, value)
                if tag not in reviewed:
                    reject("tag was never in a shard", f"{path.name}: {tag}")
                elif wanted_categories is not None and category.get(tag) not in wanted_categories:
                    reject("wrong category for this target", f"{tag} (category {category.get(tag)})")
                elif not value:
                    reject("empty or non-string", tag)
                elif gap_only and current.get(tag):
                    # 这一轮只填空缺。zh_supplement 是会*覆盖*名字映射的(见
                    # load_zh_supplement 的注释),所以一条针对已有名字的提案会悄悄
                    # 盖掉前几轮审查过的结果 —— 而分片是按「当前没有中文名」挑的,
                    # 有名字就说明这条不在任务范围内。
                    reject("already has a name; this round only fills gaps", f"{tag}: {current[tag]} -> {value}")
                elif value == (strip_qualifier(tag, current[tag]) if strip and current.get(tag) else current.get(tag)):
                    # 两边都剥掉括号后缀再比。后缀是消歧层加的,审查看到的是加完的值,
                    # 于是「把后缀去掉」会被当成一处改动 —— 一个 shard 里就有上百条,
                    # 合进去只是在人工层堆一堆什么都不改的条目。
                    reject("unchanged", tag)
                elif not is_simplified(value):
                    reject("not canonical simplified Chinese", f"{tag}: {current.get(tag)} -> {value}")
                else:
                    if tag in seen and accepted[tag] != value:
                        reject("superseded by a later round", f"{tag}: {accepted[tag]} ({seen[tag]}) -> {value} ({path.name})")
                    seen[tag] = path.name
                    accepted[tag] = value
    return accepted, rejected


def merge_rejections(reviewed: set[str], category: dict[str, int], apply: bool) -> None:
    """把审查的「不」写进 zh_rejected.json。

    补空缺那一轮留下的空,只在本项目里是空。下游 pictoria 有一份 2024 年冻结的机器
    翻译基线,上游没有名字它就继续显示自己那份 —— `omori` 显示「大森」(把游戏名当成
    日本姓氏读)、`voiceroid` 显示「声库音」(凭空造的词)。空覆盖不掉它,显式的 null 才行。

    记下被拒绝的那个值而不只是标签名:三年后想知道当初否掉的是什么,得有东西可查。
    """
    legacy: dict[str, str] = {}
    for directory in REVIEW_DIRS:
        for path in sorted(directory.glob("lg*.tsv")):
            with path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle, delimiter=DELIMITER):
                    if row.get("tag") and row.get("legacy_zh"):
                        legacy[row["tag"]] = row["legacy_zh"]

    path = TRANSLATIONS_DIR / "zh_rejected.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    added, skipped = {}, []
    for directory in REVIEW_DIRS:
        for source in sorted(directory.glob("fix_lg*.json")):
            for tag in json.loads(source.read_text(encoding="utf-8")).get("reject", []):
                if tag not in reviewed:
                    skipped.append(f"{source.name}: {tag} 不在任何分片里")
                elif category.get(tag) not in (0, 3, 5):
                    skipped.append(f"{tag} 分类 {category.get(tag)} 不在本轮范围")
                elif tag in existing:
                    skipped.append(f"{tag} 已在拒绝表里")
                elif tag not in legacy:
                    skipped.append(f"{tag} 查不到被拒绝的值")
                else:
                    added[tag] = legacy[tag]
    print(f"rejections proposed: {len(added):,}")
    for line in skipped[:8]:
        print(f"      skipped -- {line}")
    if not apply:
        print("")
        print(f"--apply not given; {path.name} untouched")
        return
    merged = {**existing, **added}
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + chr(10), encoding="utf-8")
    print("")
    print(f"{path}: {len(existing):,} -> {len(merged):,} entries")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the manual file (default: report only)")
    parser.add_argument("--target", choices=sorted(TARGETS), default="general")
    parser.add_argument("--check-shipped", action="store_true", help="also report format drift in what already ships")
    parser.add_argument("--rejections", action="store_true", help="merge the reject lists into zh_rejected.json instead")
    args = parser.parse_args()

    manual_file, bulk_file, wanted, rounds = TARGETS[args.target]
    reviewed = load_reviewed_tags()
    if args.rejections:
        merge_rejections(reviewed, categories(), args.apply)
        return
    if not reviewed:
        message = f"no shards under {', '.join(str(d) for d in REVIEW_DIRS)}"
        raise SystemExit(message)
    manual = json.loads(manual_file.read_text(encoding="utf-8")) if manual_file.exists() else {}
    bulk = json.loads(bulk_file.read_text(encoding="utf-8")) if bulk_file else {}
    if args.target == "variant":
        bulk = {}
    elif bulk_file is None:
        # No bulk file for proper nouns -- the value being reviewed is whatever
        # the resolved export currently ships, which is where the alias-pool
        # heuristic's answer ends up.
        resolved = json.loads((TRANSLATIONS_DIR / "display_names.json").read_text(encoding="utf-8"))
        bulk = {t: v["zh_hans"] for t, v in resolved.items() if v.get("zh_hans")}
    accepted, rejected = collect(
        reviewed,
        categories(),
        {**bulk, **manual},
        wanted,
        rounds,
        strip=args.target == "character",
        gap_only=args.target == "supplement",
    )

    print(f"tags reviewed: {len(reviewed):,}, proposals accepted: {len(accepted):,}")
    for reason, items in sorted(rejected.items(), key=lambda kv: -len(kv[1])):
        print(f"  rejected -- {reason}: {len(items)}")
        for item in items[:6]:
            print(f"      {item}")

    # Hand-fixed entries were made with the whole picture in view; a shard agent
    # saw 1,932 rows. Keep what is already there and say which ones differed.
    overlap = {t: (manual[t], accepted[t]) for t in accepted if t in manual and manual[t] != accepted[t]}
    if overlap:
        print(f"  kept existing hand-fixed value for {len(overlap)} tag(s):")
        for tag, (was, proposed) in list(overlap.items())[:10]:
            print(f"      {tag}: {was} (not {proposed})")
        for tag in overlap:
            del accepted[tag]

    # 报告,不自动改 —— 见 FAMILY_FORMATS。这个函数定义好之后有一阵子没人调用,
    # 于是这道检查存在、测试也在跑,却对谁都不说话。
    warnings = format_warnings(accepted)
    if warnings:
        print(f"  与既有族格式不符 {len(warnings)} 条(仅报告,未拦截):")
        for line in warnings[:12]:
            print(f"      {line}")

    # 已发布的名字也扫一遍。只查新提案的话,历史欠账永远看不见 —— 这些规则是照着
    # 语料写出来的,而语料里违反它们的有 63 条,包括 20 个 bad_*_id 里唯一那条
    # 「坏drawr ID」。它们能留下来,正是因为写规则的那一轮里这个函数没人调用。
    if args.check_shipped:
        shipped = {t: v for t, v in {**bulk, **manual}.items() if isinstance(v, str)}
        stale = format_warnings(shipped)
        print(f"  已发布语料里不符族格式的: {len(stale)}")
        for line in stale[:20]:
            print(f"      {line}")

    if not args.apply:
        print(f"\n--apply not given; {manual_file.name} untouched")
        return

    merged = {**manual, **accepted}
    manual_file.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n{manual_file}: {len(manual):,} -> {len(merged):,} entries")


if __name__ == "__main__":
    main()
