"""Resolve the translation layers into one display name per tag, for consumers.

    uv run python scripts/export_display_names.py

Writes `data/translations/display_names.json`: `{tag: {en, ja, ko, zh_hans,
zh_hant}}` after every layer this project applies -- the reviewed name maps, the
general/meta vocabulary and its hand-fixes, the translated supplement, the
rejections, and the manual corrections, in that order.

The bundle already resolves those layers; this writes the same answer out for
things that are not the bundle. The pictoria image library merged the raw name
maps itself, which meant keeping a second copy of an order that only makes sense
with the comments attached to it (the supplement must beat the pool, a rejection
must beat the supplement, a hand-check must beat them all). That copy pointed at
a path this project no longer writes, printed `missing, skipped`, and shipped
unreviewed names for months. One resolved artifact removes the copy.

Unlike the bundle this keeps *every* tag, not just the ones above the index's
post-count floor: a library tags its own files and needs the long tail. And it
keeps the rejections as explicit nulls -- a consumer with its own fallback layers
has to be able to tell "no name" from "no opinion", or it fills the rejected name
straight back in.

Regenerable, so gitignored. The layers it reads are the committed part.
"""

import argparse
import json
from pathlib import Path

from _paths import TRANSLATIONS_DIR
from export_index_bundle import LANGS, build_converters, resolve_display_names

OUTPUT = "display_names.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--translations-dir", type=Path, default=TRANSLATIONS_DIR)
    parser.add_argument("--output", type=Path, help=f"default: <translations-dir>/{OUTPUT}")
    args = parser.parse_args()

    names = resolve_display_names(args.translations_dir, None, build_converters())
    # Drop tags left with nothing to say. A slot that is present and None is a
    # rejection and must survive; a tag with no slots at all is just absent.
    names = {tag: slots for tag, slots in names.items() if slots}

    output = args.output or args.translations_dir / OUTPUT
    output.write_text(json.dumps(names, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    counts = ", ".join(f"{lang}={sum(1 for v in names.values() if v.get(lang)):,}" for lang in LANGS)
    rejected = sum(1 for v in names.values() if "zh_hans" in v and v["zh_hans"] is None)
    print(f"{output}: {len(names):,} tags ({counts}; {rejected} rejected)")


if __name__ == "__main__":
    main()
