"""Import Chinese display names for general and meta tags from a translation table.

    uv run python scripts/import_general_names.py --source <path>/tag.zh-Hans.json

General tags shipped under their English slug in every language, which left the
most-used half of the vocabulary untranslated for four of the five audiences.
The alias pool cannot fill it -- deriving display names from it was tried and
reverted, because it is built for recall and will happily claim `school_uniform`
means "制服スパッツ".

A translation of the slug is a different proposition, and the split this script
enforces is the whole point:

  general and meta are ordinary words. `long_hair`, `blush`, `tsundere` have no
  official rendering to get wrong; any competent translator produces the same
  answer, and a cheap model is competent at it.

  copyright and character are proper nouns. There the same table gives
  `kemono_friends` as 兽之朋友 and `dark_souls_(series)` as 黑暗灵魂 -- both
  plausible, both not what anyone calls them. That is exactly the failure this
  project already documented: a model that will not decline invents a name, and
  a wrong name is worse than none. Those categories keep the reviewed name maps.

So this imports categories 0 and 5 only, and refuses anything else even if the
source has it. Traditional Chinese is not imported: fill_cjk derives it from the
simplified name, which is a conversion rather than a second guess.
"""

import argparse
import json
from pathlib import Path

import duckdb

from _paths import INDEX_DIR, TRANSLATIONS_DIR

OUTPUT = "general_zh.json"

# 0 = general, 5 = meta. Named here rather than inlined so that widening the
# import is a deliberate edit next to the docstring explaining why it should not be.
ORDINARY_WORD_CATEGORIES = (0, 5)

HAN = tuple(
    (chr(a), chr(b))
    for a, b in ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF))
)


def has_han(text: str) -> bool:
    return any(lo <= ch <= hi for ch in text for lo, hi in HAN)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import general/meta Chinese names.")
    parser.add_argument("--source", type=str, required=True, help="tag.zh-Hans.json to read.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--translations-dir", type=str, default=str(TRANSLATIONS_DIR))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = json.loads(Path(args.source).read_text(encoding="utf-8"))

    con = duckdb.connect()
    dim = (Path(args.index_dir) / "dim_tag.parquet").as_posix()
    rows = con.execute(
        f"SELECT name, category FROM read_parquet('{dim}') WHERE category IN {ORDINARY_WORD_CATEGORIES}"
    ).fetchall()
    con.close()

    out: dict[str, str] = {}
    skipped_same = skipped_latin = 0
    for name, _category in rows:
        value = source.get(name)
        if not value or value == name:
            skipped_same += 1        # untranslated entries echo the slug back
            continue
        if not has_han(value):
            skipped_latin += 1       # a "translation" with no Han is the slug in disguise
            continue
        out[name] = value

    path = Path(args.translations_dir) / OUTPUT
    path.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{path}: {len(out):,} of {len(rows):,} general/meta tags ({len(out) / len(rows):.0%})")
    print(f"  skipped: {skipped_same:,} untranslated, {skipped_latin:,} without Han characters")
    print(f"  {path.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
