"""v1.5.6 免翻区（core/no_translate.py）的行为钉子。

这个模块的失败模式是"静默翻坏用户的东西"，所以测试的重点不是功能，而是
**收着的分寸**：

1. **保守默认**：``latex_inline`` / ``identifier`` 两条启发式规则不在默认
   规则里（写进 DEFAULT_RULES 就红）；普通美元金额（"$5 和 $10"）即使显式
   打开公式规则也不能被锁住；
2. **三前缀不撞车**：``Zxq``（术语）/ ``Ztq``（术语候选）/ ``Znt``（本模块）
   必须互不相同，restore 也不能碰别人的占位符；
3. **还原不盲删**：认不出的占位符原样留下（删了就再也找不回来）；
4. **roundtrip 无痕**：mask → restore 必须得到逐字符相同的原文；
5. **出声**：intro() 要说清锁了几处、都是什么；关掉时（off）什么都不做。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.glossary as glossary  # noqa: E402
import core.termbase as termbase  # noqa: E402
from core import no_translate as nt  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state():
    """每条用例前后把规则与累计计数复位——本模块有进程级状态。"""
    nt.reset_rules()
    nt.reset_counts()
    yield
    nt.reset_rules()
    nt.reset_counts()


# ---------------------------------------------------------------------------
# 一、基本掩码与还原
# ---------------------------------------------------------------------------


def test_mask_hides_url_and_restores_it_verbatim() -> None:
    text = "See https://example.com/docs?a=1&b=2 for details."
    masked, mapping = nt.mask(text)
    assert "https://" not in masked
    assert nt.restore(masked, mapping) == text


def test_mask_keeps_trailing_punctuation_outside_the_url() -> None:
    """句号属于句子不属于网址：被网址顺走就等于把这一段的句读抹掉了。"""
    masked, mapping = nt.mask("Visit www.baidu.com.")
    assert masked.endswith("Znt0Z.")
    assert mapping["Znt0Z"] == "www.baidu.com"


def test_mask_hides_email_version_numbering_and_code() -> None:
    cases = [
        ("Contact zhang.san@corp.example.com.", "zhang.san@corp.example.com"),
        ("Upgrade to v2.13.4 now.", "v2.13.4"),
        ("See section 3.2.1 please.", "3.2.1"),
        ("Run `pip install torch` first.", "`pip install torch`"),
        ("```\npython train.py\n```", "```\npython train.py\n```"),
    ]
    for text, piece in cases:
        masked, mapping = nt.mask(text)
        assert piece not in masked, text
        assert list(mapping.values()) == [piece]
        assert nt.restore(masked, mapping) == text


def test_mask_assigns_unique_placeholders_across_rules() -> None:
    """占位符编号跨规则连续递增——同一段里重名将互相覆盖映射。"""
    text = "See https://a.example.com and mail me@corp.com about v2.1."
    masked, mapping = nt.mask(text)
    assert len(set(mapping.keys())) == len(mapping) == 3
    assert nt.restore(masked, mapping) == text


def test_mask_plain_sentence_is_untouched() -> None:
    text = "Please review the attached agreement and sign it."
    masked, mapping = nt.mask(text)
    assert masked == text and mapping == {}


def test_mask_empty_text_returns_empty() -> None:
    assert nt.mask("") == ("", {})
    assert nt.mask(None) == (None, {})  # 防御：调用方可能塞进来 None


# ---------------------------------------------------------------------------
# 二、保守默认（这一组是主力钉子）
# ---------------------------------------------------------------------------


def test_default_rules_exclude_the_two_heuristics() -> None:
    """变异检验：把 latex_inline 加进免翻区默认规则 → 这条变红。"""
    assert nt.DEFAULT_RULES == ("url", "email", "version", "numbering", "code_fence")
    assert nt.OPTIONAL_RULES == ("latex_inline", "identifier")
    assert "latex_inline" not in nt.DEFAULT_RULES
    assert "latex_inline" not in nt.active_rules()
    assert "identifier" not in nt.active_rules()


def test_inline_formula_is_not_masked_by_default() -> None:
    """``$x^2$`` 用默认规则不该动——句内公式启发式默认关。"""
    text = "Let $x^2$ be the square of x."
    masked, mapping = nt.mask(text)
    assert masked == text
    assert mapping == {}


def test_dollar_amounts_survive_even_with_formula_rule_on() -> None:
    """即使显式打开公式规则，"花了 $5 到 $10"也不能被锁——这是保守默认的
    第二条：形状像公式、内容不像，二次判定必须把它放回正文。"""
    rules = tuple(nt.DEFAULT_RULES) + ("latex_inline",)
    text = "The cost went from $5 to $10 in total."
    masked, mapping = nt.mask(text, rules=rules)
    assert masked == text and mapping == {}


def test_inline_formula_is_masked_when_rule_enabled() -> None:
    rules = tuple(nt.DEFAULT_RULES) + ("latex_inline",)
    text = "Let $x^2$ be the square."
    masked, mapping = nt.mask(text, rules=rules)
    assert list(mapping.values()) == ["$x^2$"]
    assert nt.restore(masked, mapping) == text


def test_identifier_rule_is_off_by_default() -> None:
    text = "Call pandas.DataFrame.merge() with max_retries set."
    masked, mapping = nt.mask(text)
    assert masked == text
    masked, mapping = nt.mask(text, rules=tuple(nt.DEFAULT_RULES) + ("identifier",))
    assert "pandas.DataFrame.merge" not in masked
    assert nt.restore(masked, mapping) == text


def test_identifier_rule_does_not_swallow_prose() -> None:
    """英语正文（句首大写、人名）不能被标识符规则吃掉——这是它默认关的原因。"""
    rules = tuple(nt.DEFAULT_RULES) + ("identifier",)
    text = "Mr. Smith met John Smith yesterday."
    masked, mapping = nt.mask(text, rules=rules)
    assert masked == text and mapping == {}


# ---------------------------------------------------------------------------
# 三、三前缀不撞车
# ---------------------------------------------------------------------------


def test_three_placeholder_prefixes_are_distinct() -> None:
    """术语 Zxq / 术语候选 Ztq / 免翻区 Znt：三个道具在同一段里出现，前缀必须不同。

    变异检验：把免翻区前缀改成 Zxq → 这条变红。
    """
    assert nt.PLACEHOLDER.format(0) == "Znt0Z"
    assert glossary.PLACEHOLDER.format(0) == "Zxq0Z"
    assert termbase.TERM_PLACEHOLDER.format(0) == "Ztq0Z"
    assert len({nt.PLACEHOLDER.format(3), glossary.PLACEHOLDER.format(3), termbase.TERM_PLACEHOLDER.format(3)}) == 3


def test_restore_only_touches_its_own_placeholders() -> None:
    """别人的占位符一个都不许碰——误换等于把别人的映射炸掉。"""
    text = "Zxq0Z 与 Ztq1Z 与 Znt0Z"
    assert nt.restore(text, {"Znt0Z": "https://a.example.com"}) == (
        "Zxq0Z 与 Ztq1Z 与 https://a.example.com"
    )


def test_glossary_unlock_leaves_no_translate_placeholders_alone() -> None:
    """同一个道理的反方向：术语解锁也不能顺手把免翻区占位符换掉。

    （glossary.unlock 会顺手挤掉 CJK 之间的空格，那是它既有的润色行为，这里
     只断言两种占位符各归各家。）
    """
    text = "详情见 Zxq0Z 和 Znt0Z 两处。"
    unlocked = glossary.unlock(text, {"Zxq0Z": "银行"})
    assert "银行" in unlocked
    assert "Zxq" not in unlocked
    assert "Znt0Z" in unlocked, "免翻区占位符不该被术语解锁动过"


# ---------------------------------------------------------------------------
# 四、还原不盲删 / 组合 façade
# ---------------------------------------------------------------------------


def test_restore_leaves_unknown_placeholder_untouched() -> None:
    """模型把 Znt0Z 改写成 ZntOZ（或干脆多出一个）时必须原样留着。

    变异检验：把 ``restore`` 改成"删掉认不出的占位符" → 这条变红。
    """
    assert nt.restore("保留 Znt9Z 原样", {}) == "保留 Znt9Z 原样"
    assert nt.restore("模型改写了 ZntOZ", {"Znt0Z": "x"}) == "模型改写了 ZntOZ"
    assert nt.restore("文本", {}) == "文本"
    assert nt.restore("", {"Znt0Z": "x"}) == ""


def test_lock_and_mask_combines_term_and_no_translate_mappings() -> None:
    """组合入口：一进一出两个映射，调用方不用记顺序。"""
    text = "See https://a.example.com today."
    masked, mapping = nt.lock_and_mask(text)
    assert mapping[nt.LOCKS_KEY] and mapping[nt.TERMS_KEY] is not None
    restored = nt.unlock_and_restore(masked, mapping)
    assert restored == text


def test_masking_runs_before_term_locking(monkeypatch) -> None:
    """组合顺序是保命的：免翻区必须先掩，术语后锁。

    顺序反过来会发生什么（这是本条存在的理由，写在这里免得后来者"顺手
    换一下"）：术语被锁的意思是"换成用户指定的中文译名"，而 URL / 代码里
    的词不是自然语言词汇——``https://example.com/docs`` 里的 ``example``
    若先被换成中文，URL 就变成 ``https://Zxq0Z.com/docs``，还原时术语占位
    符先被换回中文、免翻区再换回"原文"，拿到的原文已经是坏的那个，链接
    就永久失效了。而正确顺序下 URL 整块进免翻区，术语根本咬不到它。

    变异检验：把 ``lock_and_mask`` 里的两步调换（先 ``glossary.lock`` 再
    ``mask``）→ 下面这几行断言立刻变红。**写这条之前那次变异是全绿的**
    （docstring 预言了灾难却没人守），所以这里刻意盯"URL 里不许出现术语
    占位符"这个中间态，而不只盯最终还原结果。
    """
    monkeypatch.setattr(
        glossary,
        "rules",
        lambda: [(nt.re.compile(r"\bexample\b", nt.re.I), "示例")],
    )
    text = "Open https://example.com/docs and read the example section."
    masked, mapping = nt.lock_and_mask(text)

    locks = mapping[nt.LOCKS_KEY]
    assert locks, "URL 应该整块进免翻区"
    # 关键中间态：免翻区里存的是**完好**的 URL，不含任何术语占位符
    assert all("example.com" in piece for piece in locks.values()), locks
    assert not any("Zxq" in piece for piece in locks.values()), locks
    # 正文里那个术语照常被锁（否则这条用例是空的）
    assert mapping[nt.TERMS_KEY], "正文里的术语仍应被锁"

    restored = nt.unlock_and_restore(masked, mapping)
    assert "https://example.com/docs" in restored, restored  # 链接完好
    assert "示例" in restored, restored  # 术语译名到位
    assert "Zxq" not in restored and "Znt" not in restored


def test_unlock_and_restore_handles_both_key_mappings() -> None:
    text = "详情见 Zxq0Z 与 Znt0Z。"
    mapping = {
        nt.TERMS_KEY: {"Zxq0Z": "银行"},
        nt.LOCKS_KEY: {"Znt0Z": "https://a.example.com"},
    }
    # glossary.unlock 会挤掉 CJK 之间的空格（既有的润色行为），所以只断言
    # 两类占位符各自回到了自己的位置
    restored = nt.unlock_and_restore(text, mapping)
    assert "银行" in restored
    assert "https://a.example.com" in restored
    assert "Zxq" not in restored and "Znt" not in restored


def test_unlock_and_restore_tolerates_missing_or_empty_mappings() -> None:
    """防御：老代码路径塞进来的也许只有一个空 dict 或 None。"""
    assert nt.unlock_and_restore("文本", None) == "文本"
    assert nt.unlock_and_restore("文本", {}) == "文本"
    assert nt.unlock_and_restore("", {"terms": {}, "locks": {}}) == ""


def test_lock_and_mask_passes_hits_through_to_glossary(monkeypatch) -> None:
    """术语命中的细粒度输出（报告用）不能被组合入口吃掉。"""
    monkeypatch.setattr(
        glossary,
        "rules",
        lambda: [(nt.re.compile(r"agreement", nt.re.I), "协议")],
    )
    hits: list[tuple[str, str, str]] = []
    masked, mapping = nt.lock_and_mask("sign the agreement now", hits)
    assert mapping[nt.TERMS_KEY], "术语应该被锁成占位符"
    assert hits and hits[0][1].lower() == "agreement"


# ---------------------------------------------------------------------------
# 五、开关解析与出声
# ---------------------------------------------------------------------------


def test_parse_rules_argument_variants() -> None:
    assert nt.parse_rules_argument("default") == nt.DEFAULT_RULES
    assert nt.parse_rules_argument("") == nt.DEFAULT_RULES
    assert nt.parse_rules_argument("off") == ()
    assert nt.parse_rules_argument("+latex,+ident") == nt.DEFAULT_RULES + nt.OPTIONAL_RULES
    assert nt.parse_rules_argument("latex") == nt.DEFAULT_RULES + ("latex_inline",)
    # 默认的(default) 顺序：先走默认规则，再按命令行写的顺序追加可选项
    assert nt.parse_rules_argument("ident，latex") == nt.DEFAULT_RULES + (
        "identifier",
        "latex_inline",
    )


def test_parse_rules_argument_rejects_unknown_rule() -> None:
    with pytest.raises(ValueError, match="no-translate 没有"):
        nt.parse_rules_argument("math")


def test_set_rules_off_disables_everything() -> None:
    nt.set_rules(())
    assert nt.active_rules() == ()
    assert nt.mask("https://a.example.com") == ("https://a.example.com", {})


def test_set_rules_none_restores_defaults() -> None:
    nt.set_rules(())
    nt.set_rules(None)
    assert nt.active_rules() == nt.DEFAULT_RULES


def test_intro_reports_what_was_locked() -> None:
    text = "See https://a.example.com and mail me@corp.com about v2.1."
    _masked, mapping = nt.mask(text)
    message = nt.intro(mapping)
    assert message is not None
    assert "本次锁住 3 处" in message
    assert "网址" in message and "邮箱" in message


def test_intro_is_silent_when_nothing_locked() -> None:
    assert nt.intro({}) is None


def test_intro_describes_the_run_totals_used_by_summary() -> None:
    """summary() 走的是累计口径：掩码逐段发生，收尾念一句才有人看。"""
    assert nt.intro() is None
    nt.mask("Visit https://a.example.com today.")
    message = nt.intro()
    assert message is not None and "本次锁住 1 处" in message
    assert "网址 1 处" in message
    nt.reset_counts()
    assert nt.intro() is None


# ---------------------------------------------------------------------------
# 六、四个档位的接线（掩码在 prepare、还原在 decode）
# ---------------------------------------------------------------------------


class _EchoChannel:
    """把收到的话原样加个前缀吐回来：占位符是否被改写一眼可见。"""

    name = "fake"
    label = "假通道"
    supports_ctx = False

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def translate(self, text: str) -> str:
        return f"译文：{text}"

    def translate_batch(self, texts: list[str]) -> list[str]:
        self.batches.append(list(texts))
        return [f"译文：{text}" for text in texts]


def _cloud_translator(tmp_path):
    from core.translator import Translator

    cloud = Translator(cache_path=str(tmp_path / "cache.sqlite"))
    cloud._preflighted = True
    cloud.bing_disabled = True
    cloud.edge = _EchoChannel()
    return cloud


def test_cloud_tier_masks_before_engine_and_restores_after(tmp_path) -> None:
    """云端的整条链路：URL 不出现在送给引擎的文本里，译文里原样回来。"""
    cloud = _cloud_translator(tmp_path)
    text = "Please read the guide at https://example.com/docs?a=1 before signing."
    results = cloud.translate_texts([text])
    sent = cloud.edge.batches[0][0]
    assert "https://example.com/docs?a=1" not in sent, "整个 URL 不该进引擎"
    assert "https://example.com/docs?a=1" in results[0], "译文里 URL 要原样回来"
    assert "译文：" in results[0]


def test_offline_tier_prepare_decode_restores_mask(tmp_path) -> None:
    """保密档与云端同口径：_prepare 掩、_decode 还原（同一utility 组合入口）。"""
    from core.local_translator import LocalTranslator

    offline = LocalTranslator.__new__(LocalTranslator)
    offline.style = __import__("core.style", fromlist=["STANDARD"]).STANDARD
    text = "Open https://example.com/docs and check v2.1."
    masked, _drop, mapping = offline._prepare(text, "en")
    assert "https://example.com/docs" not in masked
    restored = offline._decode(f"请打开 {list(mapping[nt.LOCKS_KEY])[0]} 查看。", False, mapping)
    assert "https://example.com/docs" in restored


def test_main_rejects_unknown_no_translate_rule(monkeypatch, capsys) -> None:
    """认不出的规则名必须当面报错——"写了却没生效"是最难排查的一类失效。"""
    import app

    monkeypatch.setattr(sys, "argv", ["app.py", "--no-translate=math"])
    with pytest.raises(SystemExit) as excinfo:
        app.main()
    assert excinfo.value.code == 4
    assert "no-translate 没有" in capsys.readouterr().out


def test_main_parses_no_translate_off(tmp_path, monkeypatch, capsys) -> None:
    """off 是能被验证的状态：规则表被清空（不是"写错了就默认放行"）。"""
    import app

    tmx_out = tmp_path / "x.tmx"
    monkeypatch.setattr(
        sys, "argv", ["app.py", "--no-translate=off", f"--tmx-export={tmx_out}", "--yes-i-know-tmx"]
    )
    # 导出会在解析开关之后立刻分发，用它确认 main() 走到了开关解析那一步
    try:
        app.main()
    except SystemExit:
        pass
    assert nt.active_rules() == ()
