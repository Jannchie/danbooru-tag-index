# danbooru-tag-index

A Google-Trends-style popularity index for Danbooru tags, and a zero-dependency
static site that charts it.

For every tag, how many posts it got per month across Danbooru's full history
(2005-05 to now, 255 months, 74k tags), normalised three ways, searchable in five
languages. The whole index ships as one 21.9 MB binary the browser downloads once.

```
uv sync
uv run python scripts/refresh.py         # stage 1 -- needs the metadata database
uv run python scripts/build_name_map.py
uv run python scripts/export_index_bundle.py
python -m http.server 8000               # then open http://localhost:8000/web/
```

The metadata database itself comes from the sibling project
[`Jannchie/danbooru_metadata`](https://huggingface.co/datasets/Jannchie/danbooru_metadata),
which syncs Danbooru's API into SQLite. Point `DANBOORU_DB` at it.

## Two stages, and why

The metadata database is **46 GB**. That single fact decides the architecture:
it cannot travel to CI, so the build is split at the smallest seam that lets
everything downstream of the database run anywhere.

**Stage 1** (`scripts/refresh.py`) runs on the machine that holds the database.
It scans 11.8M posts into monthly parquet, extracts the wiki alias pool, and
buckets wiki aliases by language. Roughly 45 minutes, resumable. It publishes
~75 MB of artifacts:

| Artifact | Size | What |
| --- | --- | --- |
| `fact_tag_monthly.parquet` | 30 MB | the index — posts per tag per month |
| `dim_tag.parquet` | 1.0 MB | tag id, name, category, lifetime post count |
| `fact_total_monthly.parquet` | 4 KB | site-wide monthly baseline |
| `fact_category_monthly.parquet` | 19 KB | per-category posts and `n_eff` |
| `wiki_other_names.json` | 2.3 MB | alias pool, search only |
| `{character,copyright,artist}_names.json` | 39 MB | wiki aliases bucketed by language |

**Stage 2** (`.github/workflows/build.yml`) runs in CI on every push. It downloads
those artifacts, combines them with the committed translation data, builds the
bundle, and deploys the site to Pages. **Nothing in stage 2 opens the database** —
that is the whole point of the split, and `export_wiki_aliases.py` exists only to
make it true.

To trigger a rebuild after a data refresh:

```
gh release upload data-latest <artifacts> --clobber
gh api repos/:owner/:repo/dispatches -f event_type=data-refreshed
```

## The three metrics

**Posts** — the raw monthly count. Measures output, but Danbooru's total upload
volume grows year over year, so it mostly tracks the platform's size.

**Share** — `tag posts / all posts × 10000`. Removes platform growth. Right for
tags that co-occur freely: `1girl` sits at ~7200 (72% of posts) for twenty years,
which is exactly the flat line it should be.

**Relative** — `(tag posts / category posts) × n_eff`, where `n_eff = 1/HHI` is
the effective number of equally sized competitors that month. **1.0 means "the
size of a typical tag in this category"**.

The third metric exists because share has a second bias that normalising by total
posts does not fix. Copyright tags are near mutually exclusive — an image usually
belongs to one franchise — so they compete for one finite pool of monthly uploads.
As the field fragments, every franchise's share falls even if its following is
unchanged. It fragmented a lot: `n_eff` for copyright went **12 → 56** between
2010 and 2026, and for character tags **181 → 1347**.

That distortion is big enough to invert conclusions. `touhou`'s share fell 1648 →
390 (2015 → 2024), reading as a 76% collapse; its relative index over the same
span went 2.22 → 1.46, and by 2026 it is at **2.65, a decade high**. Meanwhile
`kantai_collection` fell 2.84 → 0.55 on the relative index too — that decline is
real, just 5× rather than the 15× share implied. Use share for general tags and
relative for copyright/character.

```sql
-- relative index: 1.0 = the size of a typical tag in the same category
WITH per_month AS (
    SELECT d.name, d.category, m.month, m.posts,
           SUM(m.posts) OVER (PARTITION BY d.category, m.month) AS cat_posts
    FROM read_parquet('data/index/fact_tag_monthly.parquet') m
    JOIN read_parquet('data/index/dim_tag.parquet') d USING (tag_id)
)
SELECT p.month, ROUND(p.posts / p.cat_posts * c.n_eff, 2) AS relative
FROM per_month p
JOIN read_parquet('data/index/fact_category_monthly.parquet') c
  USING (category, month)
WHERE p.name = 'touhou' ORDER BY p.month;
```

Notes on the data:
- Tag aliases are resolved to their canonical name (alias chains included), so a
  concept is not split across renames.
- Tags with `post_count < 100` are dropped (`--min-post-count`); the long tail
  carries no statistical signal.
- Deleted posts are excluded by default (`--include-deleted` to keep them).
- Deprecated tags are kept — they were genuinely used at the time — and flagged
  in `dim_tag` so a consumer can hide them from search while still charting them.
- `created_at` is the *upload* time, not when the art was drawn. For measuring
  community attention that is the right semantics, but it is not a release date.
- Pre-2012 data is thin: Danbooru's tagging conventions were still forming, so
  early shares are unstable. Treat that range as indicative only.

## The bundle

`export_index_bundle.py` packs the monthly index into `index_bundle.bin`, a single
file the browser downloads once (21.9 MB raw, ~9.7 MB gzipped). Parquet would need
a multi-megabyte WASM reader to open client-side, so the bundle uses a purpose-built
layout that plain JavaScript parses with a `DataView`: fixed-width tag records,
LEB128-encoded monthly runs, and a lowercased search haystack. The header carries
its own epoch and complete-month count, so no client hardcodes either. Parsing
costs **~2 ms** — tag names, translations, the search haystack and each tag's
series are all decoded on demand.

`web/index.html` is a single fullscreen page with no build step and no
dependencies: search, three metrics, linear/log, a 0–36 month smoothing slider,
time ranges, light/dark, five UI languages, undo/redo on Ctrl+Z / Ctrl+Y, and
URL state in the hash. It is laid out for portrait phones as well as desktop.

Undo covers what you composed — the selected tags, their visibility, and the four
chart controls — but not theme or UI language, since undoing a chart edit should
not also flip the page back to dark. A slider drag is one undo step, not one per
pixel.

The readout above the chart is one control doing three jobs — it is the selection
list, the chart legend, and the value display, showing each series' value at the
crosshair (or at the latest complete month when not scrubbing). That is also what
keeps identity off colour alone at any screen width, since end labels are dropped
below 560 px.

### Smoothing

Smoothing is **Gaussian-weighted local linear regression** (LOESS, degree 1), not
a moving average. A moving average is badly biased wherever its window runs off
an edge: with only later months available, `1girl`'s first charted month smoothed
to 1313 against a true 318, because the mean of rising data is dragged up by the
trend. Shrinking the window does not help — the remaining samples are all on one
side. Fitting a local line and reading its centre is unbiased under a linear
trend however lopsided the window is, and distance weighting cuts roughness ~40%
while preserving peaks better (`k-on!`'s peak survives at 58% rather than 53%).

Both partial months are excluded from the fit as well as from the chart, or the
truncated ends would drag the curve near them.

The window is a slider rather than presets because the effect is continuous and,
at 253 months across ~900 px, a month is 3–4 px — narrower than the noise being
removed. Three buttons therefore read as inert even though they are not:
`touhou`'s roughness falls 27× from a 1- to a 12-month window. Dragging shows the
change as it happens, and the range runs to 36 months because only past ~24 does
the curve become a pure trend line.

Any centred smoother still spreads a sharp onset backwards in time — a 12-month
window around 2020-11 genuinely contains `blue_archive`'s 2021 surge. That is
what a 12-month average means; drag to 0 to see an onset unsmeared.

## Multilingual

Tags carry names in `zh_hans`, `zh_hant`, `ja`, `en`, `ko`, and search matches any
of them — `初音` and `하츠네` both find `hatsune_miku`, `けものみみ` finds
`animal_ears`. Search also folds katakana to hiragana and converts between Han
scripts, so `东方` and `東方` are equivalent queries. A full query costs 1–13 ms.

Three sources feed this, with deliberately different roles and trust levels.

**1. The wiki alias pool → search only.** 58.2k tags are reachable in some other
language this way. It is built for recall, not equivalence: it lists whatever
people call a tag, including narrower and related terms. Deriving display names
from it was tried and reverted — it claimed `school_uniform` was `制服スパッツ`
("school uniform + spats"). A wrong name is worse than none, so general tags stay
under their English name while remaining searchable in any language. When a match
comes from an alias, the suggestion list shows which alias hit.

**2. `*_name_map.json` → display names** for character/copyright/artist, 46.5k of
74k tags. Built by `build_name_map.py` from the language-bucketed alias pool plus
the committed `*_official.json`, which an LLM produced by **selecting** the
official name among each tag's candidates. Where only one Chinese script is
present, the other is generated by conversion.

**3. `zh_supplement.json` → Chinese names the first two cannot produce.** The
selection pass could only pick from what the wiki listed, so where Danbooru has
no Chinese alias it produced none — Chinese coverage sat at 24% for copyright.
This file **translates** instead of selecting. `translate_prep.py` batches the
tags that need one with their English name, Japanese name and alias pool as
anchors; `translate_merge.py` validates and merges the answers. It is a separate
file because its provenance is different: nothing in Danbooru backs it, so it must
be separately auditable and separately revertible.

| Category | Tags with a Chinese name | Posts under one | Tags ≥1000 posts |
| --- | --- | --- | --- |
| copyright | 33% | 85% | 82% |
| character | 42% | 76% | 89% |
| artist | 28% | 29% | — |
| general | — | — | — |

### What this pipeline gets wrong, and how it is caught

**Model choice is not incidental.** On the first batch of 100, Haiku answered 99
and about 20% were wrong, including `precure` → 美少女战士 (that is Sailor Moon)
and `kaname_madoka` → 圆神佳名子 (not a name); the instruction to return null when
unsure did not bind. Sonnet answered 71, declined the rest, and was right in every
disagreement. Since a wrong name is worse than none, this pass needs a model that
will decline.

**Validation catches only what is mechanically decidable**: no Han characters,
kana, Japanese-only glyphs, traditional instead of simplified, a copy of the
Japanese name, excessive length. Correctness of a translation is not decidable —
an earlier attempt to flag "contains Latin letters" rejected 哆啦A梦, 头文字D and
龙珠Z. The glyph check tests whether `jp2t`→`t2s` round-trips, because merely
asking whether `jp2t` changes the string flags 精灵宝可梦 (宝 is both a Japanese
shinjitai and a Chinese simplification).

**Chinese names shared by more than one tag are reported, not rejected.**
`metal_gear_solid` and `metal_gear_(series)` legitimately share one, as do
`lapras` and hololive's `La+ Darknesss`. But that same signal caught
`fate/unlimited_blade_works` carrying Steins;Gate's 命运石之门, `yamper` carrying
Cinnamoroll's 大耳狗, and `latios` carrying Latias's 拉帝亚斯.

**The alias pool can be wrong, not merely absent.** `dark_souls_(series)` carried
黑暗靈魂, a literal translation someone typed into the wiki, while 黑暗之魂 — the
name everyone actually uses — sat in the same pool misfiled as Japanese by
`han_language_overrides.json`. A comment in `export_tag_translations.py` argued
such ja/zh_hant mixups are harmless because `fill_cjk` regenerates the other
scripts, and that is true only when a tag has **one** Han candidate. When two
compete, the bucket decides which becomes the Chinese name and no later step can
recover the other. 1,645 tags have Han candidates split across `ja` and `zh`
buckets, 359 of them above 1,000 posts. `zh_manual.json` holds verified
corrections; the supplement layer overrides the map rather than only filling gaps,
because a fill-only layer could never have fixed this one.

**2,576 popular tags still carry a Chinese name that came from the pool and was
never reviewed by a translation pass.** `translate_prep.py` selects only tags with
no Chinese name, so those are not even candidates for review yet.

**A supplied Chinese name is not necessarily Chinese.** `fill_cjk` converts only
when *deriving* a missing field; a value the wiki bucket or `*_official.json`
supplies is trusted verbatim. 2,493 shipped names were therefore traditional or
Japanese while labelled `zh_hans` — 未来日記, 封神演義, 愛知万博, 戦国BASARA.
`normalize_chinese` now runs `t2s` over every supplied simplified name.

It runs **only** `t2s`, never `jp2t`. Applying `jp2t` to text that is already
Chinese turns shared characters back into Japanese: `mirinsoup`'s Japanese name
酢酸汁 converts correctly to 醋酸汁, and a second `jp2t` pass flips it back
(the mapping is bidirectional), while 默天蕓 → 默天芸 becomes 默天艺 because
`jp2t` reads 芸 as the shinjitai of 藝. `t2s` is the identity on simplified text,
so it cannot damage a value that is already right. The cost is that pure
shinjitai with no Chinese counterpart (戦, 伝, 錬) survive; removing those needs a
hand-checked character table, not a converter that bites back.

**`opencc` is pinned, not floated** — though calling 1.4.1 a bug would be too
strong. Only `jp2t` changed, and it changed both ways: it added faithful kyūjitai
restorations (郎 → 郞, 真 → 眞) and dropped Japanese-variant normalisations
(猫 → 貓, 内 → 內, 彦 → 彥, 聡 → 聰). The problem is that `fill_cjk` uses `jp2t`
as "give me a Chinese base", which is not what `jp2t` promises, and the pipeline
depends on `jp2t` → `t2s` round-tripping. It does for 真琴 (眞 → 真 is in `t2s`)
and does not for 士郎正宗 (郞 → 郎 is not), so 1.4.1 leaves kyūjitai stranded in
output labelled simplified Chinese. 6,663 names move. Nothing counts that — the
bundle builds to the same tag total and quietly renders different glyphs.
`test_opencc_produces_chinese_glyphs_not_japanese_variants` guards the pin.

## What is committed and what is not

`data/index/` and the regenerable halves of `data/translations/` are gitignored:
everything there is reproducible from the metadata database or from deterministic
code. These five files are **not**, and must never be — they cost real money and
nothing in this repo can regenerate them:

| File | Size | Produced by |
| --- | --- | --- |
| `han_language_overrides.json` | 3.3 MB | LLM pre-classification of Han strings |
| `copyright_official.json` | 2.1 MB | LLM selection among wiki candidates |
| `character_official.json` | 0.9 MB | LLM selection among wiki candidates |
| `zh_supplement.json` | 51 KB | LLM translation |
| `zh_manual.json` | — | hand-verified corrections |
