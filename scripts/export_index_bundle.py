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

from _hanzi import is_simplified, is_traditional, repair_to_simplified, repair_to_traditional
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

# No artist map: artist tags are not indexed at all (see build_tag_index.py).
NAME_MAP_FILES = ("character_name_map.json", "copyright_name_map.json")

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

# Character names a review corrected. Separate from zh_manual.json because that
# file is a short list of decisions made by hand with the whole picture in view,
# and folding hundreds of reviewed names into it would blur what it is. Applied
# before it, so a hand decision still wins.
CHARACTER_MANUAL_FILE = "character_manual.json"

# Franchise titles a review corrected. Separate from the character file for the
# same reason that one is separate from zh_manual: the two vocabularies fail
# differently. A wrong character name is one tag; a wrong franchise title is the
# tag *and* the bracketed qualifier every character of that franchise carries.
COPYRIGHT_MANUAL_FILE = "copyright_manual.json"

# Tags whose pool-derived Chinese name a review found wrong without finding a
# replacement. Separate from the supplement because it is the opposite assertion:
# that file says "the name is X", this one says "whatever the pool gave is not it".
REJECTED_FILE = "zh_rejected.json"

# Chinese for the qualifiers Danbooru puts in brackets to tell one depiction of a
# character from another. Hand-maintained: they are franchise vocabulary, not
# ordinary words -- (third_ascension) is a Fate/Grand Order stage, (harbinger) a
# Genshin faction, (1st_costume) a VTuber's debut outfit.
VARIANT_FILE = "character_variants.json"

# 同一张表的日文版。日文那一遍以前照用中文表,于是 1,457 个日文名带上了简体括注 ——
# 「加賀（舰队Collection）」。这里只列日本人实际在用的那些写法(艦これ、第一再臨、
# 水着),列不到的仍旧留英文:kancolle 日本人读得懂,舰队 不是日文。
VARIANT_FILE_JA = "character_variants.ja.json"

BRACKETED = re.compile(r"\(([^()]+)\)")
KATAKANA = re.compile(r"[ァ-ヶ]")

# A tag name's length is stored in one byte; this is a real format constraint,
# unlike an arbitrary cap on translated text.
MAX_NAME_BYTES = 0xFF


class Converters(NamedTuple):
    """Named, not positional: swapping these two silently inverts Han conversion."""

    to_simplified: Callable[[str], str]
    to_traditional: Callable[[str], str]
    to_taiwan: Callable[[str], str] | None = None


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
    return Converters(
        opencc.OpenCC("t2s").convert,
        opencc.OpenCC("s2t").convert,
        opencc.OpenCC("s2tw").convert,
    )


# 汉字之间的 ASCII 标点。别名池里的中文名是各处抄来的,标点跟着来源走:
# 「崩坏:星穹铁道」半角冒号,「命运/冠位指定」全角,同一批数据两种写法。
HAN = "一-鿿〇"
HAN_CHARS = re.compile(f"[{HAN}]")
FULLWIDTH = {":": "：", "!": "！", "?": "？", ",": "，", ";": "；"}
# 成串一起转,否则「这样的我有罪!?」只转得动前一半。句末也转 —— 结尾的「!」
# 前面是汉字,那它就是中文叹号,`BanG Dream!少女乐团派对!` 里两个「!」性质不同。
PUNCT_RUN = re.compile(f"(?<=[{HAN}])([:!?,;]+)(?=[{HAN}]|$)")
# 括号里含汉字且不含拉丁字母才转:「(系列)」「(崩坏：星穹铁道)」要转,
# 而残留的「(anime)」不该被悄悄转成全角 —— 那是没翻译,不是排版问题。
PAREN = re.compile(f"(?<=[{HAN}])\(([^()]*[{HAN}][^()]*)\)")
# 中文正文里不留空格再接括注 —— 那个空格是从英文排版抄来的,也正是这条规则的抓手:
# 「初音未来 (cosplay)」和「初音未来（cosplay）」是同一族标签的两种写法,254 个
# cosplay 标签里 115 个是前者。有了空格就不必要求括注内容是汉字,所以 (cosplay)、
# (meme)、(ff14) 这些没翻译的限定词也一起规整,而不带空格的「罗托(DQ3)」不受影响。
SPACED_PAREN = re.compile(f"(?<=[{HAN}]) \(([^()]+)\)$")
# 片假名中点。中文用的是 U+00B7「·」,日文是 U+30FB「・」,渲染出来几乎一样,
# 所以「干将・莫邪」这种混进来不会有人发现。
KATAKANA_MIDDLE_DOT = "・"


