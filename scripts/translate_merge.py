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

from _hanzi import is_simplified
from _paths import TRANSLATIONS_DIR
from translate_prep import REVIEW_DIRNAME, WORK_DIRNAME

HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
KANA = re.compile(r"[぀-ヿㇰ-ㇿ]")
MAX_LEN = 60
MANUAL_FILE = "zh_manual.json"

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Merge LLM translation batches into zh_supplement.json.")
    parser.add_argument("--dir", type=str, default=str(TRANSLATIONS_DIR))
    parser.add_argument(
        "--review",
        action="store_true",
        help="合并审查批次(_zh_review)。答案与现有名字相同视为确认,不写入任何东西。",
    )
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
    if not is_simplified(zh):
        return "not-simplified"
    # 不检查「和日文名相同」。中日共用汉字名是常态(中野二乃、魈、橙 在两种语言里
    # 都是同一串),而 wiki 的 ja 桶里又混着被 han_language_overrides 误判成日文的
    # 中文名 —— 两件事叠起来,这条检查专门拒绝那些修复误判的正确答案:实测 24 条,
    # 包括为了守住「黒森峰女学园」(另一部作品的学校名)而丢掉「赤星小梅」,为了守住
    # 「恋柱」(称号)而丢掉「甘露寺蜜璃」。真正未翻译的日文串由 KANA 和
    # is_simplified 拦下,不需要这条。
    if zh.lower() == tag.replace("_", " "):
        return "same-as-tag"
    return None


CONFIRMED = "confirmed"


def judge(tag: str, zh: str, anchors: dict, review: bool) -> str | None:
    """把一个答案归类:CONFIRMED、None(采纳)、或拒绝理由。

    审查模式下答案等于现有名字就是「确认」,必须**不写入任何东西**。把确认也写进
    zh_supplement 会把「别名池随手挑的」提升成「审过的翻译」——它的可信度并没有提高,
    而下一轮审查再也找不到它们了(prep 靠「不在 supplement 里」来判定未审查)。
    """
    if review and isinstance(zh, str) and zh.strip() == (anchors.get("current_zh") or "").strip():
        return CONFIRMED
    return reject(tag, zh, anchors)


def collisions(answers: dict[str, str], inputs: dict[str, dict], existing: dict[str, str]) -> dict[str, list[str]]:
    """新名字撞上别的 tag、而旧名字没撞 —— 只报告,不拦。

    审查会被要求去掉「训练员(赛马娘)」这类作品后缀,对只有一个 Trainer 的情况是对的。
    但当同名的兄弟 tag 存在时,去掉限定就让两条曲线在图例上完全一样:
    `shameimaru_aya_(newsboy)` 本来叫「铃奈庵文」,改成「射命丸文」之后和本体
    `shameimaru_aya` 分不开了 —— 那是净损失。

    不自动回退,因为有一半的撞名是对的:`scaramouche_(genshin_impact)` 和
    `scaramouche_(harbinger)_(genshin_impact)` 是同一个角色,本就该同名;
    `dante_(devil_may_cry)` 和 `dante_(limbus_company)` 是两个都叫但丁的人。
    区分这两种要看 tag 之间是不是变体关系,机械上判不出来。
    """
    final = dict(existing)
    for tag, zh in answers.items():
        final[tag] = zh
    owners: dict[str, list[str]] = {}
    for tag, zh in final.items():
        owners.setdefault(zh, []).append(tag)

    out: dict[str, list[str]] = {}
    for tag, zh in answers.items():
        before = (inputs.get(tag) or {}).get("current_zh")
        others = [t for t in owners.get(zh, []) if t != tag]
        if not others or zh == before:
            continue
        # 旧名字本来就撞的,不算这次改出来的。
        if before and len([t for t in owners.get(before, []) if t != tag]):
            continue
        out[tag] = others
    return out


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
    work = base / (REVIEW_DIRNAME if args.review else WORK_DIRNAME)
    if not work.exists():
        raise SystemExit(f"no work dir at {work} -- run translate_prep.py first")

    batch = load_batches(work)
    inputs = batch.inputs

    accepted: dict[str, str] = {}
    rejected: dict[str, list[str]] = {}
    unknown = 0
    confirmed = 0
    for tag, zh in batch.answers.items():
        anchors = inputs.get(tag)
        if anchors is None:
            unknown += 1
            continue
        why = judge(tag, zh, anchors, args.review)
        if why == CONFIRMED:
            confirmed += 1
            continue
        if why:
            rejected.setdefault(why, []).append(f"{tag}={zh}")
        else:
            accepted[tag] = zh.strip()

    # 人工核实过的修正。批次输出是可重跑的产物,手改会被下次合并覆盖;而且有些错的
    # tag 根本不在批次里(prep 只挑没有中文名的,像 dark_souls_(series) 那样「有名字
    # 但名字是错的」永远进不来)。所以单独一个文件,最后应用,不受批次成员检查限制。
    # 两遍都应用人工修正,而且放在最后 —— 它比 LLM 的答案权威。曾经在审查模式下跳过它,
    # 因为 reject() 里有条检查依赖批次锚点、缺锚点就虚报失败;那条检查(same-as-japanese)
    # 已经删了,剩下的检查都只看值本身,所以这个特例没有理由再存在。
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
    if args.review:
        # 确认率是这一遍的主要产出:它量化了别名池挑出来的名字有多大比例本来就对。
        judged = confirmed + len(accepted) + sum(len(v) for v in rejected.values())
        rate = confirmed * 100 // max(judged, 1)
        print(f"  reviewed: {confirmed} confirmed as-is ({rate}%), {len(accepted)} corrected")
    for why, items in sorted(rejected.items()):
        print(f"  REJECTED {why}: {len(items)} -> {', '.join(items[:6])}")
    if unknown:
        print(f"  ignored {unknown} tags that were not in any batch")
    if dupes:
        print(f"  REVIEW {len(dupes)} Chinese names used by more than one tag:")
        for zh, tags in list(dupes.items())[:10]:
            print(f"    {zh} -> {tags}")
    if args.review:
        clashes = collisions(accepted, inputs, {t: (v or {}).get("current_zh", "") for t, v in inputs.items()})
        if clashes:
            print(f"  REVIEW {len(clashes)} corrections now clash with another tag (was distinct before):")
            for tag, others in list(clashes.items())[:15]:
                print(f"    {tag} = {accepted[tag]}  <-> {', '.join(others)}")
    missing = sorted((batch.expected - batch.done) | set(batch.corrupt))
    if missing:
        print(f"  MISSING/corrupt batches ({len(missing)}): {missing}")
    else:
        print("  all batches present")


if __name__ == "__main__":
    main()
