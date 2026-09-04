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

REVIEW_DIRS = (TRANSLATIONS_DIR / "_review_general", TRANSLATIONS_DIR / "_review2")

# Rounds in ascending order of authority, because they see different amounts.
# A single-entry pass judges one row at a time; the word-family pass gets every
# tag sharing a root laid out together, which is the only way to see that
# `dress_tug` was translated as a tugboat while `skirt_tug` was not; the recheck
# pass exists specifically to overturn what an earlier round decided.
#
# Not filename order -- that put the low-frequency pass above the family pass and
# let it replace 射在地板上 with 精在地板上.
ROUND_ORDER = ("fix[0-9]*.json", "fix_lowfreq*.json", "fix_family*.json", "fix_recheck*.json")
MANUAL_FILE = TRANSLATIONS_DIR / "general_manual.json"
BULK_FILE = TRANSLATIONS_DIR / "general_zh.json"
ORDINARY_WORD_CATEGORIES = (0, 5)


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
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Every accepted proposal, plus why each rejected one was dropped.

    A later round overrules an earlier one for the same tag; see ROUND_ORDER for
    what "later" means and why it is not filename order.
    """
    accepted: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    seen: dict[str, str] = {}

    def reject(reason: str, detail: str) -> None:
        rejected.setdefault(reason, []).append(detail)

    for pattern in ROUND_ORDER:
        for path in sorted(q for directory in REVIEW_DIRS for q in directory.glob(pattern)):
            data = json.loads(path.read_text(encoding="utf-8"))
            for tag, raw in data.get("fixes", {}).items():
                value = raw.strip() if isinstance(raw, str) else ""
                if tag not in reviewed:
                    reject("tag was never in a shard", f"{path.name}: {tag}")
                elif category.get(tag) not in ORDINARY_WORD_CATEGORIES:
                    reject("not a general/meta tag", f"{tag} (category {category.get(tag)})")
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
    parser.add_argument("--apply", action="store_true", help="write general_manual.json (default: report only)")
    args = parser.parse_args()

    reviewed = load_reviewed_tags()
    if not reviewed:
        message = f"no shards under {', '.join(str(d) for d in REVIEW_DIRS)}"
        raise SystemExit(message)
    manual = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    bulk = json.loads(BULK_FILE.read_text(encoding="utf-8"))
    accepted, rejected = collect(reviewed, categories(), {**bulk, **manual})

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
        print("\n--apply not given; general_manual.json untouched")
        return

    merged = {**manual, **accepted}
    MANUAL_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"\n{MANUAL_FILE}: {len(manual):,} -> {len(merged):,} entries")


if __name__ == "__main__":
    main()