COSPLAY_SUFFIX = "_(cosplay)"


HANGUL = re.compile(r"[가-힣ᄀ-ᇿ]")
KANA = re.compile(r"[぀-ヿ]")


WRONG_OPENERS = "「{『【"


def repair_brackets(name_maps: dict[str, dict[str, str | None]]) -> int:
    """别名池里括号配不上对的名字。

    两种坏法,都来自抄写而不是翻译:开括号被写成了别的形状(「足柄「一番くじ)」、
    「ジャービス{三越)」),或者整对里丢了一个(「エイプリル(DTB」、「(盗贼山惠」)。

    只在数目不平时动手,而且只补形状,不补内容 —— 括号里少了字的那种(「风花雪月)」
    丢的是「暗黑骑士」)这里补不出来,留给人工层。
    """
    fixed = 0
    for names in name_maps.values():
        for lang, value in list(names.items()):
            if not value or value.count("(") == value.count(")"):
                continue
            if value.count(")") > value.count("("):
                wrong = next((c for c in WRONG_OPENERS if c in value), None)
                out = value.replace(wrong, "(", 1) if wrong else value.rstrip(")")
            else:
                out = value.lstrip("(") if value.startswith("(") else value + ")"
            if out != value:
                names[lang] = out
                fixed += 1
    return fixed


def normalize_slugs(name_maps: dict[str, dict[str, str | None]]) -> int:
    """别名池里混进来的下划线,是标签串而不是名字。

    Danbooru 的 other_names 有一部分是照着标签名写的,`함대_컬렉션`、
    `バンドリ!_ガールズバンドパーティ!`。显示名里那个下划线该是空格。

    只动含非 ASCII 的值。纯 ASCII 的下划线未必是标签串:`o_o` 的显示名就是「O_O」,
    那是个颜文字,下划线正是它的脸;`imas_cg` 是有人选的缩写。实测 603 个带下划线的
    值里,462 个含非 ASCII,全都该换空格,而剩下的都是这两类。
    """
    fixed = 0
    for names in name_maps.values():
        for lang, value in list(names.items()):
            if not value or "_" not in value or value.isascii():
                continue
            names[lang] = value.replace("_", " ")
            fixed += 1
    return fixed


def drop_wrong_script(name_maps: dict[str, dict[str, str | None]]) -> int:
    """写错文字系统的名字整条丢掉。

    韩文用谚文书写。ko 槽里一个只有汉字或假名、一条谚文都没有的值,按构造就不是韩文
    ——「麻将灵魂」「カガミチヒロ」「怪獣8号」都是这么来的,来源是 *_official.json 那一步
    LLM 挑名字时挑错了格子。日文那边反过来:日文不用谚文,`shift_up` 的 ja 是「시프트업」。

    丢掉而不是留着:韩文界面上显示一串中文,比什么都不显示更糟,而这里没有能力给出对的
    韩文。丢掉之后那个格子空着,别的层还能填。

    汉字混谚文的不算:韩文标题确实会带汉字(《쓸쓸하고 찬란하神 - 도깨비》官方就这么写)。
    """
    dropped = 0
    for names in name_maps.values():
        korean = names.get("ko")
        if korean and not HANGUL.search(korean) and (HAN_CHARS.search(korean) or KANA.search(korean)):
            del names["ko"]
            dropped += 1
        japanese = names.get("ja")
        if japanese and HANGUL.search(japanese):
            del names["ja"]
            dropped += 1
    return dropped


