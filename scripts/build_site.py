"""Assemble the deployable site, including one page per UI language.

    uv run python scripts/build_site.py --out site

The page switches language in the browser, but a crawler never runs that code:
whatever is in the served markup is the whole of what a link preview and a
search result can say. So each language gets its own URL with its own title,
description and card, and the set is tied together with hreflang.

Only the head differs. The body, the script and the bundle are byte-identical
across variants -- the sub-pages reach back up for the shared assets rather than
carrying copies, so the 18 MB bundle is still downloaded and cached once no
matter which language a visitor arrives in.

Standard library only: this runs in CI, where the point of stage 2 is that it
needs nothing the repository does not already carry.
"""

import argparse
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://jannchie.github.io/danbooru-tag-index/"

# slug is the sub-path; None is the root page, which stays the x-default and
# keeps detecting the visitor's language from the browser.
LANGS = [
    {
        "lang": "en", "slug": None, "locale": "en_US", "hreflang": "en", "html_lang": "en-us",
        "title": "Danbooru Tag Index — twenty years of anime tag popularity",
        "og_title": "Danbooru Tag Index",
        "description": "Monthly popularity for 52,000 Danbooru tags since 2005: raw posts, share of the site, and a fragmentation-adjusted index. Searchable in Chinese, Japanese, English and Korean.",
        "og_description": "Twenty years of anime tag popularity, month by month. 52,000 tags, three normalisations, five languages.",
    },
    {
        "lang": "zh_hans", "slug": "zh-hans", "locale": "zh_CN", "hreflang": "zh-Hans", "html_lang": "zh-cn",
        "title": "Danbooru 标签指数 — 二十年二次元标签流行度",
        "og_title": "Danbooru 标签指数",
        "description": "2005 年至今、5.2 万个 Danbooru 标签的逐月流行度：投稿量、站内份额，以及消除分类碎片化影响的相对指数。支持中日英韩四语搜索。",
        "og_description": "二十年二次元标签流行度，逐月记录。5.2 万个标签，三种归一化口径，五种界面语言。",
    },
    {
        "lang": "zh_hant", "slug": "zh-hant", "locale": "zh_TW", "hreflang": "zh-Hant", "html_lang": "zh-tw",
        "title": "Danbooru 標籤指數 — 二十年二次元標籤流行度",
        "og_title": "Danbooru 標籤指數",
        "description": "2005 年至今、5.2 萬個 Danbooru 標籤的逐月流行度：投稿量、站內份額，以及消除分類碎片化影響的相對指數。支援中日英韓四語搜尋。",
        "og_description": "二十年二次元標籤流行度，逐月記錄。5.2 萬個標籤，三種歸一化口徑，五種介面語言。",
    },
    {
        "lang": "ja", "slug": "ja", "locale": "ja_JP", "hreflang": "ja", "html_lang": "ja-jp",
        "title": "Danbooru タグ指数 — 20年分のタグ人気度",
        "og_title": "Danbooru タグ指数",
        "description": "2005年以降、5.2万件のDanbooruタグの月別人気度。投稿数、サイト全体に占める割合、そしてカテゴリの細分化を補正した相対指数。日中英韓の4言語で検索できます。",
        "og_description": "20年分のタグ人気度を、月ごとに。5.2万タグ、3つの正規化、5言語対応。",
    },
    {
        "lang": "ko", "slug": "ko", "locale": "ko_KR", "hreflang": "ko", "html_lang": "ko-kr",
        "title": "Danbooru 태그 지수 — 20년간의 태그 인기도",
        "og_title": "Danbooru 태그 지수",
        "description": "2005년 이후 52,000개 Danbooru 태그의 월별 인기도: 게시물 수, 사이트 점유율, 그리고 분류 파편화를 보정한 상대 지수. 중국어·일본어·영어·한국어로 검색할 수 있습니다.",
        "og_description": "20년간의 태그 인기도, 월 단위로. 52,000개 태그, 3가지 정규화, 5개 언어.",
    },
]

