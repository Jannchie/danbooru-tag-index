"""为 character 歧义角色准备 LLM 批次输入(挑官方名用)。

歧义定义:ja/ko/zh_hans/zh_hant 任一语言桶有 >1 候选,需要判断哪个是官方全名。
非歧义角色(每桶 ≤1)直接由 build_name_map 的启发式 + fill_cjk 处理,不进 LLM。
en 一律用 tag 罗马名,故批次里不含 en 候选。

输出:data/translations/_char_work/in_NNNN.json,每文件 BATCH 个 {tag: {lang:[候选]}}。
"""

import json
import math
from pathlib import Path

from _paths import TRANSLATIONS_DIR

BATCH = 100
PICK_LANGS = ("ja", "ko", "zh_hans", "zh_hant")


def main() -> None:
    base = Path(TRANSLATIONS_DIR)
    data = json.loads((base / "character_names.json").read_text(encoding="utf-8"))

    items: list[tuple[str, dict[str, list[str]]]] = []
    for tag, buckets in data.items():
        picks = {lang: buckets[lang] for lang in PICK_LANGS if buckets.get(lang)}
        if any(len(v) > 1 for v in picks.values()):
            items.append((tag, picks))

    work = base / "_char_work"
    work.mkdir(parents=True, exist_ok=True)
    # 清掉旧输入(保留 out_*.json 以便续跑)
    for f in work.glob("in_*.json"):
        f.unlink()

    n_batches = math.ceil(len(items) / BATCH)
    for i in range(n_batches):
        chunk = dict(items[i * BATCH : (i + 1) * BATCH])
        (work / f"in_{i:04d}.json").write_text(json.dumps(chunk, ensure_ascii=False), encoding="utf-8")

    print(f"ambiguous characters: {len(items)}")
    print(f"batches: {n_batches} (BATCH={BATCH}) -> {work}")


if __name__ == "__main__":
    main()
