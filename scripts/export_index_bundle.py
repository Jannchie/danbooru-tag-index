"""Pack the monthly tag index into a single binary bundle for the web client.

The whole index is ~30 MB as Parquet, which is small enough that a site can just
download it once instead of querying a backend. But Parquet needs a WASM reader
(tens of MB) to open in a browser, so this writes a purpose-built format that
plain JavaScript can parse with a DataView and no dependencies.

The layout is defined by HEADER_FORMAT and RECORD_FORMAT alone -- every size is
computed from them and every section offset is written into the header, so a
reader never recomputes a position that the writer could change:

    header       64 B    magic, version, counts, section offsets, epoch, build date
    totals       n_months x u32       site-wide posts per month
    categories   per category: id, monthly posts, monthly n_eff
    tag table    n_tags x 24 B        fixed-width, sorted by name for binary search
    names        concatenated UTF-8 tag names
    i18n         per tag: a names block of (lang, text) pairs -- display names
    search       lowercased haystack, one \\n-delimited row per tag, in tag order
    values       per tag: `span` LEB128 varints, month-by-month from first_month

Each tag stores a contiguous run from its first to its last active month. Gaps
inside that run are stored as a zero, which costs one byte -- cheaper than the
month delta a sparse encoding would need, since 95% of values fit in one byte
anyway.

The header carries the epoch month and the range of complete months. Both are
properties of the data: a client that hardcoded the epoch would mislabel every
point on the x axis if the index were ever rebuilt from a different range, and
one that guessed at completeness would confuse a partial sync at either end with
a real change in activity.

It also carries the build date, as a plain YYYYMMDD integer. That is not a
property of the data but of this run, and it is here rather than in a sidecar
file so the page can show it without a second request -- the client already has
to fetch the bundle, and a date that arrives separately can disagree with the
data it describes. It occupies reserved header bytes, so an older client reads
past it unchanged and a newer one reading an older bundle sees 0, which it
renders as "unknown" rather than a wrong date.

The category section carries each category's monthly post total and `n_eff`
(the effective number of competitors, 1/HHI), which together let the client
compute a fragmentation-adjusted index. See build_tag_index.py for why a plain
share is not enough.

Multilingual names come from two sources with different jobs. The structured
`*_name_map.json` files give per-language display names, and are the *only*
thing shown as a translation. The wiki `other_names` alias pool is unlabelled
and built for recall rather than equivalence -- it lists whatever people call a
tag, including narrower and related terms -- so it feeds search only. That split
means general tags, which no name map covers, are findable in any language but
still display under their English tag name rather than a guessed translation.

`search` exists so the client can find a tag in any language without building an
index at startup: one native `indexOf` over a single ~4 MB string beats 74k
per-tag comparisons. Rows are in tag order, so the client splits on newlines once
and a binary search over those positions maps a hit back to its tag. The row
offsets are deliberately *not* stored -- they would be UTF-8 byte offsets, while
the client needs UTF-16 code-unit offsets, and rescanning costs ~15 ms once.
"""

import argparse
import json
import re
import struct
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import duckdb
import opencc

# Reuse the pipeline's script ranges rather than writing narrower copies: these
# include CJK compatibility ideographs and katakana phonetic extensions, and a
# private copy omitting them would classify a name differently from the pipeline
# that produced its translation.
from build_name_map import _HAN as HAS_CJK
from build_name_map import _KANA as HAS_KANA

from _paths import INDEX_DIR, TRANSLATIONS_DIR

MAGIC = b"DBIX"
VERSION = 4

# The format lives in these two strings. Sizes are derived, never restated in a
# comment, so a field added to either cannot silently break a reader's stride.
HEADER_FORMAT = "<4sHIHB3xIIIIIIIIHHHI6x"
RECORD_FORMAT = "<IBBBHHIIIx"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
TAG_RECORD_SIZE = struct.calcsize(RECORD_FORMAT)

# Slot order is part of the format; the client indexes into it.
LANGS = ("zh_hans", "zh_hant", "ja", "en", "ko")
LANG_CODE = {lang: i for i, lang in enumerate(LANGS)}
ALIAS_CODE = 255

NAME_MAP_FILES = ("character_name_map.json", "copyright_name_map.json", "artist_name_map.json")

# Chinese for the general and meta vocabulary, translated from the slug rather
# than selected from the alias pool. See load_general_names for why those two
# categories can take a translation the proper-noun ones cannot.
GENERAL_FILE = "general_zh.json"

