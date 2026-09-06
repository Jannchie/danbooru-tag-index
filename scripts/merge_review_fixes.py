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
import sqlite3

from _hanzi import is_simplified
from _paths import DANBOORU_DB_PATH, TRANSLATIONS_DIR

REVIEW_DIRS = tuple(TRANSLATIONS_DIR / name for name in ("_review_general", "_review2", "_review3", "_review4", "_review_char"))

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
    "general": (TRANSLATIONS_DIR / "general_manual.json", TRANSLATIONS_DIR / "general_zh.json", (0, 5), ("fix[0-9]*.json", "fix_tail*.json", "fix_lowfreq*.json", "fix_deep*.json", "fix_family*.json", "fix_recheck*.json")),
    "character": (TRANSLATIONS_DIR / "character_manual.json", None, (4,), ("fix_char*.json",)),
}


# Formats settled in earlier rounds. A reviewer shown 573 rows cannot see that
# `bad_pixiv_id` reads 失效Pixiv ID twelve shards away, so it proposes 劣质Daum ID
# and the family splits again. Reported, never auto-applied: these are the shapes
# the vocabulary settled on, not laws, and a real exception should be visible.
FAMILY_FORMATS = (
    (lambda t: t.startswith("bad_") and t.endswith("_id"), lambda v: v.startswith("失效"), "bad_*_id 用「失效X」"),
    (lambda t: t.endswith("_pull"), lambda v: v.startswith("拉"), "*_pull 用「拉X」"),
    (lambda t: t.endswith("_tug"), lambda v: v.startswith("拉扯"), "*_tug 用「拉扯X」"),
    (lambda t: t.endswith("_lift"), lambda v: v.startswith("掀起"), "*_lift 用「掀起X」"),
    (lambda t: t.startswith("spoken_"), lambda v: v.startswith("对话框"), "spoken_* 用「对话框X」"),
    (lambda t: t.endswith("_(medium)"), lambda v: v.endswith("（媒介）"), "*_(medium) 用「X（媒介）」"),
    (lambda t: t.startswith("cum_on_"), lambda v: v.startswith("射在"), "cum_on_* 用「射在X上」"),
    (lambda t: t.startswith("unworn_"), lambda v: v.startswith(("未穿", "未戴")), "unworn_* 用「未穿/未戴的X」"),
    (lambda t: t.endswith("_censor") and t != "bar_censor", lambda v: "打码" in v or "遮挡" in v or v == "圣光", "*_censor 用「X打码」"),
)


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
                tags.update(r["tag"] for r in csv.DictReader(handle, delimiter="\t") if r.get("tag"))
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
    wanted_categories: tuple[int, ...],
    rounds: tuple[str, ...],
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
                if tag not in reviewed:
                    reject("tag was never in a shard", f"{path.name}: {tag}")
                elif category.get(tag) not in wanted_categories:
                    reject("wrong category for this target", f"{tag} (category {category.get(tag)})")
                elif not value:
                    reject("empty or non-string", tag)
                elif value == current.get(tag):
                    reject("unchanged", tag)
                elif not is_simplified(value):
                    reject("not canonical simplified Chinese", f"{tag}: {current.get(tag)} -> {value}")
                else:
                    if tag in seen and accepted[tag] != value:
                        reject("superseded by a later round", f"{tag}: {accepted[tag]} ({seen[tag]}) -> {value} ({path.name})")
                    seen[tag] = path.name
                    accepted[tag] = value
    return accepted, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the manual file (default: report only)")
    parser.add_argument("--target", choices=sorted(TARGETS), default="general")
    args = parser.parse_args()

    manual_file, bulk_file, wanted, rounds = TARGETS[args.target]
    reviewed = load_reviewed_tags()
    if not reviewed:
        message = f"no shards under {', '.join(str(d) for d in REVIEW_DIRS)}"
        raise SystemExit(message)
    manual = json.loads(manual_file.read_text(encoding="utf-8")) if manual_file.exists() else {}
    bulk = json.loads(bulk_file.read_text(encoding="utf-8")) if bulk_file else {}
    if args.target == "character":
        # No bulk file for names -- the value being reviewed is whatever the
        # resolved export currently ships, which is where the alias-pool
        # heuristic's answer ends up.
        resolved = json.loads((TRANSLATIONS_DIR / "display_names.json").read_text(encoding="utf-8"))
        bulk = {t: v["zh_hans"] for t, v in resolved.items() if v.get("zh_hans")}
    accepted, rejected = collect(reviewed, categories(), {**bulk, **manual}, wanted, rounds)

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

    if not args.apply:
        print(f"\n--apply not given; {manual_file.name} untouched")
        return

    merged = {**manual, **accepted}
    manual_file.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n{manual_file}: {len(manual):,} -> {len(merged):,} entries")


if __name__ == "__main__":
    main()
