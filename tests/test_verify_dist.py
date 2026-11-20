"""封版闸门 `verify_dist.py` 自己的用例：工具的判据不许是没人复核的绿灯。

本轮补它的动机是两条真红出来的缺口：① 旧验收脚本第 2 项只数 `^组件.*正常`，缺离线模型的包
能带着 PASS 发出去；② 这工具第一次写真包时就自己报了一条假红——zip 条目名带顶层目录前缀，
按包内相对路径比对两边永远不相交。两条都在这里钉住，改坏了当场红。
"""

import os
import zipfile

import pytest

import verify_dist as vd

#: 抄自 v1.9.2 封版包实跑的 分发\翻译工具-免安装版-v1.9.2\自检结果.txt（不手搓期望值）
REAL_SELFTEST = """版本: v1.9.2
模式: 保密（本机离线）
目录: ./分发\\翻译工具-免安装版-v1.9.2
术语表: 已找到
   └ 无条目
离线模型: 已找到
组件 pymupdf: 正常
组件 docx: 正常
组件 pptx: 正常
组件 openpyxl: 正常
组件 wordninja: 正常
组件 tkinterdnd2: 正常
组件 rapidocr_onnxruntime: 正常
组件 requests: 正常
试译: Chapter 1. The future of artificial intelligence in education.
正在加载本地翻译模型（首次约需数秒）…
本地模型已就绪，处理过程不会联网。
结果: 第 1 章。教育中人工智能的未来。
"""


def parse(text):
    return vd.parse_selftest(text)


def test_real_selftest_is_green():
    """实跑那份自检的每一项都被正确读出来，且**判据判它过**——期望值全部来自真文件。"""
    info = parse(REAL_SELFTEST)
    assert info == {
        "version": "v1.9.2",
        "mode": "保密（本机离线）",
        "model": "已找到",
        "glossary": "已找到",
        "components_ok": 8,
        "components_fail": 0,
        "translate_failed": False,
    }
    assert vd.selftest_problems(info, expect_models=True, version="v1.9.2") == []


@pytest.mark.parametrize(
    "text, expect_in_problem",
    [
        (REAL_SELFTEST.replace("离线模型: 已找到", "离线模型: 缺失"), "离线模型"),
        (REAL_SELFTEST.replace("组件 requests: 正常", "组件 requests: 失败（ImportError）"), "组件失败 1 条"),
        (REAL_SELFTEST + "翻译失败: RuntimeError: boom\n", "翻译失败"),
        (REAL_SELFTEST.replace("版本: v1.9.2", "版本: v1.9.1"), "版本对不上"),
        (REAL_SELFTEST.replace("模式: 保密（本机离线）", "模式: 联网"), "模式不是保密"),
        (REAL_SELFTEST.replace("组件 requests: 正常\n", ""), "组件正常只有 7 条"),
    ],
)
def test_each_blind_spot_now_blocks_the_package(text, expect_in_problem):
    """六条「以前会放过去」的包，现在每条都能被单独拦下。

    第一条正是旧验收第 2 项的假绿本体：组件 8 条一字没变、照样 PASS，缺的只是离线模型。
    """
    problems = vd.selftest_problems(parse(text), expect_models=True, version="v1.9.2")
    assert problems, "这条本该拦住"
    assert any(expect_in_problem in p for p in problems), problems


def test_modelless_package_is_not_blocked_by_the_model_line():
    """联网版包本来就不带模型：`expect_models=False` 时「缺失」不该拦，但「翻译失败」照拦。"""
    text = REAL_SELFTEST.replace("离线模型: 已找到", "离线模型: 缺失")
    assert vd.selftest_problems(parse(text), expect_models=False, version="v1.9.2") == []
    blocked = vd.selftest_problems(parse(text + "翻译失败: X: y\n"), expect_models=False,
                                   version="v1.9.2")
    assert any("翻译失败" in p for p in blocked), blocked


