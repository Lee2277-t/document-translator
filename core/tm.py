"""翻译记忆（TM）：模糊匹配复用历史译文，保证同一项目里同一句话一个译法。

和译文缓存的区别
----------------
* ``TranslationCache`` 是**精确**匹配：同一个字符串原样命中。它省的是
  推理时间，但换一种说法（多一个空格、改一个标点）就完全失效；
* TM 是**模糊**匹配：``请确认以下条款`` 与 ``请确认下述条款`` 高度相似时
  直接复用，这正是 CAT 工具（Trados、memoQ）保证全文一致性的核心机制。

四级命中口径（v1.5.6）
----------------------
调用方按 ``Match.ratio`` 决定怎么用这条记忆，而不是拿到一个字符串就开始翻：

* ``ratio == 1.0``（精确）：**100% 直通**，不进翻译引擎，省网络/配额/时间；
* ``ratio >= threshold``（模糊）：可以用，但要标「待确认」，且**绝不回写 TM**
  ——近似句之间的差异常常正是关键信息，写回去等于把一处疑虑复印到全文；
* 低于阈值：当它没有。

工程取舍
--------
1. **只存通过校验的译文**。人工确认过（译后编辑）或从通过质量引擎校验的
   段落才进 TM——把存疑译文存进去，下一次模糊匹配会把同一个错误复印到更多
   句子上去。
2. **阈值默认 0.92**，宁可不用也不错用。低于此的"相似句"往往差在
   关键信息上（数字、否定词），复用比不翻更危险。
3. **候选预筛**。全量 SequenceMatcher 太慢（一万条记忆 × 一千句 =
   一千万次比对），先用长度差与共用 token 做廉价预筛，只有少数候选
   进入精确相似度计算。
4. **长度归一化惩罚**见 ``_similarity``：短句只差一个字就到 0.9 几，
   长句差同样比例的信息量却完全不同，因而对"长度差超过 20%"打折。
   **这条惩罚的实际作用域比字面小得多，改之前务必先读 ``_similarity`` 的
   docstring**：受上面第 3 条预筛的封顶，长度 ≥20 字的候选 gap 恒 ≤0.2，
   惩罚恒不生效——它实际只在短句上起作用。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, Iterable, NamedTuple

from .state import StateStore

#: 默认相似度阈值。0.92 是"几乎相同"与"差一点"的经验分界。
DEFAULT_THRESHOLD = 0.92

#: 预筛：长度差超过 max(4, 20%) 的直接出局。
_LENGTH_TOLERANCE = 0.2
_MIN_LENGTH_TOLERANCE = 4

#: 长度归一化惩罚的参数（v1.5.6）。为什么要除以长度而不是直接用编辑距离：
#: ``SequenceMatcher.ratio`` 本身是 2*M/(len_a+len_b)，它对**短句**天生偏心
#: ——两个 5 字短句改一个字还能有 0.8，而两个 40 字长句差同样比例的信息
#: （差 8 个字）比值反而更低。实践后果是短记忆显得"很像"、长记忆显得"不像"，
#: 同一个阈值在两种长度上宽严不一致。这里只对「长度差超过 20%」的
#: 那部分打折，最多打 5 折
#: （raw * (1 - 0.5 * max(0, gap-0.2))）——差得离谱的候选被拉到阈值以下，
#: 而"差一个标点""多一个句号"这类（gap 通常远小于 0.2）完全不受影响。
_GAP_FREE = 0.2
_GAP_PENALTY_WEIGHT = 0.5

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


class Match(NamedTuple):
    """一次记忆命中的全部信息。

    ``target`` 是复用的译文；``ratio`` 是相似度（1.0 表示完全一致）；
    ``origin`` / ``confirmed`` 来自库里的元信息（见 core/state.py 的
    ``_TM_EXTRA_COLUMNS``），供调用方决定"这条能不能直接当终稿用"——
    只有人工确认过的那一类才敢全自动采纳。
    """

    target: str
    ratio: float
    origin: str = "auto"
    confirmed: bool = False


def fuzzy_hit(paragraph: int, source: str, match: Match) -> dict:
    """一条「记忆近似复用」明细，四个引擎共用同一份形状（v1.9.9）。

    ``paragraph`` 是 1 起的段号，与 ``term_hits`` 同一个口径（段号取本次
    ``translate_texts`` 调用里的位置，前端怎么用它就怎么念）。只存内存、
    不落盘——保密模式零落盘承诺不受影响。记明细而不是只记计数，是因为
    「待确认」三个字如果只出现在一句汇总里，用户找不到要确认的段。
    """
    return {"paragraph": paragraph, "source": source, "ratio": match.ratio}


def _similarity(source: str, candidate: str) -> float:
    """相似度计算：SequenceMatcher 原始比值 × 长度归一化惩罚。

    返回值落在 [0, 1]。惩罚只作用于长度差超过 ``_GAP_FREE`` 的那部分，
    所以"差一个标点/多一个句号"（gap 通常 <0.1）完全不受影响——这也是
    既有测试（``test_fuzzy_hit_above_threshold``）赖以成立的前提。

    **实际作用域（量化实测，别被上面的"长句"措辞误导）**：``_cheap_prefilter``
    先按 ``max(4, 0.2*len(source))`` 的容差把候选筛过一遍，能走到这里的
    候选其 gap 已经被封顶——长候选 ``diff/(len+diff)`` ≤ 0.167、短候选
    ``diff/len`` ≤ 0.2。于是：

    * **长度 ≥20 字的句子：gap 恒 ≤0.2 → 惩罚恒等于 1.0，即完全不生效**
      （实测长句 gap=0.069 时 raw=penalized=0.9643）；
    * **短句（长度 <20，固定容差 4 字占主导）：惩罚才真的起作用**
      （实测 4 字对 12 字：raw 0.5 → 0.3833）。

    换句话说这条惩罚是**短句专属**的。要让它对长句也生效，得先放宽预筛
    容差——那是``_cheap_prefilter``的改动，会牵动匹配召回与性能，不是这里
    改一个数就能办到的事；在动它之前，本函数的行为就是这个样子。
    """
    if not source and not candidate:
        return 1.0
    if not source or not candidate:
        return 0.0
    raw = SequenceMatcher(None, source, candidate).ratio()
    longest = max(len(source), len(candidate), 1)
    gap = abs(len(source) - len(candidate)) / longest
    return raw * (1.0 - _GAP_PENALTY_WEIGHT * max(0.0, gap - _GAP_FREE))


def _tokens(text: str) -> set[str]:
    """把文本切成用于预筛的 token 集合。

    西文按词切；中文**必须切二元组**——一开始图省事用 ``[\\u4e00-\\u9fff]{2,}``
    整段切，结果每个中文句子都是一个 token，两个不同的句子永远没有交集，
    预筛把全部候选挡在外面，中文模糊匹配直接失效（症状是"TM 永远不命中"，
    但单元测试用英文写的时候完全正常）。二元组是中文信息检索的常规做法。
    """
    tokens: set[str] = set()
    for word in _WORD_RE.findall(text or ""):
        tokens.add(word.lower())
    for run in _CJK_RUN_RE.findall(text or ""):
        if len(run) == 2:
            tokens.add(run)
        else:
            for index in range(len(run) - 1):
                tokens.add(run[index : index + 2])
    return tokens


class TranslationMemory:
    """翻译记忆。底层是 StateStore 的 tm 表，与术语库/角色卡同一个库。"""

    def __init__(
        self,
        store: StateStore | None = None,
        threshold: float = DEFAULT_THRESHOLD,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store or StateStore()
        self.threshold = threshold
        self.log = log or (lambda message: None)
        self._entries: list[tuple[str, str]] | None = None
        #: 带元信息的行缓存。与 ``_entries`` 同一套"读一次、写时维护"的口径：
        #: match() 是逐段调用的热路径，每次重跑 SELECT 会把它变成 O(N) 次全表读。
        self._full: list[dict[str, object]] | None = None
        self._hits = 0
        self._misses = 0

    # ---------------------------------------------------------------- 读写

    def add(
        self,
        source: str,
        target: str,
        origin: str = "auto",
        confirmed: int = 0,
        src_lang: str = "",
    ) -> None:
        """记一条译文。默认按「引擎自动产出、未经人工确认」记。

        ``origin`` / ``confirmed`` / ``src_lang`` 只是透传给
        ``StateStore.upsert_tm``（三列的意义见 core/state.py）。调用方要声称
        「这是人工确认过的终稿」必须显式传 ``confirmed=1``——默认值不背书。
        """
        source = (source or "").strip()
        target = (target or "").strip()
        if not source or not target or source == target:
            return
        try:
            self.store.upsert_tm(
                source,
                target,
                origin=origin,
                confirmed=confirmed,
                src_lang=src_lang,
            )
        except TypeError:
            # 替身 store（只认两参数的老签名测试桩）：退回原形态，不因新的
            # 元信息列把整个记忆功能拖挂。
            self.store.upsert_tm(source, target)
        if self._entries is not None:
            self._entries.insert(0, (source, target))
        if self._full is not None:
            self._full.insert(
                0,
                {
                    "source": source,
                    "target": target,
                    "origin": origin,
                    "confirmed": bool(confirmed),
                    "src_lang": src_lang,
                },
            )

    def confirm(self, source: str, target: str, src_lang: str = "") -> None:
        """记一条**人工确认过**的译文（译后编辑的传播源头）。

        「确认一次、全文生效」就是靠它：人在译后编辑里改出来的这段既人工
        拍过板，之后无论精确还是模糊命中都可以放心复用，因此
        ``origin="human"`` + ``confirmed=1``——这是记忆库里唯一全自动信任
        的那一类。
        """
        self.add(source, target, origin="human", confirmed=1, src_lang=src_lang)

    def _load(self) -> list[tuple[str, str]]:
        if self._entries is None:
            try:
                self._entries = self.store.all_tm()
            except Exception:
                self._entries = []
        return self._entries

    def _load_full(self) -> list[dict[str, object]]:
        """读带元信息的记忆行（缓存）。

        拿不到（替身 store / 结构异常）就退回 ``all_tm`` 的两列形态，退化成
        ``origin="auto"``、``confirmed=False``——宁可读到缺信息的行，也不要
        因为缺信息让整条模糊匹配失效。
        """
        if self._full is not None:
            return self._full
        try:
            rows = self.store.all_tm_full()
        except Exception:
            rows = []
        if not rows:
            rows = [
                {
                    "source": source,
                    "target": target,
                    "origin": "auto",
                    "confirmed": False,
                    "src_lang": "",
                }
                for source, target in self._load()
            ]
        self._full = rows
        return self._full

    def _meta_for(self, source: str) -> tuple[str, bool]:
        """取某条记忆的 (origin, confirmed)。

        精确命中后补一次查询拿到元信息——调用方据此决定是「100% 直通」
        还是「近似复用待确认」。查不到时按最保守的 (auto, False) 返回：
        宁可多标一句"待确认"，也不要把没背书过的译文当成终稿。
        """
        for row in self._load_full():
            if row.get("source") == source:
                return (
                    str(row.get("origin") or "auto"),
                    bool(row.get("confirmed")),
                )
        return ("auto", False)

    # ---------------------------------------------------------------- 查询

    def match(self, source: str) -> Match | None:
        """查记忆。精确命中优先；否则在预筛后的候选里找最相似的一条。

        返回 ``Match``（含 ratio / origin / confirmed），调用方据此分级使用：

        * ``ratio == 1.0`` → **100% 直通**，不必再进翻译引擎；
        * ``ratio >= threshold`` → 模糊复用，**标待确认**，用归用、绝不再
          写回记忆（把近似句的差异复印出去是不允许的）。

        四个档位（cloud / paid / offline / local-llm）都从这里取**命中判定**
        的口径（同一对原文/候选得到同一个 ratio、同一套 origin/confirmed），
        但**查询排在缓存前还是后，各档并不一致**——这是主动取舍，不是疏漏：

        * **cloud（core/translator.py）与 paid（core/deepl.py）：记忆在缓存
          之前**。这两档命中的收益是实打实的省钱省网络（DeepL 按字符计费，
          命中一次就是一次不计费的请求），所以记忆必须抢在缓存前面；
        * **local-llm（core/local_llm.py）：缓存在记忆之前**。这一档的缓存
          与记忆都在本机、都廉价，谁先谁后没有可计量的差别，因此**原样保留
          了接线前的既有顺序**，不为"看起来统一"去承受一次回归风险；
        * **offline / 保密档（core/local_translator.py）：进程内 ``_memory``
          最优先，记忆查询整段不参与**——保密模式零落盘，``app.py`` 装配时
          注入的就是 ``None``，连读都不读。

        也正因为四档引擎本就不同（云端 / DeepL / 本地 NLLB / 本地大模型），
        "同一句话在四档得到同一份记忆答复"只在**记忆命中时**成立；未命中时
        各档译文本来就不一样，这不是本模块能承诺、也不该承诺的事。
        """
        source = (source or "").strip()
        if not source:
            return None
        exact = self.store.get_tm(source)
        if exact:
            self._hits += 1
            origin, confirmed = self._meta_for(source)
            return Match(target=exact, ratio=1.0, origin=origin, confirmed=confirmed)

        best: tuple[float, str, str, bool] | None = None
        source_tokens = _tokens(source)
        rows = self._load_full()
        for row in rows:
            candidate_source = str(row.get("source") or "")
            if not self._cheap_prefilter(source, candidate_source, source_tokens):
                continue
            ratio = _similarity(source, candidate_source)
            if best is None or ratio > best[0]:
                best = (
                    ratio,
                    str(row.get("target") or ""),
                    str(row.get("origin") or "auto"),
                    bool(row.get("confirmed")),
                )
        if best is not None and best[0] >= self.threshold:
            self._hits += 1
            return Match(
                target=best[1], ratio=best[0], origin=best[2], confirmed=best[3]
            )
        self._misses += 1
        return None

    def lookup(self, source: str) -> str | None:
        """只取译文的旧接口，委托 ``match``——既有调用方不用改。"""
        found = self.match(source)
        return found.target if found is not None else None

    @staticmethod
    def _cheap_prefilter(source: str, candidate: str, source_tokens: set[str]) -> bool:
        """长度差 + token 重叠的双重廉价预筛，挡掉绝大多数不可能候选。"""
        tolerance = max(_MIN_LENGTH_TOLERANCE, len(source) * _LENGTH_TOLERANCE)
        if abs(len(source) - len(candidate)) > tolerance:
            return False
        if not source_tokens:
            return True
        return bool(source_tokens & _tokens(candidate))

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict[str, int]:
        return {
            "entries": self.store.count_tm(),
            "hits": self._hits,
            "misses": self._misses,
        }

    def reset_stats(self) -> None:
        self._hits = 0
        self._misses = 0
