import argparse
import json
import re
from pathlib import Path
from typing import Any

import opencc

from _hanzi import repair_to_simplified, repair_to_traditional
from _paths import TRANSLATIONS_DIR

LANGS = ("en", "ja", "ko", "zh_hans", "zh_hant")

# 汉字是中日共享的书写系统:日文汉字名可作中文名、中文汉字名可作日文名,简繁之间可互转。
# 用 OpenCC 把任一汉字名规范化后填充缺失的中日字段(日文汉字标题直接可作中文名)。
_jp2t = opencc.OpenCC("jp2t")  # 日文新字体 → 繁体
_t2s = opencc.OpenCC("t2s")  # 繁体 → 简体
_s2t = opencc.OpenCC("s2t")  # 简体 → 繁体
_t2jp = opencc.OpenCC("t2jp")  # 繁体 → 日文新字体

_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")


def is_han_name(s: str | None) -> bool:
    # 含汉字且无假名无谚文 → 可作中日汉字名基准(假名/谚文混合的名字无法跨语言转写)
    return bool(s) and bool(_HAN.search(s)) and not _KANA.search(s) and not _HANGUL.search(s)


def fill_cjk(chosen: dict[str, str]) -> dict[str, str]:
    ja, hans, hant = chosen.get("ja"), chosen.get("zh_hans"), chosen.get("zh_hant")
    if is_han_name(hant):
        base = hant
    elif is_han_name(hans):
        base = _s2t.convert(hans)
    elif is_han_name(ja):
        base = _jp2t.convert(ja)
    else:
        return chosen  # 无汉字基准可复用
    if not hant:
        chosen["zh_hant"] = base
    if not hans:
        chosen["zh_hans"] = _t2s.convert(base)
    if not ja:
        chosen["ja"] = _t2jp.convert(base)
    return chosen


def normalize_chinese(chosen: dict[str, str]) -> dict[str, str]:
    """把中文字段归一到该语言的规范字形。

    供给的名字不能照单全收。fill_cjk 只在**派生**缺失字段时转换,直接给定的值它信任 ——
    但 wiki 的 zh 桶和 LLM 的 official 经常给出掺着日文新字体或繁体的「简体名」:
    「未来日記」「戦国BASARA」「封神演義」「銀牙伝説WEED」都是这么发出去的,占已发布
    中文名的 2.3%,且与 opencc 版本无关。

    交给 `_hanzi.repair_to_simplified`,它只在这个值**当前不合格**时才动手。这道闸门
    是关键:对已经是中文的串跑 jp2t 会把共用字当成日文倒转回去(「醋酸汁」变回
    「酢酸汁」、「默天芸」变成「默天艺」),而闸门让那些串根本不进入转换。

    因此这里现在也能修掉纯日文新字体 —— 曾经说需要一张人工字表才能做的事。实测修好
    3,416 个已发布名字,最热的是「月姫」→「月姬」(15,740 投稿)和「西行寺幽々子」→
    「西行寺幽幽子」(27,787 投稿)。自动筛查(被转掉的字若出现在其他合格名字里就可疑)
    只剩 7 种可疑替换、共 10 个名字,且都是正确的日译中词汇(交差点→交叉点)。

    繁体字段也修,但目标不同:只清掉日文字形,不碰简繁。以前完全不修它,理由是「它已经
    是繁体」—— 而实际上它常常是别名池里的日文原值,于是简体修好了、繁体还挂着「月姫」
    「桜內梨子」「戦鬥潮流」。
    """
    hans = chosen.get("zh_hans")
    if hans:
        chosen["zh_hans"] = repair_to_simplified(hans)
    hant = chosen.get("zh_hant")
    if hant:
        chosen["zh_hant"] = repair_to_traditional(hant)
    return chosen


def beautify_tag(tag: str) -> str:
    # Danbooru 的 tag 名是规范罗马名(snake_case):下划线转空格、按词首字母大写
    words = tag.replace("_", " ").split(" ")
    return " ".join(w[:1].upper() + w[1:] if w else w for w in words)


def _candidates(names: list[str]) -> list[str]:
    # 下划线转空格便于阅读;保持 other_names 原序,首项在 wiki 里就是主名
    return [n.replace("_", " ").strip() for n in names if n and n.strip()]


def shortest(names: list[str]) -> str | None:
    # 同语言多别名时取最短的作为代表(主笔名/规范名通常最简洁)
    candidates = _candidates(names)
    return min(candidates, key=len) if candidates else None