def test_offline_selftest_without_models_skips_trial_and_hides_install_path(tmp_path, monkeypatch):
    import app

    monkeypatch.setattr(app, "APP_DIR", tmp_path)
    monkeypatch.setattr(app, "OFFLINE", True)
    assert app._selftest() == 0

    report = (tmp_path / "自检结果.txt").read_text(encoding="utf-8")
    assert "离线模型: 缺失" in report
    assert "试译: 已跳过" in report
    assert str(tmp_path) not in report
    assert vd.selftest_problems(parse(report), expect_models=False, version=f"v{app.VERSION}") == []


@pytest.mark.parametrize(
    "value, want",
    [(None, "utf-8"), ("utf-8", "utf-8"), ("UTF8", "utf-8"), ("cp65001", "utf-8"),
     ("utf-8:replace", "utf-8"), ("  utf_8  ", "utf-8"),
     ("gbk", "gbk"), ("cp936", "gbk"), ("utf-16", "gbk"), ("big5", "gbk")],
)
def test_expected_encoding_agrees_with_the_app_rule(monkeypatch, value, want):
    """闸门里的「应当是什么编码」必须与 app 的弃权判据同口径，两份实现不许各说各话。

    未设 env 那一格没有对应的 `app._env_wants_utf8()` 语义（它只看值），所以只对**设了值**的
    格子做交叉核对：`app._env_wants_utf8()` 说用户要 UTF，闸门就应当期望 utf-8，反之亦然。
    """
    import app

    assert vd.expected_encoding(value) == want
    if value is not None:
        monkeypatch.setenv("PYTHONIOENCODING", value)
        assert app._env_wants_utf8() is (want == "utf-8")


@pytest.mark.parametrize("text, want", [
    ("结果: 第 1 章。教育中人工智能的未来。", "utf-8"),
    ("版本: v1.9.2 中文", "gbk"),
    ("ASCII only", "utf-8"),
    (b"\xff\xfe\x00\x00".hex(), "other"),
])
def test_classify_picks_the_encoding(text, want):
    """分类只看字节：解得动 UTF-8 就是 UTF-8，否则试 GBK，都不动就是 other。"""
    raw = text.encode("gbk") if want == "gbk" else (
        bytes.fromhex(text) if want == "other" else text.encode("utf-8"))
    assert vd.classify(raw) == want


def test_ascii_only_output_cannot_prove_utf8(tmp_path):
    """纯 ASCII 两种编码都解得动——所以判据必须靠带中文的输出，这条把原因钉在这里。"""
    assert vd.classify("plain ascii".encode("gbk")) == "utf-8"


def test_zip_prefix_is_stripped_before_comparing(tmp_path):
    """复刻真包的结构（zip 条目带顶层目录名）：判据必须认它是**一致**的。"""
    pkg = tmp_path / "翻译工具-免安装版-v1.9.2"
    (pkg / "_internal").mkdir(parents=True)
    (pkg / "翻译工具.exe").write_bytes(b"exe")
    (pkg / "_internal" / "a.dll").write_bytes(b"dll")
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for path in pkg.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(tmp_path).as_posix())  # 顶层目录名在条目里
    with zipfile.ZipFile(zip_path) as zf:
        assert vd._zip_digests(zf) == vd._disk_digests(pkg)


def test_missing_and_differing_entries_are_reported(tmp_path):
    """少文件与内容不同都得被列出来，不能只报「条目数不一样」。"""
    pkg = tmp_path / "pkg"
    (pkg / "logs").mkdir(parents=True)
    (pkg / "a.txt").write_bytes(b"1")
    (pkg / "b.txt").write_bytes(b"2")
    (pkg / "自检结果.txt").write_text("跑过闸门的痕迹", encoding="utf-8")
    (pkg / "logs" / "app.log").write_text("运行日志", encoding="utf-8")
    zip_path = tmp_path / "pkg.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("a.txt", b"9")  # 内容不同
        zf.writestr("c.txt", b"3")  # 只在 zip
    with zipfile.ZipFile(zip_path) as zf:
        in_zip = vd._zip_digests(zf)
    on_disk = vd._disk_digests(pkg)
    assert sorted(set(in_zip) - set(on_disk)) == ["c.txt"]
    assert sorted(set(on_disk) - set(in_zip)) == ["b.txt"]
    assert [k for k in set(in_zip) & set(on_disk) if in_zip[k] != on_disk[k]] == ["a.txt"]


