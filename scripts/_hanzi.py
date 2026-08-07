"""繁简转换里必须避开 OpenCC 的语义简化。

`t2s` 做的不全是字形转换。有一类字它按**意思**替换:乾→干、吒→咤、於→于、薹→苔、
徵→征。这些字本身就是规范简体(哪吒、乾坤、蒜薹、於菟),被替换之后是错字。

CJK 基本区里有 3,532 个字满足「jp2t 不动、t2s 会改」,绝大多数是正当的繁→简
(並→并、來→来)。分不出哪些是语义替换 —— 那需要一张字表。所以这里只列**实测在本
项目已发布名字里出现过**的那几个:5 个字、17 个名字(哪吒之魔童降世、武动乾坤、
於菟、蒜薹、徵弦★),外加审查批次里撞到的 紬。

这不是通用修复。语料变了就要重新量一遍 —— 量的办法是枚举 jp2t 不动而 t2s 会改的字,
和已发布名字取交集,人眼过一遍那个交集(实测只有个位数)。
"""

import re

import opencc

_t2s = opencc.OpenCC("t2s")
_jp2t = opencc.OpenCC("jp2t")

# 转换器会动、但动了就是错的字。一律不交给 OpenCC —— 摘出来原样保留。
#
# 两个来源,同一种毛病:转换器做的不只是字形映射,还有语义/异体替换。
#   t2s 把它们按意思换掉:吒->咤、乾->干、於->于、薹->苔、徵->征、紬->䌷
#   jp2t 把它们误当成日文新字体:芸(读成 藝)、疏(换成日文形 疎)、予(换成 豫)、
#     欠(换成 缺)、醋(双向映射到 酢);装 则是 t2s 的短语规则「衣装->衣裳」,摘出单字就跨不过去
#
# 每一个都是实测出来的:先枚举转换器会改的字,和已发布名字取交集,再人眼过一遍那个
# 交集。不加保护时它们会把「哪吒之魔童降世」写成「哪咤」、「予愿安洁莉娜」写成
# 「豫愿」、「缺月得疏桐」写成「疎桐」—— 共 171 个名字。语料变了要重新量。
PROTECTED = frozenset("吒乾於薹徵紬芸疏予欠装醋")

# 保留一份对照,好在 OpenCC 某天修好之后能发现这里的例外已经过期(见测试)。
SEMANTIC_QUIRKS = {
    "吒": "咤",
    "乾": "干",
    "於": "于",
    "薹": "苔",
    "徵": "征",
    "紬": "䌷",
}
# 日文专用字形里可以确定性转写的两个。OpenCC 的 jp2t 都不管:
#   姫 -> 姬(中文没有 姫 这个字形)
#   々 -> 重复前一个字(日文叠字符号,现代中文不用):幽々子 -> 幽幽子
# 修掉这两个能救 1,829 条已发布名字,最热的是 saigyouji_yuyuko(27,787 投稿)的
# 「西行寺幽々子」。其余的日文新字体交给 jp2t。
JP_GAPS = {"姫": "姬"}
ITERATION_MARK = "々"

# OpenCC 的 jp2t 覆盖绝大多数日文新字体(测过 24 个字形只漏 1 个),这里补它的漏网:
# 姫 中文应作「姬」(jp2t 原样返回),々 是日文叠字符号,现代中文不用。折进 is_simplified
# 而不是留给调用方,因为漏掉它就等于放过「四季映姫」这类值,而那正是它要拦的。
JP_ONLY = re.compile(r"[姫々]")


def to_simplified(text: str) -> str:
    """繁体→简体,但不让 OpenCC 动 PROTECTED 里的字。

    做法是把 quirk 字**摘出来**、其余交给 t2s,而不是事后把输出里的字换回去。后者是
    无条件反向替换,会把本来正确的字改坏:「鲁道夫象征」被写成「象徵」、「关于我转生」
    被写成「关於我转生」—— 那些 征/于 本来就是对的,只是恰好等于某个 quirk 的错误形。

    代价:在 quirk 字处切段,t2s 的短语级规则跨不过切口。quirk 只有 6 个字,很罕见,
    而切错的后果远小于把「哪吒」写成「哪咤」。
    """
    return _protecting(_t2s.convert, text)