STATIC = ["og.png", "robots.txt"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble the deployable site.")
    parser.add_argument("--web", type=str, default="web")
    parser.add_argument("--index-dir", type=str, default="data/index")
    parser.add_argument("--out", type=str, default="site")
    return parser.parse_args()


def replace_once(html: str, pattern: str, replacement: str, what: str) -> str:
    """Substitute exactly one match, or fail loudly.

    A silent no-match here ships a page whose card and title belong to another
    language, which nothing downstream would catch -- the build stays green and
    the preview is simply wrong.
    """
    out, n = re.subn(pattern, lambda _: replacement, html, count=1)
    if n != 1:
        raise SystemExit(f"build_site: expected exactly one {what} in web/index.html, found {n}")
    return out


def alternates() -> str:
    links = [
        f'<link rel="alternate" hreflang="{entry["hreflang"]}" href="{BASE}{entry["slug"] + "/" if entry["slug"] else ""}">'
        for entry in LANGS
    ]
    links.append(f'<link rel="alternate" hreflang="x-default" href="{BASE}">')
    return "\n".join(links)


def variant(html: str, entry: dict) -> str:
    slug = entry["slug"]
    url = f"{BASE}{slug}/" if slug else BASE
    card = f"{BASE}og-{slug}.png" if slug else f"{BASE}og.png"
    up = "../" if slug else ""

    html = replace_once(html, r"<title>.*?</title>", f"<title>{entry['title']}</title>", "<title>")
    html = replace_once(
        html,
        r'<meta name="description" content="[^"]*">',
        f'<meta name="description" content="{entry["description"]}">',
        "description meta",
    )
    html = replace_once(
        html, r'<link rel="canonical" href="[^"]*">', f'<link rel="canonical" href="{url}">', "canonical link"
    )
    for prop, value in (
        ("og:url", url),
        ("og:title", entry["og_title"]),
        ("og:description", entry["og_description"]),
        ("og:image", card),
    ):
        html = replace_once(
            html, rf'<meta property="{prop}" content="[^"]*">',
            f'<meta property="{prop}" content="{value}">', f"{prop} meta",
        )

    # og:locale and the alternates go in as a block; the canonical line is a
    # stable anchor that every variant has exactly one of.
    block = f'<meta property="og:locale" content="{entry["locale"]}">\n{alternates()}\n<link rel="canonical" href="{url}">'
    html = replace_once(html, rf'<link rel="canonical" href="{re.escape(url)}">', block, "canonical link")

    if slug:
        # A served language is a decision the URL already made, so record it for
        # the client. The root page carries no marker and keeps sniffing the
        # browser, which is what an x-default should do.
        html = replace_once(
            html, r'<meta name="color-scheme" content="light dark">',
            f'<meta name="color-scheme" content="light dark">\n<meta name="dbix-lang" content="{entry["lang"]}">',
            "color-scheme meta",
        )
        html = html.replace('<html lang="en">', f'<html lang="{entry["html_lang"]}">', 1)
        # Shared assets live one level up; nothing is duplicated per language.
        html = html.replace('href="fonts/inter.woff2"', f'href="{up}fonts/inter.woff2"')
        html = html.replace('url("fonts/inter.woff2")', f'url("{up}fonts/inter.woff2")')
        html = html.replace('"./index_bundle.bin"', f'"{up}index_bundle.bin"')

    return html


def sitemap() -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    urls = "\n".join(
        f"  <url>\n    <loc>{BASE}{entry['slug'] + '/' if entry['slug'] else ''}</loc>\n"
        f"    <lastmod>{today}</lastmod>\n  </url>"
        for entry in LANGS
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{urls}\n</urlset>\n'


def main() -> None:
    args = parse_args()
    web, index_dir, out = Path(args.web), Path(args.index_dir), Path(args.out)
    source = (web / "index.html").read_text(encoding="utf-8")

    out.mkdir(parents=True, exist_ok=True)
    shutil.copytree(web / "fonts", out / "fonts", dirs_exist_ok=True)
    for name in STATIC + [f"og-{entry['slug']}.png" for entry in LANGS if entry["slug"]]:
        shutil.copy(web / name, out / name)
    for name in ("index_bundle.bin", "index_bundle.json"):
        shutil.copy(index_dir / name, out / name)
    (out / "sitemap.xml").write_text(sitemap(), encoding="utf-8")

    for entry in LANGS:
        page = out / entry["slug"] / "index.html" if entry["slug"] else out / "index.html"
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(variant(source, entry), encoding="utf-8")
        print(f"{entry['lang']:8} {page}")


if __name__ == "__main__":
    main()