# Hand-fixed entries for the file above. Danbooru's own vocabulary is the part a
# general-purpose translator gets wrong -- `commentary` is the artist's note, not
# a broadcast; `bad_id` is a dead upstream link; `absurdres` is a resolution.
# Separate file because import_general_names.py overwrites the bulk one.
GENERAL_MANUAL_FILE = "general_manual.json"

# Chinese names translated from knowledge rather than selected from Danbooru's
# alias pool. Applied last and only into empty slots -- see load_zh_supplement.
SUPPLEMENT_FILE = "zh_supplement.json"

# Hand-verified corrections. translate_merge.py folds these into the supplement,
# but only while merging an LLM batch -- so a correction made on its own would sit
# unapplied until the next translation run. Read directly, and last, because a
# name someone checked by hand outranks every automatic layer.
MANUAL_FILE = "zh_manual.json"

# Tags whose pool-derived Chinese name a review found wrong without finding a
# replacement. Separate from the supplement because it is the opposite assertion:
# that file says "the name is X", this one says "whatever the pool gave is not it".
REJECTED_FILE = "zh_rejected.json"

KATAKANA = re.compile(r"[ァ-ヶ]")

# A tag name's length is stored in one byte; this is a real format constraint,
# unlike an arbitrary cap on translated text.
MAX_NAME_BYTES = 0xFF


class Converters(NamedTuple):
    """Named, not positional: swapping these two silently inverts Han conversion."""

    to_simplified: Callable[[str], str]
    to_traditional: Callable[[str], str]


class Sections(NamedTuple):
    totals: bytes
    categories: bytes
    table: bytes
    names: bytes
    i18n: bytes
    search: bytes
    values: bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pack the monthly tag index into a single binary bundle.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--translations-dir", type=str, default=str(TRANSLATIONS_DIR))
    parser.add_argument("--wiki-aliases", type=str, default=None, help="Alias pool JSON (default: <index-dir>/wiki_other_names.json).")
    parser.add_argument("--output", type=str, default=None, help="Bundle path (default: <index-dir>/index_bundle.bin)")
    parser.add_argument("--min-post-count", type=int, default=0, help="Drop tags below this lifetime post count.")
    parser.add_argument("--no-i18n", action="store_true", help="Skip multilingual names entirely.")
    parser.add_argument("--built", type=str, default=None, help="Build date as YYYY-MM-DD (default: today, UTC).")
    return parser.parse_args()


def encode_varint(value: int, out: bytearray) -> None:
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def to_hiragana(text: str) -> str:
    """Fold katakana to hiragana so either kana input finds either spelling."""
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text)


def build_converters() -> Converters:
    return Converters(opencc.OpenCC("t2s").convert, opencc.OpenCC("s2t").convert)


def complete_month_range(totals: list[int]) -> tuple[int, int]:
    """Inclusive [first, last] month indices that hold a full month of uploads.

    Both ends can be partial. The last month is cut off wherever the sync
    stopped, and the first is cut off wherever the site started -- Danbooru
    opened late in 2005-05, so that month holds about a week of uploads and
    charts as a dip that never happened. Deciding this here keeps every consumer
    of the bundle from re-guessing it.
    """
    n = len(totals)
    if n < 14:
        return 0, n - 1

    def median(values: list[int]) -> float:
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    last = n - 2 if totals[-1] < median(totals[-13:-1]) * 0.6 else n - 1
    first = 1 if totals[0] < median(totals[1:13]) * 0.6 else 0
    return first, last


def load_name_maps(translations_dir: Path, wanted: set[str]) -> dict[str, dict[str, str]]:
    """Per-language display names, keyed by tag, restricted to tags we ship.

    Reports per-file and per-language coverage rather than failing on a missing
    file: any single map being absent or thin is a silent quality loss in the
    shipped bundle, which is otherwise only visible by searching for a tag that
    should have a translated name and finding nothing.
    """
    merged: dict[str, dict[str, str]] = {}
    for filename in NAME_MAP_FILES:
        path = translations_dir / filename
        if not path.exists():
            print(f"  WARNING: {filename} missing -- those tags ship without display names")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        used = 0
        for tag, names in data.items():
            if tag not in wanted:
                continue
            used += 1
            slot = merged.setdefault(tag, {})
            for lang, value in names.items():
                if lang in LANG_CODE and value and lang not in slot:
                    slot[lang] = str(value)
        print(f"  {filename}: {used:,} of {len(data):,} tags used")
    counts = ", ".join(f"{lang}={sum(1 for v in merged.values() if v.get(lang)):,}" for lang in LANGS)
    print(f"  display names by language: {counts}")
    return merged