def inherit_cosplay_names(name_maps: dict[str, dict[str, str | None]]) -> int:
    """`X_(cosplay)` 的名字跟着 X 走。

    这类标签的意思是「有人 cos 成 X」,所以名字只能是 X 的名字 —— 它不是一个独立的
    词条,没有自己的译法可言。但它在 Danbooru 里属于 general 分类,于是走的是另一条
    数据链:角色名被审查改对了,cosplay 那份副本留在原地。实测 254 个 cosplay 标签
    里有 135 个和本体对不上 —— 镜音铃写成镜音凛、碧姬公主写成桃子公主、兔田佩克拉
    写成乌萨达·佩科拉,都是本体早就修过的名字。

    放在消歧之后:本体名要先拿到自己的括号后缀,继承的才是最终形态。
    """
    inherited = 0
    for tag, names in name_maps.items():
        if not tag.endswith(COSPLAY_SUFFIX):
            continue
        base = name_maps.get(tag[: -len(COSPLAY_SUFFIX)])
        if not base:
            continue
        for lang in ("zh_hans", "zh_hant"):
            want = f"{base[lang]}（cosplay）" if base.get(lang) else None
            if want and names.get(lang) != want:
                names[lang] = want
                inherited += 1
    return inherited


def normalize_simplified(name_maps: dict[str, dict[str, str | None]]) -> int:
    """把过不了简体闸门的名字交给 _hanzi 的修复函数。

    只动闸门已经判定不合格的值,而且修完仍不合格就原样退回 —— 这两道限制之下,这个
    变换在闸门的定义里是只增不减的。风险全部压在那个定义上,也就是 PROTECTED:
    OpenCC 的转换不只做字形映射,还做语义和短语替换,而它替换掉的有些字本来就是对的
    (暴露->曝露、樫->㭴、魟->𫚉)。那张表是按本项目已发布的名字实测出来的,语料变了
    要重新量 —— 量的办法见 _hanzi 的模块注释。

    简繁一起修。只修简体的话,繁体那边会保持旧字形,而 normalize_traditional 不会来
    收拾:它看到繁体串里有繁体字就认定是人工写的,于是「ICG姐贵」配「ICG姉貴」一直
    并存下去。
    """
    fixed = 0
    for names in name_maps.values():
        for lang, guard, repair in (("zh_hans", is_simplified, repair_to_simplified), ("zh_hant", is_traditional, repair_to_traditional)):
            value = names.get(lang)
            if not value or guard(value):
                continue
            out = repair(value)
            if out != value:
                names[lang] = out
                fixed += 1
    return fixed


def normalize_punctuation(name_maps: dict[str, dict[str, str | None]]) -> int:
    """半角标点转全角,只在两侧都是汉字时。

    条件苛刻是有原因的:「Re:从零开始的异世界生活」的冒号属于拉丁词 Re,
    「火焰纹章:if」的冒号后面跟着 if,两者都该保持半角。要求前后皆汉字,
    这两种就自己排除掉了,剩下的才是中文正文里的标点。

    只管中日韩三种字形里的中文两种。日文名同样命中汉字类,但日文排版另有
    规矩(冒号常用半角),不该按中文规范去改。
    """
    fixed = 0
    for names in name_maps.values():
        for lang in ("zh_hans", "zh_hant"):
            value = names.get(lang)
            if not value:
                continue
            out = PUNCT_RUN.sub(lambda m: "".join(FULLWIDTH[c] for c in m.group(1)), value)
            out = PAREN.sub(lambda m: "（" + m.group(1) + "）", out)
            out = SPACED_PAREN.sub(lambda m: "（" + m.group(1) + "）", out)
            out = out.replace(KATAKANA_MIDDLE_DOT, "·")
            if out != value:
                names[lang] = out
                fixed += 1
        # 韩文的间隔号是 ·(U+00B7),日文的是 ・(U+30FB)。两者渲染出来几乎一样,所以
        # 「안젤리아・카를로스」这种混进来没人看得出。日文那边不能碰 —— 它有 17,560 个
        # 名字正当地用着 ・。
        korean = names.get("ko")
        if korean and KATAKANA_MIDDLE_DOT in korean:
            names["ko"] = korean.replace(KATAKANA_MIDDLE_DOT, "·")
            fixed += 1
    return fixed


