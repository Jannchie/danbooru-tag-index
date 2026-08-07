"""Stage 1: rebuild everything that needs the Danbooru metadata database.

The database is 46 GB, so it cannot travel to CI. This script runs on the machine
that holds it and produces the handful of small artifacts CI needs to build the
bundle:

    dim_tag.parquet              tag id, name, category, post count
    fact_tag_monthly.parquet     the index itself (~30 MB)
    fact_total_monthly.parquet   site-wide monthly baseline
    fact_category_monthly.parquet
    wiki_other_names.json        alias pool for search (~2.3 MB)
    {character,copyright,artist}_names.json   wiki aliases bucketed by language

CI combines those with the committed translation data to produce the bundle. The
split is what keeps a 46 GB dependency out of the build: nothing downstream of
here opens the database.

The daily-grain fact table is deliberately not published -- it is 200 MB and the
web client only ever reads monthly.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

from _paths import DANBOORU_DB_PATH, INDEX_DIR, SCRIPTS_DIR, TRANSLATIONS_DIR

# Order matters: the alias export reads dim_tag.parquet that the index build writes.
STAGES = (
    ("build_tag_index.py", "scan posts into the monthly index"),
    ("export_wiki_aliases.py", "extract the wiki alias pool"),
    ("export_tag_translations.py", "bucket wiki aliases by language"),
)

# (directory, filename) -- CI downloads exactly this set and needs nothing else
# from the database side.
PUBLISH = (
    (INDEX_DIR, "dim_tag.parquet"),
    (INDEX_DIR, "fact_tag_monthly.parquet"),
    (INDEX_DIR, "fact_total_monthly.parquet"),
    (INDEX_DIR, "fact_category_monthly.parquet"),
    (INDEX_DIR, "wiki_other_names.json"),
    (TRANSLATIONS_DIR, "character_names.json"),
    (TRANSLATIONS_DIR, "copyright_names.json"),
    (TRANSLATIONS_DIR, "artist_names.json"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Rebuild the database-dependent artifacts (stage 1).")
    parser.add_argument("--database", type=str, default=str(DANBOORU_DB_PATH))
    parser.add_argument("--resume", action="store_true", help="Continue an interrupted index scan.")
    parser.add_argument("--skip-index", action="store_true", help="Reuse the existing parquets; only re-export.")
    return parser.parse_args()


def run(script: str, args: list[str]) -> None:
    command = [sys.executable, str(SCRIPTS_DIR / script), *args]
    print(f"\n$ {' '.join(command[1:])}", flush=True)
    subprocess.run(command, check=True)


def main() -> None:
    args = parse_args()
    database = Path(args.database)
    if not database.exists():
        raise SystemExit(f"database not found: {database}\nSet DANBOORU_DB or pass --database.")

    started = time.monotonic()
    for script, what in STAGES:
        if script == "build_tag_index.py" and args.skip_index:
            print(f"\n(skipping {script} -- {what})")
            continue
        extra = ["--resume"] if (args.resume and script == "build_tag_index.py") else []
        run(script, ["--database", str(database), *extra])

    paths = [directory / name for directory, name in PUBLISH]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise SystemExit(f"stage 1 finished but these artifacts are missing: {missing}")

    total = sum(path.stat().st_size for path in paths)
    print(f"\nstage 1 done in {(time.monotonic() - started) / 60:.1f} min")
    print(f"artifacts to publish ({total / 1e6:.1f} MB):")
    for path in paths:
        print(f"  {path}  ({path.stat().st_size / 1e6:.1f} MB)")
    print("\nupload with:")
    print("  gh release upload data-latest \\\n    " + " \\\n    ".join(str(path) for path in paths) + " --clobber")


if __name__ == "__main__":
    main()