def _protecting(convert, text: str) -> str:
    """逐段跑 convert,PROTECTED 里的字原样留下。"""
    out: list[str] = []
    run: list[str] = []
    for char in text:
        if char in PROTECTED:
            if run:
                out.append(convert("".join(run)))
                run = []
            out.append(char)
        else:
            run.append(char)
    if run:
        out.append(convert("".join(run)))
    return "".join(out)


def _from_japanese(text: str) -> str:
    return _protecting(_jp2t.convert, text)


def strip_japanese_glyphs(text: str) -> str:
    """把 jp2t 管不到的两个日文字形转写成中文,再交给它处理其余的。"""
    out: list[str] = []
    for char in text:
        if char == ITERATION_MARK:
            # 叠字符号重复前一个字。开头就是它的话没有可重复的对象,原样留下让校验拦。
            if out:
                out.append(out[-1])
            else:
                out.append(char)
        else:
            out.append(JP_GAPS.get(char, char))
    return "".join(out)


def repair_to_simplified(text: str) -> str:
    """把掺了日文字形或繁体的名字修成规范简体;修不动就原样返回。

    只在当前**不合格**时才动手。已经是规范简体的串一律不碰 —— 对它跑 jp2t 会把共用字
    当成日文倒转回去:`mirinsoup` 的「醋酸汁」会变回「酢酸汁」(jp2t 双向映射 酢⇄醋),
    「默天芸」会变成「默天艺」(jp2t 把 芸 读成 藝 的新字体)。这道闸门是整个函数能安全
    存在的前提。

    修不动就返回原值而不是一个更差的猜测:失败的转写留在原地,由 is_simplified 拦下来
    报告,而不是悄悄替换成错的。
    """
    if is_simplified(text):
        return text
    candidate = to_simplified(_from_japanese(strip_japanese_glyphs(text)))
    return candidate if is_simplified(candidate) else text


def is_simplified(text: str) -> bool:
    """这串是否已经是规范简体(不掺日文新字体、不是繁体)。

    看 jp2t→t2s 往返能否还原自己。单看 jp2t 有变化不行:日文新字体和中文简化字大量
    重合(宝/実→寶/實 都会变),那样「精灵宝可梦」会被误判成日文。简体字往返回来还是
    自己(宝→寶→宝),日文专用字形不会(黒→黑→黑≠黒)。繁体也会被拦下,那是想要的:
    只收简体,繁体由程序转换得出。
    """
    if JP_ONLY.search(text):
        return False
    return to_simplified(_from_japanese(text)) == text


def has_japanese_glyphs(text: str) -> bool:
    """含中文里不存在的日文字形(新字体、姫、叠字符号)。

    不判简繁 —— 繁体字段本来就该是繁体,拿简体标准去衡量它只会全军覆没。
    """
    return bool(JP_ONLY.search(text)) or ITERATION_MARK in text or _from_japanese(text) != text


def repair_to_traditional(text: str) -> str:
    """把掺了日文字形的繁体名字修成规范繁体;没有日文字形就不碰。

    繁体字段以前完全不修,理由是「它已经是繁体」。实际上它常常是别名池里的日文原值:
    `battle_tendency` 的繁体是「戦鬥潮流」、`sakurauchi_riko` 是「桜內梨子」、
    `tsukihime` 是「月姫」。jp2t 的目标就是繁体,所以这里比简体那边还直接 —— 不需要
    再走一次 t2s。
    """
    if not has_japanese_glyphs(text):
        return text
    candidate = _from_japanese(strip_japanese_glyphs(text))
    return candidate if not has_japanese_glyphs(candidate) else text
