"""合并 LLM 批次输出 -> zh_supplement.json,并做机械校验。

单独一个文件、不并入 *_official.json,是因为两者可信度不同:official 是 LLM 从 wiki
候选里**挑**的,有原始数据背书;这里是**翻**的,没有。分开放才能分别审计、分别回退,
重跑任一遍也不会覆盖另一遍。

校验只做高精度的几项。翻译的正确性机械上判不出来(试过按「含拉丁字母」判坏值,结果
把「哆啦A梦」「头文字D」「龙珠Z」全判错了),所以这里只拦下确定性的失败模式:不含
汉字、含日文新字体、和日文名逐字相同、过长。跨 tag 重名只报告不拦 —— metal_gear_solid
和 metal_gear_(series) 共用一个中文名是对的,但 fate/unlimited_blade_works 拿到
steins;gate 的「命运石之门」不是,这个只能人看。
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NamedTuple

import opencc

from _paths import TRANSLATIONS_DIR
from translate_prep import WORK_DIRNAME

HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
# OpenCC 的 jp2t 覆盖绝大多数日文新字体(测了 24 个字形只漏 1 个),这里补它的漏网:
# 姫 中文应作「姬」(jp2t 原样返回),々 是日文叠字符号,现代中文不用。
JP_ONLY = re.compile(r"[姫々]")
MAX_LEN = 60
MANUAL_FILE = "zh_manual.json"

_jp2t = opencc.OpenCC("jp2t")  # 日文新字体 → 繁体
_t2s = opencc.OpenCC("t2s")  # 繁体 → 简体


def not_simplified(text: str) -> bool:
    """这串是否不是规范简体中文(掺了日文新字体,或整个是繁体)。

    单看 jp2t 有变化不行:日文新字体和中文简化字大量重合(宝/実→寶/實 都会变),那样
    「精灵宝可梦」会被误判成日文。改看**往返**能否还原 —— 简体字 jp2t 转成繁体再 t2s
    转回来还是自己(宝→寶→宝),而日文专用字形不会(黒→黑→黑≠黒,剣→劍→剑≠剣)。
    顺带把繁体也拦下:我们只要简体,繁体由程序转换得出。
    """
    return _t2s.convert(_jp2t.convert(text)) != text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge LLM translation batches into zh_supplement.json.")
    parser.add_argument("--dir", type=str, default=str(TRANSLATIONS_DIR))
    return parser.parse_args()


def reject(tag: str, zh: str, anchors: dict) -> str | None:
    """拒绝理由,通过则返回 None。"""
    if not isinstance(zh, str) or not zh.strip():
        return "empty"
    zh = zh.strip()
    if len(zh) > MAX_LEN:
        return "too-long"
    if not HAN.search(zh):
        return "no-han"
    if KANA.search(zh):
        return "contains-kana"
    if JP_ONLY.search(zh) or not_simplified(zh):
        return "not-simplified"
    if zh == (anchors.get("ja") or "").strip():
        return "same-as-japanese"
    if zh.lower() == tag.replace("_", " "):
        return "same-as-tag"
    return None


class Batches(NamedTuple):
    inputs: dict[str, dict]      # tag -> 锚点(prep 写的)
    expected: set[int]           # 有 in_*.json 的批次号
    done: set[int]               # 有可解析 out_*.json 的批次号
    corrupt: list[int]           # out_*.json 存在但坏了
    answers: dict[str, str]      # tag -> LLM 给的中文名(未校验)


def load_batches(work: Path) -> Batches:
    inputs: dict[str, dict] = {}
    expected: set[int] = set()
    for path in sorted(work.glob("in_*.json")):
        expected.add(int(path.stem[3:]))
        inputs.update(json.loads(path.read_text(encoding="utf-8")))

    done: set[int] = set()
    corrupt: list[int] = []
    raw: dict[str, str] = {}
    for path in sorted(work.glob("out_*.json")):
        index = int(path.stem[4:])
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, ValueError):
            corrupt.append(index)
            continue
        if not isinstance(data, dict):
            corrupt.append(index)
            continue
        done.add(index)
        for tag, value in data.items():
            if isinstance(value, dict):
                value = value.get("zh_hans")
            if value is not None:
                raw[tag] = value
    return Batches(inputs, expected, done, corrupt, raw)


def main() -> None:
    # 这个脚本的报告主体就是中文名。Windows 控制台默认 cp932/gbk 编不了,不重配就会
    # 在打印被拒条目时抛 UnicodeEncodeError,而那正是最需要看到的一行。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()
    base = Path(args.dir)
    work = base / WORK_DIRNAME
    if not work.exists():
        raise SystemExit(f"no work dir at {work} -- run translate_prep.py first")

    batch = load_batches(work)
    inputs = batch.inputs

    accepted: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    unknown = 0
    for tag, zh in batch.answers.items():
        anchors = inputs.get(tag)
        if anchors is None:
            unknown += 1
            continue
        why = reject(tag, zh, anchors)
        if why:
            rejected.setdefault(why, []).append(f"{tag}={zh}")
        else:
            accepted[tag] = zh.strip()

    # 人工核实过的修正。批次输出是可重跑的产物,手改会被下次合并覆盖;而且有些错的
    # tag 根本不在批次里(prep 只挑没有中文名的,像 dark_souls_(series) 那样「有名字
    # 但名字是错的」永远进不来)。所以单独一个文件,最后应用,不受批次成员检查限制。
    manual_path = base / MANUAL_FILE
    manual_applied = 0
    if manual_path.exists():
        for tag, zh in json.loads(manual_path.read_text(encoding="utf-8")).items():
            why = reject(tag, zh, inputs.get(tag, {}))
            if why:
                rejected.setdefault(f"{why} (manual)", []).append(f"{tag}={zh}")
            else:
                accepted[tag] = zh.strip()
                manual_applied += 1

    output = base / "zh_supplement.json"
    merged = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
    merged.update(accepted)
    output.write_text(json.dumps(merged, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")

    by_zh: dict[str, list[str]] = {}
    for tag, zh in merged.items():
        by_zh.setdefault(zh, []).append(tag)
    dupes = {zh: tags for zh, tags in by_zh.items() if len(tags) > 1}

    print(f"zh_supplement.json: {len(merged)} names ({len(accepted)} from this merge, {manual_applied} hand-verified)")
    print(f"  batches done: {len(batch.done)}/{len(batch.expected)}   asked for {len(inputs)} tags, answered {len(batch.answers)}")
    for why, items in sorted(rejected.items()):
        print(f"  REJECTED {why}: {len(items)} -> {', '.join(items[:6])}")
    if unknown:
        print(f"  ignored {unknown} tags that were not in any batch")
    if dupes:
        print(f"  REVIEW {len(dupes)} Chinese names used by more than one tag:")
        for zh, tags in list(dupes.items())[:10]:
            print(f"    {zh} -> {tags}")
    missing = sorted((batch.expected - batch.done) | set(batch.corrupt))
    if missing:
        print(f"  MISSING/corrupt batches ({len(missing)}): {missing}")
    else:
        print("  all batches present")


if __name__ == "__main__":
    main()
