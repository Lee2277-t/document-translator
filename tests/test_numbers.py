"""数字归一化与数字核对回归。

交接稿第 5 节设计决策 6：拼写数字先转阿拉伯数字再翻译，否则小模型算不对。
number_check 的两个实测坑（千分位正则、逗号归一）也在这里钉死。
"""
from __future__ import annotations

from core.local_translator import normalize_cjk_numbers, normalize_numbers
from core.number_check import check_numbers, extract_numbers
from core.numwords import spelled_to_digits


def test_english_spelled_numbers_become_digits():
    assert normalize_numbers("three point two billion people") == "3.2 billion people"
    assert normalize_numbers("forty two") == "42"


def test_plain_english_sentence_untouched():
    text = "The board approved the plan."
    assert normalize_numbers(text) == text


def test_cjk_number_run_collapsed():
    """「1千2百万」折叠成「1200万」——汉字单位连写的老问题。"""
    assert normalize_cjk_numbers("1千2百万") == "1200万"


def test_extract_numbers_normalizes_thousands_separator():
    """必须整串吃掉千分位，写成 \\d{1,3} 会把 1500 截成 150。"""
    assert extract_numbers("We sold 1,500 units") == ["1500"]
    assert extract_numbers("Amount: 1500") == ["1500"]


def test_extract_numbers_handles_currency_and_percent():
    """货币前缀剥离，百分号跟数字一起留着（比对时两边都会带，不影响）。"""
    assert extract_numbers("$1,200.50 and 15%") == ["1200.50", "15%"]


def test_cjk_scale_is_expanded_so_equivalent_amounts_match():
    """3億5000万 与 3.5亿 是同一个数，不展开就是两条假疑点。"""
    assert extract_numbers("予算総額は3億5000万ユーロ") == ["350000000"]
    assert extract_numbers("总预算为3.5亿欧元") == ["350000000"]
    assert check_numbers("予算総額は3億5000万ユーロ", "总预算为3.5亿欧元") == []


def test_cjk_scale_matches_compact_form():
    """1千2百万 == 1200万。"""
    assert extract_numbers("1千2百万") == ["12000000"]
    assert extract_numbers("1200万") == ["12000000"]
    assert check_numbers("1千2百万", "1200万") == []


def test_percent_word_forms_match_the_sign():
    """40パーセント / 40 percent / 40% 都应该归一到同一形态。"""
    assert check_numbers("排出量を40パーセント削減", "将碳排放量减少40%") == []
    assert check_numbers("cut by 40 percent", "减少了 40%") == []


def test_fullwidth_digits_are_normalized_before_matching():
    assert extract_numbers("２０３０年まで") == ["2030"]


def test_structural_numbers_do_not_create_false_positives():
    """章节号与日期大多是个位数，已被「≥2 位」滤掉；日期写法两边不同也不该报。"""
    assert check_numbers("第3章を参照", "参见第 3 章") == []
    assert check_numbers("3月31日までに提出", "于 31 March 前提交") == []


def test_years_are_still_checked():
    """年份必须查——俄文年份翻错是已知问题，不能当成结构性噪音滤掉。"""
    issues = check_numbers("В отчёте за 2030 год", "报告中提到了 1903 年")
    assert any(issue.kind == "缺失" and issue.source == "2030" for issue in issues)


def test_real_number_mismatch_is_still_caught():
    """归一做对之后，真错的必须还能抓到。"""
    issues = check_numbers("売上は3億5000万ユーロ", "营收为 3.5 万欧元")
    assert any(issue.kind == "缺失" for issue in issues)


def test_european_grouping_is_merged():
    """欧陆写法 `3.000,00`（点分千位 + 逗号小数）必须归并成一个数。

    这里原先钉的是**缺陷现状**（会被切成 "3.000" 和 "00" 两个数，德/法文档因此
    必刷假疑点）。现已修复，断言反过来写成正确行为——保留这段历史是因为：
    如果哪天有人为了别的语种把这里的正则改宽，这个测试要能立刻跳出来拦住。

    同时要守住边界：单个点组 `1.234` 必须仍是小数，不能被当成千分位，
    因为英语文档里它是合法的小数写法。
    """
    assert extract_numbers("€3.000,00") == ["3000.00"]
    assert extract_numbers("人口 1.234.567 人") == ["1234567"]
    assert extract_numbers("Ratio is 1.234") == ["1.234"]


def test_spelled_numbers_are_normalized_before_comparison():
    """拼写数字要和阿拉伯数字比得起来，否则译文里凭空多出一个数。

    three million ↔ 300万 现在两边都算到 3000000，不再报「新增」。
    """
    assert check_numbers("About three million people", "约 300万 人") == []
    assert check_numbers("There are twenty three items", "共有 23 项") == []


def test_spelled_numbers_do_not_harm_ordinary_words():
    """数字词藏在普通单词里时不能乱码：Freund / under 里有 und / un。"""
    assert spelled_to_digits("Mein Freund kommt") == "Mein Freund kommt"
    assert spelled_to_digits("go under the bridge") == "go under the bridge"
    assert spelled_to_digits("one option left") == "one option left"


def test_compound_and_quirk_forms():
    """德语复合词与法语特殊写法。

    德语先个位再十位（einundzwanzig），法语 70/80 是历史上攒出来的怪写法，
    两者都不遵循通用加法规则，必须有专门处理。
    """
    assert extract_numbers("Es waren einundzwanzig Leute") == ["21"]
    assert extract_numbers("soixante-dix jours") == ["70"]
    assert extract_numbers("quatre-vingts personnes") == ["80"]
    assert extract_numbers("пятьдесят тысяч") == ["50000"]


def test_adjacent_numbers_are_not_glued():
    """相邻的两个数字不能被「空格千分位」规则粘成一个。

    这是拼写数字归一引出的真 bug：million 换成 1000000 后紧跟另一个数字，
    正好符合「空格 + 三位」的形态，被读成了 1500100。
    """
    assert extract_numbers("Revenue was 1,500 1000000") == ["1500", "1000000"]


def test_missing_number_is_reported():
    issues = check_numbers("Revenue was 1,500 million", "收入为一千五百")
    assert any(issue.kind == "缺失" and issue.source == "1500" for issue in issues)


def test_hallucinated_number_is_reported():
    issues = check_numbers("We have two sites", "我们有 3,400 个站点")
    assert any(issue.kind == "新增" and issue.source == "3400" for issue in issues)


def test_clean_translation_has_no_issues():
    assert check_numbers("Sales reached 1,500 units", "销量达到 1500 台") == []


def test_single_digit_noise_ignored():
    """个位数全是章节号噪音，不报。"""
    assert check_numbers("Step 3 of 9", "第 3 步，共 9 步") == []


def test_european_percent_words_are_normalized():
    """俄/德/西/法的「百分之」写法不归一，自检报告会成对刷假疑点。

    实测多语种样例一次刷了 6 条：原文 `40 процентов` 抽到 40、译文 `40%` 抽到
    40%，同一个数被当成两个数。
    """
    assert extract_numbers("на 40 процентов") == ["40%"]
    assert extract_numbers("um 40 Prozent") == ["40%"]
    assert extract_numbers("un 40 por ciento") == ["40%"]
    assert extract_numbers("de 40 pour cent") == ["40%"]
    assert extract_numbers("del 40 per cento") == ["40%"]
    assert check_numbers("снижение на 40 процентов", "下降40%") == []
    # 真错仍然要报
    assert check_numbers("снижение на 40 процентов", "下降45%")