def load_general_names(
    translations_dir: Path,
    wanted: set[str],
    name_maps: dict[str, dict[str, str]],
    converters: Converters | None,
) -> int:
    """Chinese display names for the general and meta vocabulary.

    These tags shipped under their English slug in every language, which left the
    most-used half of the vocabulary unreadable for four of the five audiences.
    The alias pool cannot fill it: deriving display names from it was tried and
    reverted, since it is built for recall and will claim `school_uniform` means
    "制服スパッツ".

    Translating the slug is a different proposition, and only for these two
    categories. `long_hair`, `blush` and `tsundere` are ordinary words with no
    official rendering to get wrong. A franchise or a character is a proper noun,
    where the same source offers 兽之朋友 for `kemono_friends` and 黑暗灵魂 for
    `dark_souls_(series)` -- plausible, and not what anyone calls them. Those
    categories keep their reviewed maps; see scripts/import_general_names.py.

    Fills only. Anything already carrying a Chinese name got it from a reviewed
    map or a hand-checked correction, and both outrank a bulk translation.
    """
    path = translations_dir / GENERAL_FILE
    if not path.exists():
        print(f"  WARNING: {GENERAL_FILE} missing -- general tags ship under their English slug")
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    manual_path = translations_dir / GENERAL_MANUAL_FILE
    manual = json.loads(manual_path.read_text(encoding="utf-8")) if manual_path.exists() else {}
    data = {**data, **manual}   # hand-checked wins over bulk
    added = 0
    for tag, zh in data.items():
        if tag not in wanted or not zh:
            continue
        slot = name_maps.setdefault(tag, {})
        if slot.get("zh_hans"):
            continue
        slot["zh_hans"] = str(zh)
        # Traditional is a conversion of the simplified name, not a second guess.
        if converters is not None and not slot.get("zh_hant"):
            slot["zh_hant"] = converters.to_traditional(str(zh))
        added += 1
    print(f"  {GENERAL_FILE}: {added:,} general/meta tags given a Chinese name ({len(manual)} hand-fixed)")
    return added


def load_zh_supplement(
    translations_dir: Path,
    wanted: set[str],
    name_maps: dict[str, dict[str, str]],
    converters: Converters | None,
    filename: str = SUPPLEMENT_FILE,
) -> int:
    """Fill Chinese display names the wiki-derived maps have no source for.

    Applied here rather than in build_name_map.py because this is where a tag's
    category is known: 105 of the tags needing a Chinese name have no wiki
    `other_names` at all, so they appear in no `*_names.json` and no per-category
    map could carry them.

    It is a separate file because it is *translated* from knowledge, while the
    name maps are *selected* from Danbooru's alias pool -- a different trust
    level, separately auditable and separately revertible. Only Chinese slots are
    touched: deriving a Japanese title from a Chinese one would invent a name that
    does not exist.

    The supplement *wins* over the map rather than only filling gaps, because the
    pool can be wrong and not merely absent: `dark_souls_(series)` carried
    黑暗靈魂, a literal translation someone put in the wiki, while 黑暗之魂 -- the
    name everyone actually uses -- sat in the same pool misfiled under Japanese.
    A layer that could only fill gaps could never correct that.
    """
    path = translations_dir / filename
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    filled = corrected = 0
    for tag, zh in data.items():
        if tag not in wanted or not zh:
            continue
        zh = str(zh)
        slot = name_maps.setdefault(tag, {})
        previous = slot.get("zh_hans")
        if previous == zh:
            continue
        slot["zh_hans"] = zh
        traditional = slot.get("zh_hant")
        # Follow along only when the traditional name was itself converted from the
        # simplified one. An independent regional title -- blue_archive is 碧蓝档案
        # in the mainland and 蔚藍檔案 in Taiwan -- is real data, not a conversion
        # artifact, and must survive a change to the simplified name.
        derived = converters is not None and (not traditional or (previous and converters.to_simplified(traditional) == previous))
        if derived and converters is not None:
            slot["zh_hant"] = converters.to_traditional(zh)
        if previous:
            corrected += 1
        else:
            filled += 1
    print(f"  {filename}: {filled:,} Chinese names filled in, {corrected:,} corrected")
    return filled + corrected