def test_runtime_byproducts_are_skipped_on_both_sides(tmp_path):
    """跑过闸门会添 `自检结果.txt` 与 `logs\\`：往返比对跳过它们，但真少文件照样拦得住。

    这条是上面那个用例的配套——把两个副产物丢进同一棵树，`b.txt` 仍然是**唯一**报出来的缺口；
    要是哪天有人把忽略清单放大到目录级，这条就会红。
    """
    assert vd.is_byproduct("自检结果.txt")
    assert vd.is_byproduct("分段规则.json")
    assert vd.is_byproduct("logs/app.log")
    assert vd.is_byproduct("cache/translations.sqlite")
    for keep in ("术语表.txt", "logs_extra.txt", "models/opus-mt-en-zh-ct2/model.bin"):
        assert not vd.is_byproduct(keep), keep


def test_trial_problems_green_case_and_four_blind_spots():
    """试译烟测的判据只看成品文件：原文一条不少、译文真的长出中文、回显里没有失败。"""
    src = ["Alpha sentence with e.g. an abbreviation.", "Second source paragraph."]
    green = src + ["字母句（含 e.g. 缩写）。", "第二段译文。"]
    assert vd.trial_problems(green, src, "已生成 1 份成品") == []
    assert any("少了" in p for p in vd.trial_problems(src[:1], src, "")), "漏原文没被拦"
    assert any("没有任何中文" in p for p in vd.trial_problems(src, src, "")), "没译文没被拦"
    assert any("翻译失败" in p for p in vd.trial_problems(green, src, "翻译失败：引擎超时")), "回显失败没被拦"
    assert any("编码" in p for p in vd.trial_problems(green, src, "UnicodeEncodeError: boom")), "编码崩溃没被拦"


def test_decode_output_reads_both_sides_of_the_encoding_regression():
    """回显关键字判据要两种编码都认：包退回 GBK 时，「翻译失败」必须还能被查出来。

    这条钉的是 `errors="replace"` 那种写法的盲区——GBK 字节被替换成一串乱码，关键字永远查不到，
    那条判据假绿。期望值来自实际编码：先确认 GBK 字节确实解不成 UTF-8，否则这测试自欺欺人。
    """
    phrase = "失败：翻译失败 UnicodeEncodeError"
    assert vd.decode_output(phrase.encode("utf-8")) == phrase
    gbk = phrase.encode("gbk")
    try:
        gbk.decode("utf-8")
        raise AssertionError("GBK 字节竟然解得成 UTF-8——这条用例的前提没了，得重想")
    except UnicodeDecodeError:
        pass
    got = vd.decode_output(gbk)
    assert "翻译失败" in got and "UnicodeEncodeError" in got, repr(got)


