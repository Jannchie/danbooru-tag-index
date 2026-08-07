"""合并 character LLM 批次输出 -> character_official.json,并报告缺失/损坏批次。

校验:每个 out_*.json 须为 {tag: {lang: name}};lang 仅取 ja/ko/zh_hans/zh_hant,
值须为非空字符串。损坏(JSON 解析失败)或缺失的批次下标会列出,供续跑。
"""

import json
from pathlib import Path

from _paths import TRANSLATIONS_DIR

PICK_LANGS = ("ja", "ko", "zh_hans", "zh_hant")


def main() -> None:
    base = Path(TRANSLATIONS_DIR)
    work = base / "_char_work"

    expected = {int(f.stem[3:]) for f in work.glob("in_*.json")}
    merged: dict[str, dict[str, str]] = {}
    corrupt: list[int] = []
    done: set[int] = set()

    for f in sorted(work.glob("out_*.json")):
        idx = int(f.stem[4:])
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, ValueError):
            corrupt.append(idx)
            continue
        if not isinstance(data, dict):
            corrupt.append(idx)
            continue
        done.add(idx)
        for tag, langs in data.items():
            if not isinstance(langs, dict):
                continue
            clean = {
                lang: langs[lang].strip()
                for lang in PICK_LANGS
                if isinstance(langs.get(lang), str) and langs[lang].strip()
            }
            if clean:
                merged[tag] = clean

    missing = sorted((expected - done) | set(corrupt))
    (base / "character_official.json").write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    counts = {lang: sum(1 for v in merged.values() if v.get(lang)) for lang in PICK_LANGS}
    print(f"merged tags: {len(merged)} -> character_official.json")
    print(f"  per-lang: {counts}")
    print(f"  batches done: {len(done)}/{len(expected)}")
    if missing:
        print(f"  MISSING/corrupt batch indices ({len(missing)}): {missing}")
        print("  -> 续跑: Workflow args 传这个数组即可")
    else:
        print("  all batches present")


if __name__ == "__main__":
    main()