def load_zh_rejections(
    translations_dir: Path,
    wanted: set[str],
    name_maps: dict[str, dict[str, str]],
    converters: Converters | None,
) -> int:
    """Drop Chinese display names a review rejected without finding a replacement.

    A reviewer can be certain a name is wrong and still not know the right one:
    `anchovy_(girls_und_panzer)` carried 队长组, which is a pairing tag, and
    `selene_(pokemon)` carried SM♀主. `zh_supplement` can only assert a name, so
    with that file alone the wrong one survives -- the review's "no" has nowhere
    to go. Falling back to the English tag name is the better failure, by the same
    rule the rest of this pipeline follows: a wrong name is worse than none.

    Both Chinese slots go. The verdict is about the entity -- there is no name --
    not about one script, and keeping the traditional slot let `build_sections`
    derive the simplified one straight back: `nugget_(project_moon)` held 自職員
    (a Japanese value misfiled as traditional), so dropping only zh_hans produced
    自职员 on the next line instead of the English fallback.
    """
    path = translations_dir / REJECTED_FILE
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    tags = data if isinstance(data, list) else list(data)
    dropped = 0
    for tag in tags:
        if tag not in wanted:
            continue
        slot = name_maps.get(tag)
        if not slot or not slot.get("zh_hans"):
            continue
        slot.pop("zh_hans")
        slot.pop("zh_hant", None)
        dropped += 1
    print(f"  {REJECTED_FILE}: {dropped:,} Chinese names dropped as wrong")
    return dropped