def test_doc_drift_names_every_way_a_shipped_text_can_be_stale(tmp_path):
    """随包文本漂移：说明、README、启动器三类各漏一次都要点名，且《术语表.txt》不许报。

    第 3 项 MD5 往返只比「zip ↔ 目录」，比不出「目录 ↔ 仓库」，所以这三类漏重打包以前会全绿发出去。
    用例里的行尾差异是**故意造的**：`build_dist.py` 用 `write_text` 写《使用说明.txt》，Windows 会把
    `\\n` 写成 `\\r\\n`——只比行尾不算漂移，内容变了才算。
    """
    repo = tmp_path / "repo"
    pkg = tmp_path / "pkg"
    repo.mkdir()
    pkg.mkdir()
    readme = "标题\n第二行"
    launchers = {"打开-联网版.bat": "@echo off\r\nstart\r\n"}
    (repo / "保密模式说明.txt").write_text("保密口径\n", encoding="utf-8")
    (repo / "术语表.txt").write_text("源文,译文\n", encoding="utf-8")

    def ship():
        (pkg / "保密模式说明.txt").write_bytes((repo / "保密模式说明.txt").read_bytes())
        (pkg / "术语表.txt").write_bytes((repo / "术语表.txt").read_bytes())
        (pkg / "使用说明.txt").write_text(readme, encoding="utf-8")  # 行尾会被换成 CRLF
        for name, body in launchers.items():
            (pkg / name).write_bytes(body.encode("utf-8"))

    ship()
    assert vd.doc_drift(repo, pkg, readme, launchers) == []

    (pkg / "保密模式说明.txt").write_text("旧口径\n", encoding="utf-8")
    assert vd.doc_drift(repo, pkg, readme, launchers) == ["保密模式说明.txt 与仓库不是同一份（改过没重打包？）"]

    (pkg / "保密模式说明.txt").write_bytes((repo / "保密模式说明.txt").read_bytes())
    assert vd.doc_drift(repo, pkg, "标题\n第二行改了", launchers) == ["使用说明.txt 与仓库不是同一份（改过没重打包？）"]
    assert vd.doc_drift(repo, pkg, readme, {"打开-联网版.bat": "@echo off\r\nstart \"\" x\r\n"}) == \
        ["打开-联网版.bat 与仓库不是同一份（改过没重打包？）"]

    (pkg / "使用说明.txt").unlink()
    (pkg / "保密模式说明.txt").unlink()
    problems = vd.doc_drift(repo, pkg, readme, launchers)
    assert "保密模式说明.txt 不在包里" in problems and "使用说明.txt 不在包里" in problems

    (pkg / "术语表.txt").write_text("用户自己加的条目,译文\n", encoding="utf-8")
    (pkg / "使用说明.txt").write_text(readme, encoding="utf-8")
    (pkg / "保密模式说明.txt").write_bytes((repo / "保密模式说明.txt").read_bytes())
    assert vd.doc_drift(repo, pkg, readme, launchers) == [], "术语表被当成了漂移——那是用户会改的文件"


def test_is_current_package_only_matches_the_running_version(monkeypatch, tmp_path):
    """旧包对照时第 7 格必须 SKIP：旧包与仓库本就该不一样，报了就是恒红。"""
    monkeypatch.setattr(vd, "app_version", lambda: "9.9.9")
    assert vd.is_current_package(tmp_path / "翻译工具-免安装版-v9.9.9")
    assert not vd.is_current_package(tmp_path / "翻译工具-免安装版-v1.9.2")


def test_expect_selftest_version_trusts_the_package_dir_name(monkeypatch, tmp_path):
    """第 5 格的期望版本取自**包目录名**：照文档拿旧包做对照时不许假红，但拦「旧 exe 混进新包」那条没松。

    这条是全量跑一次旧包对照抓到的：`selftest_problems` 原先拿仓库 `app_version()` 当期望值，
    旧包的 exe 自报 v1.9.2、仓库已是 v1.9.3 → `不合格=["版本对不上…"]`，照文档用就必红。
    """
    monkeypatch.setattr(vd, "app_version", lambda: "9.9.9")
    assert vd.expect_selftest_version(tmp_path / "翻译工具-免安装版-v1.9.2") == "v1.9.2"
    assert vd.expect_selftest_version(tmp_path / "翻译工具-免安装版-v9.9.9") == "v9.9.9"
    assert vd.expect_selftest_version(tmp_path / "随手复制出来的一份") == "v9.9.9"


