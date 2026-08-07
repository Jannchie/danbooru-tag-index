"""Render the social preview card from the real index.

Run by hand, not in CI. The output is committed, because the alternative is
teaching the build to rasterise text -- a font stack, a rendering library and a
whole class of "renders differently on the runner" bugs -- to regenerate an
image whose content changes by a pixel a year.

    uv run --with pillow python scripts/make_og_image.py

The curves are real: the same tags the site seeds itself with, on the same
relative index, in the same palette. A card drawn from invented data would be a
drawing of a chart rather than a picture of this one.

Pillow has no antialiasing for lines, so everything is drawn at 4x and
downsampled -- cheaper than compositing coverage by hand, and the text comes
along for free.
"""

import argparse
import sys
from pathlib import Path

import duckdb
from PIL import Image, ImageDraw, ImageFont

from _paths import INDEX_DIR

WIDTH, HEIGHT = 1200, 630
SCALE = 4

# The dark theme's tokens, so the card and the site it links to are the same
# object. Kept as literals: CSS is the source, but a parser for one dict of
# colours would be more code than the six lines it replaces.
BG = (19, 19, 18)
TEXT = (255, 255, 255)
MUTED = (141, 140, 132)
HAIRLINE = (51, 51, 49)
SERIES = [
    ("hatsune_miku", (57, 135, 229)),
    ("touhou", (217, 89, 38)),
    ("blue_archive", (25, 158, 112)),
    ("genshin_impact", (201, 133, 0)),
    ("kantai_collection", (213, 81, 129)),
]

FONT_DIR = Path("C:/Windows/Fonts")
FONTS = {"bold": "seguisb.ttf", "regular": "segoeui.ttf"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Open Graph card.")
    parser.add_argument("--index-dir", type=str, default=str(INDEX_DIR))
    parser.add_argument("--output", type=str, default="web/og.png")
    parser.add_argument("--font-dir", type=str, default=str(FONT_DIR))
    return parser.parse_args()


def load_curves(index_dir: Path) -> tuple[dict[str, list[float]], int, int]:
    """Monthly relative index per tag, with the tag taken out of its own benchmark."""
    con = duckdb.connect()
    monthly = (index_dir / "fact_tag_monthly.parquet").as_posix()
    dim = (index_dir / "dim_tag.parquet").as_posix()
    names = ", ".join(f"'{name}'" for name, _ in SERIES)
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
    totals = con.execute(f"SELECT COUNT(*) FROM read_parquet('{dim}')").fetchone()[0]

    months = sorted({row[1] for row in rows})
    slot = {month: i for i, month in enumerate(months)}
    curves = {name: [0.0] * len(months) for name, _ in SERIES}
    for name, month, rel in rows:
        curves[name][slot[month]] = rel
    con.close()
    return curves, len(months), totals


def smooth(values: list[float], window: int = 9) -> list[float]:
    """Plain moving average -- the card is a silhouette, not a reading."""
    out = []
    for i in range(len(values)):
        lo, hi = max(0, i - window // 2), min(len(values), i + window // 2 + 1)
        span = values[lo:hi]
        out.append(sum(span) / len(span))
    return out


def font(name: str, size: int, font_dir: Path) -> ImageFont.FreeTypeFont:
    path = font_dir / FONTS[name]
    if not path.exists():
        raise SystemExit(f"font not found: {path}\nPass --font-dir, or edit FONTS.")
    return ImageFont.truetype(str(path), size * SCALE)


def main() -> None:
    args = parse_args()
    index_dir = Path(args.index_dir)
    font_dir = Path(args.font_dir)
    curves, n_months, n_tags = load_curves(index_dir)

    image = Image.new("RGB", (WIDTH * SCALE, HEIGHT * SCALE), BG)
    draw = ImageDraw.Draw(image)
    s = SCALE

    # The chart occupies the lower two thirds and bleeds off both edges: it is a
    # texture behind the words, not a figure that needs axes to be read.
    plot_top, plot_bottom = 300, 610
    peak = max(max(smooth(v)) for v in curves.values())

    for y in (plot_top, (plot_top + plot_bottom) // 2, plot_bottom):
        draw.line([(0, y * s), (WIDTH * s, y * s)], fill=HAIRLINE, width=1 * s)

    for name, colour in SERIES:
        raw = curves[name]
        # A tag's months before it existed are zeros, and drawing them lays a
        # flat rule along the baseline that reads as data. The start comes from
        # the raw series: smoothing bleeds the first real month backwards into
        # the zeros, so asking the smoothed curve would begin several years early.
        start = next((i for i, v in enumerate(raw) if v > 0), len(raw))
        values = smooth(raw)
        points = []
        for i in range(start, len(values)):
            x = (i / (n_months - 1)) * WIDTH
            y = plot_bottom - (values[i] / peak) * (plot_bottom - plot_top)
            points.append((x * s, y * s))
        if len(points) > 1:
            draw.line(points, fill=colour, width=3 * s, joint="curve")

    title = font("bold", 52, font_dir)
    lead = font("regular", 27, font_dir)
    foot = font("regular", 20, font_dir)

    draw.text((72 * s, 92 * s), "Danbooru Tag Index", font=title, fill=TEXT)
    draw.text((72 * s, 172 * s), "Twenty years of anime tag popularity, month by month", font=lead, fill=MUTED)
    draw.text(
        (72 * s, 212 * s),
        f"{n_tags:,} tags  ·  {n_months} months  ·  three normalisations  ·  five languages",
        font=foot,
        fill=MUTED,
    )

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS).save(out, optimize=True)
    print(f"{out}  {out.stat().st_size / 1024:.0f} KB  ({WIDTH}x{HEIGHT})", file=sys.stderr)


if __name__ == "__main__":
    main()
