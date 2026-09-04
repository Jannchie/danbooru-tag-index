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

REVIEW_DIR = TRANSLATIONS_DIR / "_review_general"
MANUAL_FILE = TRANSLATIONS_DIR / "general_manual.json"
ORDINARY_WORD_CATEGORIES = (0, 5)


def load_shards() -> dict[str, dict[str, str]]:
    """Shard name -> {tag: current translation}, as the agents were given it."""
    shards: dict[str, dict[str, str]] = {}
    for path in sorted(REVIEW_DIR.glob("shard*.tsv")):
        with path.open(encoding="utf-8", newline="") as handle:
            shards[path.stem] = {r["tag"]: r["current_zh"] for r in csv.DictReader(handle, delimiter="\t")}
    return shards


def categories() -> dict[str, int]:
    con = sqlite3.connect(f"file:{DANBOORU_DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return dict(con.execute("SELECT name, category FROM tags"))
    finally:
        con.close()


def collect(shards: dict[str, dict[str, str]], category: dict[str, int]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Every accepted proposal, plus why each rejected one was dropped."""
    accepted: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    seen: dict[str, str] = {}

    def reject(reason: str, detail: str) -> None:
        rejected.setdefault(reason, []).append(detail)

    for path in sorted(REVIEW_DIR.glob("fix*.json")):
        shard = shards.get(path.stem.replace("fix", "shard"), {})
        data = json.loads(path.read_text(encoding="utf-8"))
        for tag, raw in data.get("fixes", {}).items():
            if tag not in shard:
                reject("tag not in this shard", f"{path.name}: {tag}")
            elif tag in seen:
                reject("tag proposed by two shards", f"{tag}: {seen[tag]} / {path.name}")
            elif category.get(tag) not in ORDINARY_WORD_CATEGORIES:
                reject("not a general/meta tag", f"{tag} (category {category.get(tag)})")
            elif not isinstance(raw, str) or not raw.strip():
                reject("empty or non-string", tag)
            elif raw.strip() == shard[tag]:
                reject("unchanged", tag)
            elif not is_simplified(raw.strip()):
                reject("not canonical simplified Chinese", f"{tag}: {shard[tag]} -> {raw.strip()}")
            else:
                seen[tag] = path.name
                accepted[tag] = raw.strip()
    return accepted, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write general_manual.json (default: report only)")
    args = parser.parse_args()

    shards = load_shards()
    if not shards:
        message = f"no shards in {REVIEW_DIR}"
        raise SystemExit(message)
    manual = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
    accepted, rejected = collect(shards, categories())

    print(f"shards: {len(shards)}, proposals accepted: {len(accepted):,}")
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
