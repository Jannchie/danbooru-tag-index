"""Render the social preview card from the real index.

Run by hand, not in CI:

    uv run --with pillow --with "fonttools[woff]" python scripts/make_og_image.py --all

The output is committed. Teaching the build to rasterise text would drag a font
stack and a renderer into CI to regenerate an image that changes by a pixel a
year, and would put "renders differently on the runner" between a data refresh
and a green build.

The curves are real -- the tags the site seeds itself with, on the relative
index, in the site's palette, under the page's own typeface. A card drawn from
invented data would be a picture of a chart rather than a picture of this one.

One card per UI language, because a shared link previews in whatever language
its page is served in. The Latin card uses the page's own Inter; the CJK ones
use a system face for that script, since Pillow binds one file per text run and
cannot fall back mid-string -- and Inter, subset to Latin, has no Han glyphs at
all. Those faces are Windows-only paths; on another OS pass --font-dir.

Two rendering notes. Pillow antialiases text but not lines, so everything is
drawn at 4x and downsampled. And the webfont in web/fonts is woff2, which
FreeType will not open, so it is converted to a temporary TTF here rather than
committing a second copy of the same typeface in another wrapper.
"""

import argparse
import sys
import tempfile
from pathlib import Path

import duckdb
from PIL import Image, ImageChops, ImageDraw, ImageFont

from _paths import INDEX_DIR

WIDTH, HEIGHT = 1200, 630
SCALE = 4
MARGIN = 72

# The dark theme's tokens, so the card and the site it links to are one object.
BG_TOP, BG_BOTTOM = (23, 23, 22), (17, 17, 16)
TEXT = (255, 255, 255)
MUTED = (141, 140, 132)
DIM = (110, 109, 102)
FAINT = (58, 58, 55)
SERIES = [
    ("hatsune_miku", "hatsune_miku", (57, 135, 229)),
    ("touhou", "touhou", (217, 89, 38)),
    ("blue_archive", "blue_archive", (25, 158, 112)),
    ("genshin_impact", "genshin_impact", (201, 133, 0)),
    ("kantai_collection", "kantai_collection", (213, 81, 129)),
]

PLOT_TOP, PLOT_BOTTOM = 300, 584
LABEL_GUTTER = 232   # right-hand strip the curves stop short of, for end labels