def normalize_traditional(name_maps: dict[str, dict[str, str | None]], converters: Converters | None) -> int:
    """Re-derive machine-converted traditional names in Taiwan's standard glyphs.

    `s2t` maps to a generic traditional form that is not what Taiwan writes:
    衆 for 眾, 牀 for 床, 羣 for 群, 啓 for 啟, 脣 for 唇. `s2tw` is the same
    conversion against Taiwan's standard character list, so 1,884 names come out
    right that were subtly wrong before.

    Only values that `s2t` itself produced are touched. A traditional name that
    differs from the conversion is real data -- Nintendo calls `fire_emblem_fates`
    聖火降魔錄 in Taiwan while the mainland says 火焰纹章 -- and re-deriving it from
    the simplified name would destroy exactly the regional titles this pipeline
    exists to keep.

    `s2twp` was measured and rejected: its extra vocabulary layer fixes 高分辨率
    into 高解析度 but also reads ordinary words as IT jargon, turning a comic's
    對話框 into a UI 對話方塊 and 溢出 into 溢位. Taiwanese vocabulary belongs in a
    reviewed table, not in a converter that cannot tell the two apart.
    """
    if converters is None or converters.to_taiwan is None:
        return 0
    fixed = simplified = 0
    for slots in name_maps.values():
        hans, hant = slots.get("zh_hans"), slots.get("zh_hant")
        if not hans or not hant:
            continue
        # Two cases. Either the traditional value is what s2t produced from the
        # simplified one, in which case re-derive it properly; or it is not
        # traditional at all -- the wiki bucket handed the traditional field a
        # simplified value, and 168 shipped names were plain simplified Chinese
        # under a zh_hant label (鸣潮, 铃, 小红帽). Both re-derive from zh_hans.
        derived = converters.to_traditional(hans) == hant
        if not derived and is_traditional(hant):
            continue
        taiwan = converters.to_taiwan(hans)
        if taiwan != hant:
            slots["zh_hant"] = taiwan
            fixed += 1
            if not derived:
                simplified += 1
    print(f"  traditional normalised to Taiwan glyphs: {fixed:,} ({simplified} were simplified values)")
    return fixed


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