def primary(names: list[str]) -> str | None:
    # 作品名取首项而非最短:wiki 的 other_names 是人工维护的有序表,首项是主名,后面
    # 多为 Pixiv 抓来的缩写与同人俚语。取最短会系统性地选中它们:デスノート→DN腐、
    # ワンピース→ワンピ、チェンソーマン→チェ夢、ブルーアーカイブ→青アカ。
    candidates = _candidates(names)
    return candidates[0] if candidates else None


def pick_copyright(tag: str, buckets: dict[str, list[str]]) -> dict[str, str]:
    # copyright 的规则兜底。en 不取自 en 桶:那里多是 Pixiv 缩写(ONEPIECE/csm/BA/gnsn),
    # 交给 build 的 en_fallback 用 tag 罗马名兜底更稳。
    out: dict[str, str] = {}
    for lang in ("ja", "ko", "zh_hans", "zh_hant"):
        rep = primary(buckets.get(lang, []))
        if rep:
            out[lang] = rep
    return out


_DISAMBIG = re.compile(r"_\([^()]*\)$")


def strip_disambig(tag: str) -> str:
    # 角色 tag 常带消歧后缀 name_(source),如 artoria_pendragon_(fate);en 名取消歧后的本名
    return _DISAMBIG.sub("", tag)


def character_en(tag: str) -> str:
    return beautify_tag(strip_disambig(tag))


def pick_character(tag: str, buckets: dict[str, list[str]]) -> dict[str, str]:
    # 非歧义角色(每语言桶仅 1 候选)的兜底:en 用消歧后的 tag 罗马名,其余取最短
    out: dict[str, str] = {"en": character_en(tag)}
    for lang in ("ja", "ko", "zh_hans", "zh_hant"):
        rep = shortest(buckets.get(lang, []))
        if rep:
            out[lang] = rep
    return out


def build(
    source: Path,
    picker,
    official: dict[str, dict[str, str]] | None,
    en_override=None,
    en_fallback=None,
) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, list[str]]] = json.loads(source.read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = {}
    for tag, buckets in data.items():
        if official and tag in official:
            chosen = {lang: official[tag][lang] for lang in LANGS if official[tag].get(lang)}
        else:
            chosen = picker(tag, buckets)
        if en_override is not None:
            chosen["en"] = en_override(tag)  # 角色 en 一律用 tag 罗马名(比 LLM 猜更可靠)
        elif en_fallback is not None and not chosen.get("en"):
            # 作品的官方英文名比罗马名好("Steins;Gate" 优于 "Steins;gate"),但 LLM 常
            # 只给 en+ja 或漏掉 en。缺了就兜底,否则整个 tag 连英文显示名都没有。
            chosen["en"] = en_fallback(tag)
        result[tag] = normalize_chinese(fill_cjk(chosen))
    return result


def report(label: str, mapping: dict[str, dict[str, str]]) -> None:
    # 逐语言计数:显示名的缺口只在这里看得见,单看 tag 总数是看不出「中文只覆盖 16%」的
    counts = ", ".join(f"{lang}={sum(1 for v in mapping.values() if v.get(lang))}" for lang in LANGS)
    print(f"{label}: {len(mapping)} tags ({counts})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-tag canonical name maps (one name per language).")
    parser.add_argument("--dir", type=str, default=str(TRANSLATIONS_DIR))
    args = parser.parse_args()
    base = Path(args.dir)

    official_path = base / "copyright_official.json"
    official: dict[str, Any] | None = None
    if official_path.exists():
        official = json.loads(official_path.read_text(encoding="utf-8"))
        print(f"using LLM official names for {len(official)} copyright tags")
    copyright_map = build(base / "copyright_names.json", pick_copyright, official, en_fallback=beautify_tag)
    (base / "copyright_name_map.json").write_text(json.dumps(copyright_map, ensure_ascii=False), encoding="utf-8")
    report("copyright_name_map", copyright_map)

    char_official_path = base / "character_official.json"
    char_official: dict[str, Any] | None = None
    if char_official_path.exists():
        char_official = json.loads(char_official_path.read_text(encoding="utf-8"))
        print(f"using LLM official names for {len(char_official)} character tags")
    character_map = build(base / "character_names.json", pick_character, char_official, en_override=character_en)
    (base / "character_name_map.json").write_text(json.dumps(character_map, ensure_ascii=False), encoding="utf-8")
    report("character_name_map", character_map)


if __name__ == "__main__":
    main()
