"""Project paths.

The layout mirrors the two build stages. Everything under `data/index/` is a
build artifact reproducible from the Danbooru metadata database; everything under
`data/translations/` is either committed LLM/human output or regenerated from
that database. Only the first stage needs the database at all -- see README.
"""

import os
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
ROOT = SCRIPTS_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data"
INDEX_DIR = DATA_DIR / "index"
TRANSLATIONS_DIR = DATA_DIR / "translations"

# The 46 GB metadata database lives outside this repository -- it is the other
# project's output. Point DANBOORU_DB at it, or keep the default sibling layout.
DANBOORU_DB_PATH = Path(os.environ.get("DANBOORU_DB", ROOT.parent / "danbooru_metadata" / "data" / "db" / "danbooru_metadata.db"))
TAG_INDEX_STAGING_PATH = DATA_DIR / "tag_index_staging.duckdb"