# Slug is the file suffix and the site's sub-path; None means the root page.
LANGS = {
    "en": {
        "slug": None,
        "title": "Danbooru Tag Index",
        "lead": "Twenty years of anime tag popularity, month by month",
        "meta": "{tags} tags   ·   {months} months   ·   three normalisations   ·   five languages",
        "fonts": None,
        "title_size": 56,
    },
    "zh_hans": {
        "slug": "zh-hans",
        "title": "Danbooru 标签指数",
        "lead": "二十年二次元标签流行度，逐月记录",
        "meta": "{tags} 个标签   ·   {months} 个月   ·   三种归一化   ·   五种语言",
        "fonts": ("msyh.ttc", "msyhbd.ttc"),
        "title_size": 52,
    },
    "zh_hant": {
        "slug": "zh-hant",
        "title": "Danbooru 標籤指數",
        "lead": "二十年二次元標籤流行度，逐月記錄",
        "meta": "{tags} 個標籤   ·   {months} 個月   ·   三種歸一化   ·   五種語言",
        "fonts": ("msjh.ttc", "msjhbd.ttc"),
        "title_size": 52,
    },
    "ja": {
        "slug": "ja",
        "title": "Danbooru タグ指数",
        "lead": "20年分のタグ人気度を、月ごとに",
        "meta": "{tags} タグ   ·   {months} ヶ月   ·   3つの正規化   ·   5言語",
        "fonts": ("YuGothR.ttc", "YuGothB.ttc"),
        "title_size": 50,
    },
    "ko": {
        "slug": "ko",
        "title": "Danbooru 태그 지수",
        "lead": "20년간의 태그 인기도, 월 단위로",
        "meta": "{tags}개 태그   ·   {months}개월   ·   3가지 정규화   ·   5개 언어",
        "fonts": ("malgun.ttf", "malgunbd.ttf"),
        "title_size": 52,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Open Graph cards.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--font", type=str, default="web/fonts/inter.woff2")
    parser.add_argument("--font-dir", type=str, default="C:/Windows/Fonts", help="Where the system CJK faces live.")
    parser.add_argument("--out-dir", type=str, default="web")
    parser.add_argument("--lang", type=str, default=None, choices=sorted(LANGS), help="Render one language (default: all).")
    parser.add_argument("--all", action="store_true", help="Accepted for symmetry; rendering all is the default.")
    return parser.parse_args()


def load_curves(index_dir: Path) -> tuple[dict[str, list[float]], int, int]:
    """Monthly relative index per tag, with the tag taken out of its own benchmark."""
    con = duckdb.connect()
    monthly = (index_dir / "fact_tag_monthly.parquet").as_posix()
    dim = (index_dir / "dim_tag.parquet").as_posix()
    names = ", ".join(f"'{slug}'" for slug, _, _ in SERIES)
    rows = con.execute(f"""
        WITH pm AS (
            SELECT d.name, d.category, m.month,
                   m.posts * 1.0 / SUM(m.posts) OVER (PARTITION BY d.category, m.month) AS s
            FROM read_parquet('{monthly}') m
            JOIN read_parquet('{dim}') d USING (tag_id)
        ), h AS (
            SELECT category, month, SUM(s * s) AS hhi FROM pm GROUP BY category, month
        )
        SELECT p.name, p.month, p.s / (h.hhi - p.s * p.s) AS rel
        FROM pm p JOIN h USING (category, month)
        WHERE p.name IN ({names}) AND h.hhi - p.s * p.s > 0
        ORDER BY p.month
    """).fetchall()
    n_tags = con.execute(f"SELECT COUNT(*) FROM read_parquet('{dim}')").fetchone()[0]

    months = sorted({row[1] for row in rows})
    slot = {month: i for i, month in enumerate(months)}
    curves = {slug: [0.0] * len(months) for slug, _, _ in SERIES}
    for name, month, rel in rows:
        curves[name][slot[month]] = rel
    con.close()
    return curves, len(months), n_tags


def smooth(values: list[float], window: int = 9) -> list[float]:
    """Plain moving average -- the card is a silhouette, not a reading."""
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - window // 2), min(len(values), i + window // 2 + 1)
        span = values[lo:hi]
        out.append(sum(span) / len(span))
    return out


def load_font(woff2: Path) -> Path:
    """Unwrap the page's webfont into something FreeType will open."""
    from fontTools.ttLib import TTFont

    if not woff2.exists():
        raise SystemExit(f"font not found: {woff2}")
    font = TTFont(str(woff2))
    font.flavor = None
    out = Path(tempfile.gettempdir()) / "og-inter.ttf"
    font.save(str(out))
    return out


class Faces:
    """Resolves (size, weight) to a font file for one language.

    Inter is variable, so a weight is an axis value. The system CJK faces are
    not: there a weight is a different file, and asking one for an axis raises.
    """

    def __init__(self, latin: Path, cjk: tuple[Path, Path] | None):
        self.latin = latin
        self.cjk = cjk

    def __call__(self, size: int, weight: int) -> ImageFont.FreeTypeFont:
        if self.cjk is None:
            font = ImageFont.truetype(str(self.latin), size * SCALE)
            font.set_variation_by_axes([weight])
            return font
        regular, bold = self.cjk
        return ImageFont.truetype(str(bold if weight >= 600 else regular), size * SCALE, index=0)


def cjk_faces(spec: tuple[str, str] | None, font_dir: Path) -> tuple[Path, Path] | None:
    if spec is None:
        return None
    regular, bold = (font_dir / name for name in spec)
    missing = [str(path) for path in (regular, bold) if not path.exists()]
    if missing:
        raise SystemExit(f"system font not found: {missing}\nPass --font-dir, or edit LANGS.")
    return regular, bold


def backdrop() -> Image.Image:
    """A barely-there vertical wash, so 630px of flat black does not read as empty."""
    column = Image.new("RGB", (1, HEIGHT))
    for y in range(HEIGHT):
        t = y / (HEIGHT - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
    return column.resize((WIDTH * SCALE, HEIGHT * SCALE)).convert("RGBA")


def area_mask(height_px: int, fade_from: int, fade_to: int) -> Image.Image:
    """Alpha for the area fills: heavy at the curve, gone by the baseline.

    The horizontal term matters as much as the vertical one. Closing each fill
    to the baseline at its last month leaves a vertical wall down the right of
    the plot -- a hard edge that reads as a chart border nobody drew. Fading the
    last stretch to nothing lets the areas end without one, while the lines stay
    at full strength so the end labels still mark a real position.
    """
    column = Image.new("L", (1, height_px))
    for y in range(height_px):
        t = y / max(1, height_px - 1)
        column.putpixel((0, y), round(255 * (0.34 * (1 - t) + 0.03 * t)))
    mask = Image.new("L", (WIDTH * SCALE, HEIGHT * SCALE), 0)
    mask.paste(column.resize((WIDTH * SCALE, height_px)), (0, PLOT_TOP * SCALE))

    row = Image.new("L", (WIDTH, 1), 255)
    for x in range(fade_from, WIDTH):
        t = min(1.0, (x - fade_from) / max(1, fade_to - fade_from))
        row.putpixel((x, 0), round(255 * (1 - t)))
    return ImageChops.multiply(mask, row.resize((WIDTH * SCALE, HEIGHT * SCALE)))


def render(spec: dict, curves, n_months: int, n_tags: int, ttf: Path, font_dir: Path, out_dir: Path) -> Path:
    sized = Faces(ttf, cjk_faces(spec["fonts"], font_dir))
    s = SCALE

    image = backdrop()
    draw = ImageDraw.Draw(image)

    plot_right = WIDTH - LABEL_GUTTER
    peak = max(max(smooth(curves[slug])) for slug, _, _ in SERIES)
    ramp = area_mask((PLOT_BOTTOM - PLOT_TOP) * s, plot_right - 150, plot_right)

    draw.line([(0, PLOT_BOTTOM * s), (WIDTH * s, PLOT_BOTTOM * s)], fill=FAINT, width=1 * s)

    ends = []
    for slug, label, colour in SERIES:
        raw = curves[slug]
        # Months before the tag existed are zeros, and drawing them lays a flat
        # rule along the baseline that reads as data. The start comes from the
        # raw series: smoothing bleeds the first real month backwards, so the
        # smoothed curve would appear to begin years early.
        start = next((i for i, v in enumerate(raw) if v > 0), len(raw))
        values = smooth(raw)
        points = [
            (((i / (n_months - 1)) * plot_right) * s,
             (PLOT_BOTTOM - ((values[i] / peak) ** 0.5) * (PLOT_BOTTOM - PLOT_TOP)) * s)
            for i in range(start, len(values))
        ]
        if len(points) < 2:
            continue

        fill = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(fill).polygon(
            [(points[0][0], PLOT_BOTTOM * s), *points, (points[-1][0], PLOT_BOTTOM * s)],
            fill=(*colour, 255),
        )
        fill.putalpha(ImageChops.multiply(fill.getchannel("A"), ramp))
        image = Image.alpha_composite(image, fill)
        draw = ImageDraw.Draw(image)

        draw.line(points, fill=colour, width=3 * s, joint="curve")
        ends.append((points[-1][1] / s, label, colour))

    # End labels, the site's own device for naming a series. Separating them by
    # pushing down alone drives the lowest ones off the canvas -- four of these
    # five finish within a few pixels of each other -- so the run is pushed down,
    # then shifted back up as a block and re-separated upwards if it overflows.
    ends.sort(key=lambda e: e[0])
    gap, lo, hi = 27, PLOT_TOP - 8, PLOT_BOTTOM + 8
    ys = []
    for y, _, _ in ends:
        ys.append(max(y, ys[-1] + gap) if ys else y)
    if ys and ys[-1] > hi:
        ys = [y - (ys[-1] - hi) for y in ys]
        for i in range(len(ys) - 2, -1, -1):
            ys[i] = min(ys[i], ys[i + 1] - gap)
        ys = [max(y, lo) for y in ys]

    label_font = sized(15, 500)   # tag slugs are Latin in every language
    for (_, label, colour), y in zip(ends, ys):
        draw.ellipse(
            [((plot_right + 14) * s, (y - 3) * s), ((plot_right + 20) * s, (y + 3) * s)],
            fill=colour,
        )
        draw.text(((plot_right + 30) * s, (y - 9) * s), label, font=label_font, fill=colour)

    draw.text((MARGIN * s, 88 * s), spec["title"], font=sized(spec["title_size"], 680), fill=TEXT)
    draw.text((MARGIN * s, 172 * s), spec["lead"], font=sized(26, 400), fill=MUTED)
    draw.text(
        (MARGIN * s, 218 * s),
        spec["meta"].format(tags=f"{n_tags:,}", months=n_months),
        font=sized(18, 450),
        fill=DIM,
    )

    name = "og.png" if spec["slug"] is None else f"og-{spec['slug']}.png"
    out = out_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).save(out, optimize=True)
    return out


def main() -> None:
    args = parse_args()
    curves, n_months, n_tags = load_curves(Path(args.index_dir))
    ttf = load_font(Path(args.font))
    out_dir, font_dir = Path(args.out_dir), Path(args.font_dir)

    wanted = [args.lang] if args.lang else list(LANGS)
    for lang in wanted:
        out = render(LANGS[lang], curves, n_months, n_tags, ttf, font_dir, out_dir)
        print(f"{lang:8} {out}  {out.stat().st_size / 1024:.0f} KB", file=sys.stderr)


if __name__ == "__main__":
    main()