def test_default_package_follows_app_version(monkeypatch, tmp_path):
    """包目录名从 app.py 的 VERSION 推出来：改了版本忘了重打，闸门就去找新目录并当场报「不存在」。"""
    root = tmp_path / "repo"
    (root / "分发").mkdir(parents=True)
    (root / "app.py").write_text('VERSION = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(vd, "ROOT", root)
    monkeypatch.setattr(vd, "SHARE", root / "分发")
    assert vd.app_version() == "9.9.9"
    assert vd.default_package().name == "翻译工具-免安装版-v9.9.9"


def test_echo_problems_names_missing_and_forbidden_separately():
    """回显判据：缺的该点名、多出来的也该点名，两种毛病不能混成一句「回显不对」。"""
    assert vd.echo_problems("念了甲也念了乙", ["甲", "乙"], ["丙"]) == []
    assert vd.echo_problems("念了甲", ["甲", "乙"], []) == ["回显里缺「乙」"]
    degraded = "带表时念 › 与" + chr(92) + "u203a 两种写法"
    problems = vd.echo_problems(degraded, ["›"], [chr(92) + "u203a"])
    assert len(problems) == 1 and problems[0].startswith("回显里冒出")


def test_baseline_problems_is_empty_when_identical_and_names_the_paragraph_when_not():
    """空表/坏表那三格的判据是「成品与基线逐条相等」，相等时必须返回**空清单**。

    这一条是本轮第一次实跑抓出来的：写成「相等返回空串」的判据被塞进 `[diff]` 后成了 `[""]`，
    真值一个不差的好包当场报 FAIL。期望值抄自 2026-10-29 实跑输出——
    `第 1 段起不同：基线='b' vs 本次='x'`，参数顺序是 (本次, 基线)，写反了这句话会带错方向。
    """
    assert vd.baseline_problems(["a", "b", "c"], ["a", "b", "c"]) == []
    assert vd.baseline_problems(["a", "x", "c"], ["a", "b", "c"]) == \
        ["第 1 段起不同：基线='b' vs 本次='x'"]
    assert vd.baseline_problems(["a", "b"], ["a"]) == ["段落数 2 vs 基线 1"]


def test_chain_translation_reads_the_paragraph_right_after_the_source():
    """连排段那条译文只能按「译文紧跟原文段之后」取；取错一段就等于把判据挂在空串上。"""
    assert vd.chain_translation(["原文", "译文"], "原文") == "译文"
    assert vd.chain_translation(["原文"], "原文") == ""
    assert vd.chain_translation(["别的"], "原文") == ""


def test_pick_product_takes_the_newest_not_the_name_last(tmp_path):
    """成品续号 `(2)` `(3)` 后，「这一轮刚长出的那份」只能按 mtime 认，按名字排会读回旧的。

    这条是第一次实跑第 8 格时露出来的：七次转换全在比同一个文件，8d 报「带表与无表译文一模
    一样」纯是取错文件，不是规则没生效。期望值先跑了 `sorted()` 的真实顺序再抄，没手算。
    """
    old = tmp_path / "占位文档_双语.docx"
    new = tmp_path / "占位文档_双语(9).docx"
    old.write_bytes(b"first")
    new.write_bytes(b"second")
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(new, (1_700_000_060, 1_700_000_060))

    assert vd.pick_product(tmp_path, "占位文档") == new
    assert sorted(tmp_path.glob("占位文档_双语*.docx"))[-1] == old, "名字排序读回的是第一轮那份"
    assert vd.pick_product(tmp_path, "没跑过的文档") is None


def test_data_dir_problems_blocks_cache_app_log_and_leaks(tmp_path):
    """保密落盘：`cache\\` 不许有、`app.log` 有字节就不许过、按天日志里不许出现原文或路径。

    「按天日志没长出」也算不合格是刻意的：漏原文那条判据读的就是这份日志，日志不在时它恒真，
    等于把「不泄漏」这格变成没人验的绿灯。
    """
    data = tmp_path / "data"
    assert vd.data_dir_problems(data, ["秘密"]) == ["按天日志没长出（漏原文这条就成了空判）"]

    logs = data / "logs"
    logs.mkdir(parents=True)
    (logs / "app.log").write_text("有内容", encoding="utf-8")
    (data / "cache").mkdir()
    (logs / "翻译日志_2026-10-29.txt").write_text("今天译了 3 段，含秘密原文", encoding="utf-8")
    assert vd.data_dir_problems(data, ["秘密"]) == \
        ["生成了 cache\\", "logs\\app.log 有 9 字节", "按天日志里含 '秘密'"]

    (data / "cache").rmdir()
    (logs / "app.log").write_text("", encoding="utf-8")
    (logs / "翻译日志_2026-10-29.txt").write_text("共处理 3 段，未记录原文", encoding="utf-8")
    assert vd.data_dir_problems(data, ["秘密", str(data)]) == []


def test_package_rules_leaves_no_trace_when_absent_and_restores_bytes(tmp_path):
    """包是封版成品：闸门临时换的那几张规则表，跑完要么删干净、要么按原字节还回去。"""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    rules = pkg / vd.RULES_FILE_NAME

    with vd.package_rules(pkg) as path:
        assert path == rules
        path.write_text("闸门的表", encoding="utf-8")
    assert not rules.exists(), "包里没有的表，跑完还留着 = 弄脏封版包"

    rules.write_text("用户自己那份", encoding="utf-8")
    original = rules.read_bytes()
    with vd.package_rules(pkg) as path:
        path.write_text("闸门的表", encoding="utf-8")
        assert path.read_text(encoding="utf-8") == "闸门的表"
    assert rules.read_bytes() == original, "用户那份被换掉了"


def _run_gate_snippet(**env_extra):
    """在子进程里真把流理顺一次再打印 `›`+中文：判据只能拿实跑输出写。"""
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    env.update(env_extra)
    env.pop("PYTHONUTF8", None)
    snippet = "import verify_dist as vd\n" \
              "vd.tidy_streams()\n" \
              "print(chr(0x203a) + ' 分段规则')\n"
    child = subprocess.run([sys.executable, "-c", snippet], cwd=str(vd.ROOT),
                           env=env, capture_output=True, timeout=60)
    return child


def test_tidy_streams_survives_a_redirected_gbk_stdout():
    """`verify_dist.py > 日志` 时曾经死在第 8c 格：`›` 编不进 GBK，整条闸门半路崩。"""
    child = _run_gate_snippet()
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
    assert "› 分段规则" in child.stdout.decode("utf-8")


def test_tidy_streams_keeps_an_explicit_gbk_but_never_crashes():
    """用户点名 GBK 时不抢他的编码，但一个 `›` 也只能降级成字面转义，不许中断验收。"""
    child = _run_gate_snippet(PYTHONIOENCODING="gbk")
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace")
    text = child.stdout.decode("gbk")
    assert chr(92) + "u203a 分段规则" in text, text


def test_report_beside_matches_the_engine_side_tag(tmp_path):
    """报告名只能按「成品名 + 标签 + .txt」拼，与 `core/report.py` 的 `_report_path` 同一算法。"""
    product = tmp_path / "占位字幕_双语.srt"
    assert vd.report_beside(product).name == "占位字幕_双语 自检报告.txt"


def test_line_report_problems_requires_binding_footer_and_echo(tmp_path):
    """「报告存在」不算判据：错绑成品、退化成别的模板都照样骗过眼睛。三条各钉一次。"""
    product = tmp_path / "占位字幕_双语.srt"
    product.write_text("1\n", encoding="utf-8")
    report = vd.report_beside(product)
    echo = "自检报告：占位字幕_双语 自检报告.txt（0 条疑点）"

    assert vd.line_report_problems(product, "") == [f"成品旁没长出《{report.name}》"]

    report.write_text("《占位脚本_双语.json》自检报告\n\n未发现需要复核的疑点。\n\n"
                      "说明：本报告只标出「值得人眼看一遍」的位置，不代表译文有误。\n",
                      encoding="utf-8")
    assert vd.line_report_problems(product, echo) == \
        ["报告首行没绑到这一份成品：'《占位脚本_双语.json》自检报告'"]

    report.write_text("《占位字幕_双语.srt》自检报告\n\n只有标题没有结论。\n",
                      encoding="utf-8")
    assert vd.line_report_problems(product, echo) == \
        ["报告里既没有结论行也没有疑点段", "报告缺与文档同一份模板的说明行"]

    report.write_text("《占位字幕_双语.srt》自检报告\n\n未发现需要复核的疑点。\n\n"
                      "说明：本报告只标出「值得人眼看一遍」的位置，不代表译文有误。\n",
                      encoding="utf-8")
    assert vd.line_report_problems(product, "") == ["回显里缺「自检报告：」"]
    assert vd.line_report_problems(product, echo) == []


def test_pick_line_product_ignores_the_report_and_takes_the_newest(tmp_path):
    """续号后按 mtime 取最新；同名的辅助物不能被抓成「成品」。

    《译后编辑清单.json》与成品同后缀、且总比成品后写——第一次实跑第 9 格就是它被取到，
    读出来是一串字符串、9b 当场 `'str' object has no attribute 'get'`。这条把那次事故钉住。
    """
    product = tmp_path / "占位脚本_双语.json"
    listing = tmp_path / "占位脚本_双语 译后编辑清单.json"
    second = tmp_path / "占位脚本_双语(2).json"
    product.write_text('[{"译文": "第一轮"}]', encoding="utf-8")
    listing.write_text("[]", encoding="utf-8")
    second.write_text('[{"译文": "第二轮"}]', encoding="utf-8")
    os.utime(product, (1_700_000_000, 1_700_000_000))
    os.utime(second, (1_700_000_060, 1_700_000_060))
    os.utime(listing, (1_700_000_120, 1_700_000_120))
    vd.report_beside(second).write_text("报告", encoding="utf-8")

    assert vd.pick_line_product(tmp_path, "占位脚本", ".json") == second
    assert vd.pick_line_product(tmp_path, "没跑过的脚本", ".json") is None


GOOD_ANNOT_ECHO = "已生成对照 PDF：占位批注_双语.pdf；2 条批注一并保留"


def _annot_pdf(path, *, box=vd.ANNOT_BOX, note=vd.ANNOT_NOTE):
    """造一页带高亮与便签的占位 PDF；坐标与第 10 格夹具同一份常量。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(pymupdf.Point(72, 100), vd.LINE_TEXTS[0], fontsize=11)
    highlight = page.add_highlight_annot(pymupdf.Rect(*box))
    highlight.update()
    if note is not None:
        annot = page.add_text_annot(pymupdf.Point(72, 140), note)
        annot.update()
    doc.save(path)
    doc.close()
    return path


def test_annot_problems_accepts_carried_annots(tmp_path):
    """成品里两类批注都在、热区与内容对得上、回显报了条数 → 零不合格。"""
    product = _annot_pdf(tmp_path / "占位批注_双语.pdf")
    assert vd.annot_problems(product, GOOD_ANNOT_ECHO) == []


def test_annot_problems_names_shifted_highlight(tmp_path):
    """热区被挪走必须点名——批注搬过去但位置偏了，读者照样找不到原句。"""
    product = _annot_pdf(tmp_path / "偏移_双语.pdf", box=(80, 88, 260, 104))
    problems = vd.annot_problems(product, GOOD_ANNOT_ECHO)
    assert any("高亮热区偏了" in item for item in problems)


def test_annot_problems_reports_missing_annots_and_silent_echo(tmp_path):
    """一条批注都没有、回显也不提 → 缺高亮、缺便签、缺回显三条一起报。"""
    product = _annot_pdf(tmp_path / "有便签_双语.pdf")
    import pymupdf

    blank = tmp_path / "空白_双语.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text(pymupdf.Point(72, 100), vd.LINE_TEXTS[0], fontsize=11)
    doc.save(blank)
    doc.close()

    problems = vd.annot_problems(blank, "已生成对照 PDF")
    assert "成品里没有 Highlight 批注" in problems
    assert "成品里没有 Text 便签" in problems
    assert any("2 条批注一并保留" in item for item in problems)
    # 带便签但回显缺失：只报回显这一条，别把在的批注也误报成丢
    assert vd.annot_problems(product, "已生成对照 PDF") == ["回显没有报「2 条批注一并保留」"]


def test_annot_problems_names_changed_note_content(tmp_path):
    """便签在、但正文被换了 → 报内容不符，而不是「便签丢了」。"""
    product = _annot_pdf(tmp_path / "换字_双语.pdf", note="另一段备注")
    problems = vd.annot_problems(product, GOOD_ANNOT_ECHO)
    assert any("便签内容没带过来" in item for item in problems)