def load_name_maps(translations_dir: Path, wanted: set[str] | None) -> dict[str, dict[str, str | None]]:
    """Per-language display names, keyed by tag, restricted to tags we ship.

    Reports per-file and per-language coverage rather than failing on a missing
    file: any single map being absent or thin is a silent quality loss in the
    shipped bundle, which is otherwise only visible by searching for a tag that
    should have a translated name and finding nothing.
    """
    merged: dict[str, dict[str, str | None]] = {}
    for filename in NAME_MAP_FILES:
        path = translations_dir / filename
        if not path.exists():
            print(f"  WARNING: {filename} missing -- those tags ship without display names")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        used = 0
        for tag, names in data.items():
            if wanted is not None and tag not in wanted:
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
    wanted: set[str] | None,
    name_maps: dict[str, dict[str, str | None]],
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
        if (wanted is not None and tag not in wanted) or not zh:
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
    wanted: set[str] | None,
    name_maps: dict[str, dict[str, str | None]],
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
    for tag, entry in data.items():
        if (wanted is not None and tag not in wanted) or not entry:
            continue
        slot = name_maps.setdefault(tag, {})

        # A dict addresses the scripts separately, a bare string asserts the
        # simplified name and lets the traditional one follow. Both are needed:
        # `fire_emblem_fates` is 火焰纹章 on the mainland and 聖火降魔錄 in Taiwan --
        # Nintendo's own title there -- so correcting the simplified name must be
        # able to leave the traditional one alone. Only the listed scripts move.
        if isinstance(entry, dict):
            for script in ("zh_hans", "zh_hant"):
                value = entry.get(script)
                if not value or slot.get(script) == value:
                    continue
                if slot.get(script):
                    corrected += 1
                else:
                    filled += 1
                slot[script] = str(value)
            continue

        zh = str(entry)
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
    wanted: set[str] | None,
    name_maps: dict[str, dict[str, str | None]],
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

    The slots are set to None rather than removed, and set even when this pipeline
    had no name to drop. Absence and rejection mean the same thing *here* -- both
    end in the English fallback -- but not downstream: a consumer that merges its
    own lower-priority sources (pictoria keeps a frozen GPT-era table) reads an
    absent key as "no opinion, fill it yourself" and would put the rejected name
    straight back. None says the review looked and there is no name. All 16 tags
    in the file are in pictoria's table today, carrying exactly what was rejected.
    """
    path = translations_dir / REJECTED_FILE
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    tags = data if isinstance(data, list) else list(data)
    dropped = marked = 0
    for tag in tags:
        if wanted is not None and tag not in wanted:
            continue
        slot = name_maps.setdefault(tag, {})
        if slot.get("zh_hans"):
            dropped += 1
        else:
            marked += 1
        slot["zh_hans"] = None
        slot["zh_hant"] = None
    print(f"  {REJECTED_FILE}: {dropped:,} Chinese names dropped as wrong, {marked:,} marked for downstream")
    return dropped


def disambiguate_variants(
    name_maps: dict[str, dict[str, str | None]],
    translations_dir: Path,
    character_tags: set[str],
    copyright_names: dict[str, str],
    converters: Converters | None = None,
    lang: str = "zh_hans",
    also: tuple[str, ...] = ("zh_hant",),
    variant_file: str = VARIANT_FILE,
) -> int:
    """Put the bracketed qualifier back on names that need it to stay distinct.

    Danbooru distinguishes depictions of one character with a bracketed suffix,
    and the name maps drop it: `akemi_homura` and `akemi_homura_(magical_girl)`
    both come out 晓美焰, `fujimaru_ritsuka_(male)` and `..._(female)` both 藤丸立香.
    1,176 Chinese names covered more than one character tag, over 3,247 tags. In a
    tag list they are the same entry twice.

    Dropping the suffix is right when it only disambiguates the English slug --
    甘雨 needs no "(genshin impact)" because no other character here is 甘雨. It is
    wrong the moment a second tag lands on the same name, and that is exactly what
    this detects: group by Chinese name, and only touch groups with a collision.

    Within a group the tag carrying the fewest brackets keeps the bare name -- it
    is the base depiction the others are variants of. Ties (male/female) all take
    a suffix. Copyright suffixes are translated through the copyright names this
    project already has, so `shigure_(kancolle)` becomes 时雨（舰队Collection）;
    everything else goes through the hand-maintained variant table. A suffix in
    neither is left in English rather than guessed at: an unreadable qualifier
    still separates two tags, a wrong one misinforms.
    """
    path = translations_dir / variant_file
    variants = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    to_taiwan = converters.to_taiwan if converters else None

    groups: dict[str, list[str]] = {}
    for tag in character_tags:
        name = name_maps.get(tag, {}).get(lang)
        if name:
            groups.setdefault(name, []).append(tag)

    renamed = untranslated = 0
    unknown: set[str] = set()
    for tags in groups.values():
        if len(tags) < 2:
            continue
        shared = set.intersection(*[set(BRACKETED.findall(tag)) for tag in tags])
        stems = {tag: BRACKETED.sub("", tag).replace("__", "_").strip("_") for tag in tags}

        def marks(tag: str) -> list[str]:
            """The qualifiers that set this tag apart from the rest of its group.

            Some variants are marked by a prefix rather than a bracket:
            `female_admiral_(kancolle)` against `admiral_(kancolle)`. Decided per
            tag against the stems it actually extends, not for the group as a
            whole -- 「博士」 covers four tags from three franchises, and asking
            whether *every* stem shares a base found `percy` and gave up, leaving
            doctor and male_doctor both 博士（明日方舟）. Tags no other stem is a
            suffix of, like `hatsune_miku` beside `magical_mirai_miku`, get no
            prefix and stay as they are.
            """
            stem = stems[tag]
            extended = [s for s in stems.values() if s != stem and stem.endswith(s)]
            prefix = stem[: -len(min(extended, key=len))].rstrip("_") if extended else ""
            return ([prefix] if prefix else []) + [q for q in BRACKETED.findall(tag) if q not in shared]

        # The plainest tag keeps the bare name: it is the base depiction the
        # others are variants of. Measured with the same function that produces
        # the labels -- counting brackets separately once let `female_admiral`
        # pass as the plainest of its group and stay 提督 like the other five.
        plainest = min(len(marks(t)) for t in tags)
        sole = sum(1 for t in tags if len(marks(t)) == plainest) == 1

        for tag in tags:
            qualifiers = marks(tag)
            if not qualifiers or (len(qualifiers) == plainest and sole):
                continue

            labels = []
            for qualifier in qualifiers:
                label = copyright_names.get(qualifier) or variants.get(qualifier)
                if label is None:
                    label = qualifier.replace("_", " ")
                    unknown.add(qualifier)
                    untranslated += 1
                labels.append(label)
            suffix = f"（{'·'.join(labels)}）"
            slot = name_maps[tag]
            slot[lang] = f"{slot[lang]}{suffix}"
            # The other scripts of the same language follow -- in *their* glyphs.
            # The labels come from the simplified copyright names and variant
            # table, so appending them raw produced 開拓者（崩坏：星穹铁道）, half
            # the name in each script. normalize_traditional does not catch that:
            # it judges the whole string, and a string containing 開 reads as one
            # someone wrote by hand, so it is left exactly as it is.
            for other in also:
                if slot.get(other):
                    converted = to_taiwan(suffix) if other == "zh_hant" and to_taiwan else suffix
                    slot[other] = f"{slot[other]}{converted}"
            renamed += 1

    print(f"  {variant_file}: {renamed:,} colliding {lang} character names given their qualifier")
    if unknown:
        top = ", ".join(sorted(unknown)[:8])
        print(f"    {len(unknown)} qualifiers had no translation and kept the English ({untranslated} uses): {top}")
    return renamed


def resolve_display_names(
    translations_dir: Path,
    wanted: set[str] | None,
    converters: Converters | None,
) -> dict[str, dict[str, str | None]]:
    """The six translation layers collapsed into one name per tag per language.

    The order below *is* the policy, and it lives here rather than in each
    consumer because a second copy is a second thing to drift. `build_tag_i18n.py`
    in the pictoria repository used to merge the raw name maps itself; it read a
    path this project stopped writing to, said "missing, skipped", and shipped
    unreviewed names for months without anyone noticing.

    A None value means a review rejected the name and found no replacement --
    distinct from an absent key, which means no layer had an opinion. Consumers
    with their own fallback sources need that difference; see load_zh_rejections.
    """
    name_maps = load_name_maps(translations_dir, wanted)
    load_general_names(translations_dir, wanted, name_maps, converters)
    load_zh_supplement(translations_dir, wanted, name_maps, converters)
    # 拒绝在补充之后:审查若给出了替代名,那条断言更强,不该再被撤掉。
    load_zh_rejections(translations_dir, wanted, name_maps, converters)
    # 人工修正最后:它既能填也能改,而且比"这个名字是错的"更强 —— 它说得出对的是什么。
    # 角色名审查在人工层之前:它推翻的是别名池的启发式选择,而人工层推翻的是它。
    # 作品名在角色名之前:消歧会把作品名接到角色名后面,那一步读的是这里的结果。
    load_zh_supplement(translations_dir, wanted, name_maps, converters, COPYRIGHT_MANUAL_FILE)
    load_zh_supplement(translations_dir, wanted, name_maps, converters, CHARACTER_MANUAL_FILE)
    load_zh_supplement(translations_dir, wanted, name_maps, converters, MANUAL_FILE)
    # 消歧在规范化之前:它会往名字后面接括号,那部分也要跟着转成台湾字形。
    # 两个 map 的键本身就是分类 —— 谁是角色、谁是作品,读文件即知,不必开数据库。
    copyrights = json.loads((translations_dir / "copyright_name_map.json").read_text(encoding="utf-8"))
    ordinary = json.loads((translations_dir / GENERAL_FILE).read_text(encoding="utf-8"))
    # Characters by subtraction, not from character_name_map: a tag with no wiki
    # aliases is absent from that map and got its name from the supplement
    # instead -- `akemi_homura_(magical_girl)` among them, which is exactly the
    # kind of variant this pass exists for. What is left after removing the
    # copyright and general/meta vocabularies is the character namespace.
    characters = set(name_maps) - set(copyrights) - set(ordinary)
    disambiguate_variants(
        name_maps,
        translations_dir,
        characters,
        # 标签取自 name_maps 而非 copyrights:那个文件是原始名字映射,
        # copyright_manual 的修正没有回写进去。从这里读,作品名一改,
        # 该作品每个角色的括号后缀跟着改;从文件读,后缀会停在旧名字上。
        {tag: name_maps[tag]["zh_hans"] for tag in copyrights if name_maps.get(tag, {}).get("zh_hans")},
        converters,
    )
    # Japanese collides far harder than Chinese -- 3,803 names against 145 --
    # because build_name_map picks it with `shortest` and nothing reviews it, so
    # 加賀 covers the Kantai, Azur Lane and Warship Girls characters at once. The
    # variant table is Chinese, so a qualifier with no Japanese source stays in
    # English: unreadable still separates two tags, wrong misinforms.
    disambiguate_variants(
        name_maps,
        translations_dir,
        characters,
        {tag: name_maps[tag]["ja"] for tag in copyrights if name_maps.get(tag, {}).get("ja")},
        lang="ja",
        also=(),
        variant_file=VARIANT_FILE_JA,
    )
    # 标点在繁体规范化之前:两种字形都要改,否则简体一改,繁体就不再
    # 等于简体的机器转换结果,normalize_traditional 会判定它是人工写的而放过。
    print(f"  mismatched brackets repaired: {repair_brackets(name_maps)}")
    print(f"  slug underscores turned into spaces: {normalize_slugs(name_maps):,}")
    print(f"  names dropped for being in the wrong script: {drop_wrong_script(name_maps)}")
    print(f"  cosplay names inherited from their base character: {inherit_cosplay_names(name_maps):,}")
    # 字形在标点之前,两者都在繁体规范化之前:后面那一步要看到最终的简体。
    print(f"  glyphs repaired past the script guard: {normalize_simplified(name_maps):,}")
    normalize_punctuation(name_maps)
    # 最后:前面每一层都可能新填简体并派生繁体,规范化必须看到最终结果。
    normalize_traditional(name_maps, converters)
    return name_maps


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
    name_maps: dict[str, dict[str, str | None]],
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
        # Keyed on the value, not the key: load_zh_rejections leaves None behind,
        # and `"zh_hans" in display` would hand that None to the converter.
        if converters:
            if display.get("zh_hans") and not display.get("zh_hant"):
                display["zh_hant"] = converters.to_traditional(display["zh_hans"])
            if display.get("zh_hant") and not display.get("zh_hans"):
                display["zh_hans"] = converters.to_simplified(display["zh_hant"])

        if any(display.values()):
            displayable += 1
        if any(display.values()) or aliases:
            translated += 1

        # Names block: language-tagged display names first, then the remaining
        # aliases. The list is finalised before its length is written -- writing
        # a count and then skipping an entry would desync every reader.
        lowered = name.lower()
        entries = [(LANG_CODE[lang], display[lang]) for lang in LANGS if display.get(lang)]
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

    name_maps: dict[str, dict[str, str | None]] = {}
    other_names: dict[str, list[str]] = {}
    converters: Converters | None = None
    if not args.no_i18n:
        converters = build_converters()
        name_maps = resolve_display_names(Path(args.translations_dir), wanted, converters)
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