def load_other_names(path: Path, wanted: set[str]) -> dict[str, list[str]]:
    """Wiki alias pool -- unlabelled by language, so search-only.

    Read from the JSON that `export_wiki_aliases.py` extracts, not from the source
    database: that database is 46 GB, and needing it here would force the whole
    bundle build onto the one machine that stores it. The pool for the shipped
    tags is 2.3 MB, so it travels as a build artifact instead.

    The pool covers general tags too, which no name map reaches -- and those are
    exactly the ones that need alias search the most.

    Missing is a hard failure, not a warning. Without the pool 11,634 tags lose
    multilingual search while every count the build prints stays plausible and the
    run goes green -- which is exactly how a CI glob that swept
    `wiki_other_names.json` in with `*_names.json` shipped a degraded bundle once
    already. Build without translations with `--no-i18n` if that is the intent.
    """
    if not path.exists():
        raise SystemExit(
            f"alias pool not found: {path}\n"
            "Run scripts/export_wiki_aliases.py (stage 1), point --wiki-aliases at it, "
            "or pass --no-i18n to build without translations."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {tag: [str(n) for n in names if n] for tag, names in data.items() if tag in wanted and names}
    print(f"  {path.name}: alias pools for {len(out):,} tags")
    return out


def search_variants(text: str, converters: Converters | None) -> list[str]:
    """Extra spellings so a query in one script finds a name written in another."""
    variants = []
    if KATAKANA.search(text):
        variants.append(to_hiragana(text))
    # Han conversion only where there is Han and no kana: converting Japanese
    # text that mixes kana would mangle it for no search benefit.
    if converters and HAS_CJK.search(text) and not HAS_KANA.search(text):
        variants.append(converters.to_simplified(text))
        variants.append(converters.to_traditional(text))
    return variants


def load_series(con: duckdb.DuckDBPyConnection, index_dir: Path, min_post_count: int) -> tuple[list[tuple], list[int], str]:
    monthly = (index_dir / "fact_tag_monthly.parquet").as_posix()
    total_monthly = (index_dir / "fact_total_monthly.parquet").as_posix()
    dim = (index_dir / "dim_tag.parquet").as_posix()

    # DATE_TRUNC yields TIMESTAMP in the real rollups but a plain DATE elsewhere,
    # so format rather than assuming either type.
    epoch = con.execute(f"SELECT MIN(month) FROM read_parquet('{total_monthly}')").fetchone()[0]
    epoch_day = epoch.strftime("%Y-%m-%d")
    epoch_str = epoch.strftime("%Y-%m")
    totals = [int(r[0]) for r in con.execute(f"SELECT posts FROM read_parquet('{total_monthly}') ORDER BY month").fetchall()]

    # Grouping into lists in SQL keeps Python from materialising 6.2M individual
    # rows; it sees one record per tag instead.
    rows = con.execute(f"""
        SELECT d.name, d.category, d.is_deprecated, d.post_count,
               MIN(m.month_i) AS first_m, MAX(m.month_i) AS last_m,
               LIST(m.month_i ORDER BY m.month_i) AS months,
               LIST(m.posts ORDER BY m.month_i) AS vals
        FROM (SELECT tag_id, DATEDIFF('month', DATE '{epoch_day}', month) AS month_i, posts
              FROM read_parquet('{monthly}')) m
        JOIN read_parquet('{dim}') d USING (tag_id)
        WHERE d.post_count >= {min_post_count}
        GROUP BY d.name, d.category, d.is_deprecated, d.post_count
        ORDER BY d.name
    """).fetchall()
    return rows, totals, epoch_str


def load_categories(con: duckdb.DuckDBPyConnection, index_dir: Path, epoch_day: str, n_months: int) -> list[tuple[int, list[int], list[float]]]:
    """Per-category monthly posts and n_eff, densified onto the month axis."""
    path = index_dir / "fact_category_monthly.parquet"
    if not path.exists():
        return []
    rows = con.execute(f"""
        SELECT category, DATEDIFF('month', DATE '{epoch_day}', month) AS month_i, posts, n_eff
        FROM read_parquet('{path.as_posix()}') ORDER BY category, month_i
    """).fetchall()
    dense: dict[int, tuple[list[int], list[float]]] = {}
    for category, month_i, posts, n_eff in rows:
        if not 0 <= month_i < n_months:
            continue
        slot = dense.setdefault(int(category), ([0] * n_months, [0.0] * n_months))
        slot[0][month_i] = int(posts)
        slot[1][month_i] = float(n_eff or 0.0)
    return [(cat, *dense[cat]) for cat in sorted(dense)]


def build_sections(
    rows: list[tuple],
    totals: list[int],
    categories: list[tuple[int, list[int], list[float]]],
    name_maps: dict[str, dict[str, str]],
    other_names: dict[str, list[str]],
    converters: Converters | None = None,
) -> tuple[Sections, dict]:
    names = bytearray()
    i18n = bytearray()
    values = bytearray()
    search = bytearray()
    table = bytearray()
    translated = 0
    displayable = 0

    for name, category, is_deprecated, post_count, first_m, last_m, months, vals in rows:
        name = str(name)
        name_bytes = name.encode("utf-8")
        if len(name_bytes) > MAX_NAME_BYTES:
            continue

        span = last_m - first_m + 1
        run = [0] * span
        for month_i, posts in zip(months, vals):
            run[month_i - first_m] = int(posts)
        data_off = len(values)
        for posts in run:
            encode_varint(posts, values)

        display = dict(name_maps.get(name, {}))
        aliases = other_names.get(name, [])

        # Display names come only from the name maps. Deriving them from the wiki
        # alias pool was tried and reverted: the pool is built for recall, not
        # equivalence, so it yields claims like "school_uniform is 制服スパッツ"
        # (school uniform + spats). A wrong translation is worse than none, and
        # the alias still earns its keep in search below.
        #
        # Han script conversion is a different matter -- same name, other script.
        if converters:
            if "zh_hans" in display and "zh_hant" not in display:
                display["zh_hant"] = converters.to_traditional(display["zh_hans"])
            if "zh_hant" in display and "zh_hans" not in display:
                display["zh_hans"] = converters.to_simplified(display["zh_hant"])

        if display:
            displayable += 1
        if display or aliases:
            translated += 1

        # Names block: language-tagged display names first, then the remaining
        # aliases. The list is finalised before its length is written -- writing
        # a count and then skipping an entry would desync every reader.
        lowered = name.lower()
        entries = [(LANG_CODE[lang], display[lang]) for lang in LANGS if lang in display]
        seen_alias = {lowered} | {text.lower() for _, text in entries}
        for alias in aliases:
            key = alias.lower()
            if key not in seen_alias:
                seen_alias.add(key)
                entries.append((ALIAS_CODE, alias))

        i18n_off = len(i18n)
        encode_varint(len(entries), i18n)
        for code, text in entries:
            encoded = text.encode("utf-8")
            i18n.append(code)
            encode_varint(len(encoded), i18n)
            i18n += encoded

        # Search row: everything above, lowercased, plus script variants.
        terms = [lowered]
        seen_term = {lowered}
        for _, text in entries:
            for candidate in (text, *search_variants(text, converters)):
                key = candidate.lower()
                if key and key not in seen_term:
                    seen_term.add(key)
                    terms.append(key)
        search += "\x1f".join(terms).encode("utf-8")
        search += b"\n"

        table += struct.pack(
            RECORD_FORMAT,
            len(names), len(name_bytes), int(category), 1 if is_deprecated else 0,
            int(first_m), span, data_off, int(post_count), i18n_off,
        )
        names += name_bytes

    n_tags = len(table) // TAG_RECORD_SIZE
    totals_blob = struct.pack(f"<{len(totals)}I", *totals)
    categories_blob = bytearray()
    for category, posts, n_eff in categories:
        categories_blob += struct.pack("<I", category)
        categories_blob += struct.pack(f"<{len(posts)}I", *posts)
        categories_blob += struct.pack(f"<{len(n_eff)}f", *n_eff)

    stats = {
        "tags": n_tags,
        "translated": translated,
        "displayable": displayable,
        "i18n_bytes": len(i18n),
        "search_bytes": len(search),
        "values_bytes": len(values),
    }
    return Sections(totals_blob, bytes(categories_blob), bytes(table), bytes(names), bytes(i18n), bytes(search), bytes(values)), stats


def assemble(sections: Sections, n_tags: int, n_months: int, n_categories: int, epoch_month: int, first_complete: int, last_complete: int, built: int) -> bytes:
    offsets = {}
    cursor = HEADER_SIZE
    for field in ("totals", "categories", "table", "names", "i18n", "search", "values"):
        offsets[field] = cursor
        cursor += len(getattr(sections, field))

    header = struct.pack(
        HEADER_FORMAT,
        MAGIC, VERSION, n_tags, n_months, n_categories,
        offsets["totals"], offsets["categories"], offsets["table"], offsets["names"],
        offsets["i18n"], offsets["search"], len(sections.search), offsets["values"],
        epoch_month, first_complete, last_complete, built,
    )
    return b"".join([header, *sections])


def main() -> None:
    args = parse_args()
    index_dir = Path(args.index_dir)
    output = Path(args.output) if args.output else index_dir / "index_bundle.bin"

    con = duckdb.connect()
    rows, totals, epoch_str = load_series(con, index_dir, args.min_post_count)
    wanted = {str(r[0]) for r in rows}
    epoch_year, epoch_mon = (int(part) for part in epoch_str.split("-"))
    categories = load_categories(con, index_dir, f"{epoch_str}-01", len(totals))

    name_maps: dict[str, dict[str, str]] = {}
    other_names: dict[str, list[str]] = {}
    converters: Converters | None = None
    if not args.no_i18n:
        name_maps = load_name_maps(Path(args.translations_dir), wanted)
        converters = build_converters()
        # Before the supplement and the rejections: those are review decisions and
        # must be able to override a bulk-translated name.
        load_general_names(Path(args.translations_dir), wanted, name_maps, converters)
        load_zh_supplement(Path(args.translations_dir), wanted, name_maps, converters)
        # 拒绝在补充之后:审查若给出了替代名,那条断言更强,不该再被撤掉。
        load_zh_rejections(Path(args.translations_dir), wanted, name_maps, converters)
        # 人工修正最后:它既能填也能改,而且比"这个名字是错的"更强 —— 它说得出对的是什么。
        load_zh_supplement(Path(args.translations_dir), wanted, name_maps, converters, MANUAL_FILE)
        aliases = Path(args.wiki_aliases) if args.wiki_aliases else index_dir / "wiki_other_names.json"
        other_names = load_other_names(aliases, wanted)

    sections, stats = build_sections(rows, totals, categories, name_maps, other_names, converters)
    first_complete, last_complete = complete_month_range(totals)
    built = date.fromisoformat(args.built) if args.built else datetime.now(timezone.utc).date()
    bundle = assemble(sections, stats["tags"], len(totals), len(categories), epoch_year * 12 + (epoch_mon - 1), first_complete, last_complete, built.year * 10000 + built.month * 100 + built.day)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(bundle)

    meta = {
        "version": VERSION,
        "epoch": epoch_str,
        "built": built.isoformat(),
        "months": len(totals),
        "complete_months": [first_complete, last_complete],
        "categories": [c[0] for c in categories],
        "langs": list(LANGS),
        "bytes": len(bundle),
        **stats,
    }
    (output.parent / "index_bundle.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    con.close()

    print(f"{output.name}: {stats['tags']:,} tags ({stats['displayable']:,} with display names, {stats['translated']:,} searchable in other languages)")
    print(f"  {len(totals)} months from {epoch_str} (complete {first_complete}..{last_complete}) | {len(categories)} categories | {len(bundle) / 1e6:.1f} MB")
    print(f"  i18n {stats['i18n_bytes'] / 1e6:.1f} MB | search {stats['search_bytes'] / 1e6:.1f} MB | values {stats['values_bytes'] / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
