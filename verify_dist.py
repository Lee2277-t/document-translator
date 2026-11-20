"""封版闸门：对已经打好的免安装包做机器可复核的验收，不靠手点界面。

用法（在项目目录里）：
    .venv\\Scripts\\python.exe verify_dist.py                # 验当前版本号那个包
    .venv\\Scripts\\python.exe verify_dist.py --quick        # 跳过离线自检、试译与功能验收（都要加载模型，慢）
    .venv\\Scripts\\python.exe verify_dist.py 某包目录         # 验指定目录（例如旧版对照）

判据七条，缺一条都算这轮没封住：
① **包体闸门**：`exe` 在、带模型时 `models\\opus-mt-en-zh-ct2\\model.bin` 在、文件数不低于门槛、
   zip 在——v1.9.1 就是漏了这道闸门，把 1610 文件的空包送去验收，验收当场红了 9 项。
② **输出编码分流**（v1.9.0～v1.9.2 的正题）：管道里未设 `PYTHONIOENCODING` 或点的是 UTF 写法 →
   逐字节严格 UTF-8；点的是 `gbk` 等别的编码 → 仍是 GBK 字节。这一格**只在冻结包里测得出来**：
   v1.9.1 那份包里的解释器不吃这个 env、在这里给出过 GBK（《项目交接稿.md》§5 决策 70 的矩阵），
   v1.9.2 把弃权判据改成「看值」才修好——所以这六格是**红过一次**才长出来的，不能省。
③ **保密自检的内容判据**：`自检结果.txt` 里版本与模式对得上、组件「正常」够数且「失败」为零，
   而且「离线模型: 缺失」与「翻译失败:」一句都不许出现。后两条是本轮补的洞——原先的验收脚本
   只数 `^组件.*正常`，于是**缺模型的包能带着 PASS 发出去**。版本那条信的是**包目录名**里带的
   版本号（没带才退回仓库当前版本）：照文档「拿旧包做对照」时这一格不许假红，而「旧 exe 混进
   新目录名」照样红。
④ **zip↔目录往返一致**：发出去的压缩包与验收过的目录必须逐条目同名同 MD5（运行副产物跳过且计数）。
⑤ **离线试译真跑一遍**（v1.9.3 起）：①~④ 全绿只证明这包**能起**，不证明**能用**——自检里的
   「离线模型: 已找到」验的是文件在不在，不验模型能不能解出一段中文。这一项拿两句自造的西文
   占位句生成 .docx，让包里的 exe 真翻一遍，判据全落在**成品文件**上（原文逐条在、译文真的长出
   中文），不靠回显关键字。输入与成品都在临时目录，包目录一个字节都不新增。
⑥ **随包文本与仓库同一份**（第 7 格，v1.9.3 工具轮起）：④ 只证明「zip 与目录互为同一批文件」，
   证明不了「目录与仓库是同一份」——《保密模式说明.txt》改完忘了重打包（v1.9.1 真栽过）、
   《使用说明.txt》与五个启动器由 `build_dist.py` 现生成，仓库改了包里还是旧的，发出去的说明
   就与代码各说各话。这一格不比《术语表.txt》（用户自己会改，报了只会让闸门恒红）。
⑦ **功能行为验收**（第 8 格 a~i，v1.9.3 工具轮 Round 17 起）：①~⑥ 验的是「包完整、能起、能翻、
   说明对得上」，验不出三条**承诺级**行为——空表逐字等于内置、坏表只丢那一条并出声、保密模式不落
   缓存也不写应用日志。这一格在冻结包上跑七次转换（无表基线 / 四栏全空 / `›` 规则生效 / 同参无表 /
   坏 JSON / 坏正则 / 多字符标点），判据全落在成品文件与回显点名上；`--dump-segment-rules` 那两项
   排在最后（长出来的是非空模板，留在包里会把前面的基线带歪）。夹具与成品在临时目录，
   `TRANSLATOR_DATA_DIR` 另指临时子目录，封版包只临时多一份《分段规则.json》，跑完原样放回。
⑧ **逐行成品的自检报告**（第 9 格 a~c，Round 19 的正文承诺，Round 20 收进闸门）：
   ⑦ 只验文档。v1.9.5 把《自检报告》推到字幕、JSON 脚本与 T++ 表格，那四条仓库用例用的是假引擎，
   发出去的包里这条路径此前一格都没有。三种格式各真跑一次，判据是报告文件本身——长在成品旁、
   首行写着《本成品名》自检报告、带与文档同一份模板的说明行、回显念到；同时验成品结构没被接线
   弄坏（SRT 时间轴一字不动、JSON 元素数与译文键、xlsx 译文列）。图片前端不进这一格：OCR 结果会抖。
⑨ **PDF 批注搬运**（第 10 格，Round 28 起）：v1.9.12 承诺「左右对照与仅中文两种版式把批注带到
   成品上」，源树有 8 条用例，冻结包这一侧此前没有判据。夹具是一页自造 PDF（一句占位西文 +
   一条高亮 + 一条便签），让包里的 exe 真转一次 side，判据压在成品 PDF 上：两类批注都在、
   高亮热区不偏、便签内容带过来、回显报「2 条批注一并保留」。夹具与成品都在临时目录，
   `TRANSLATOR_DATA_DIR` 另指，包目录不新增文件。
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHARE = ROOT / "分发"
APP_NAME = "翻译工具"
TIMEOUT = 900

#: v1.9.2 实测：8 个组件全「正常」是封版包的底线，少于这个数就是有依赖没打进包
MIN_COMPONENTS_OK = 8
#: 冻结包应有 1623 个文件（v1.8.0～v1.9.2 四轮同数）；少一批就是有数据文件没带出去
MIN_PACKAGE_FILES = 1600
MODEL_MARK = Path("models") / "opus-mt-en-zh-ct2" / "model.bin"

#: 判「这一段是不是中文」用，与 app.py 里的 CJK 口径同一字面范围
CJK_RE = re.compile(r"[一-鿿]")

#: 功能验收用的自造占位正文（不含任何真实文档）。第二段整条是「› + 空格」的连排——
#: 只有把它填进「追加句末标点_需空白」才会改下刀位置，所以它是那张表的活体判据。
PLACEHOLDER_PLAIN = (
    "The operator shall record every measurement taken during the test run and report the "
    "result before the next cycle begins. Use a helper tool, e.g. this one, and note the "
    "value shown in item 1.2.3. Nothing here is a real document; it exists only to exercise "
    "the translator."
)
PLACEHOLDER_CHAIN = (
    "Alpha › Beta › Gamma › Delta › Epsilon › Zeta › Eta › Theta › Iota › Kappa › Lambda "
    "› Mu › Nu › Xi › Omicron › Pi › Rho › Sigma › Tau › Upsilon › Omega."
)
#: 《分段规则.json》的四栏名，与 `core/segment_rules.py` 同一套字面
RULE_KEYS = ("追加句末标点", "追加句末标点_需空白", "不切短语", "不切正则")
RULES_FILE_NAME = "分段规则.json"

#: 逐行成品（字幕 / JSON / T++ 表格）报告链路的夹具正文：两段自造西文，不含任何真实文档。
LINE_TEXTS = (
    "The operator shall record every measurement taken during the test run and report the result.",
    "Nothing here is a real document; it exists only to exercise the translator.",
)
#: 报告文件名里的标签，与 `core/report.py` 的 `REPORT_TAG` 同一字面（成品名 + 标签 + `.txt`）
REPORT_TAG = " 自检报告"
#: 报告模板的收尾行：三种逐行格式与文档必须共用同一份模板，这一句就是「同一份」的凭证
REPORT_FOOTER = "说明：本报告只标出"

#: 第 10 格（PDF 批注搬运）夹具：高亮盖住占位句、便签钉在句下，期望值与源树用例同一坐标
ANNOT_BOX = (72, 88, 260, 104)
ANNOT_NOTE = "闸门批注：请复核这一段"

#: 跑过一次闸门或自检就会在包目录里长出来的东西。它们**不是包体内容**（zip 是打包那一刻压的，
#: 里面没有），MD5 往返时必须跳过——但跳了几个要报出来，不然「少发一个真文件」会被这句话盖住。
RUNTIME_BYPRODUCTS = ("自检结果.txt", "分段规则.json")
RUNTIME_DIRS = ("logs", "cache")


def is_byproduct(rel: str) -> bool:
    """`rel` 是包内相对路径（正斜杠）：运行副产物返回真。"""
    return rel in RUNTIME_BYPRODUCTS or rel.split("/", 1)[0] in RUNTIME_DIRS

PASS: list[str] = []
FAIL: list[str] = []


def app_version() -> str:
    """从 app.py 读版本号，与 build_dist.py 同口径（两处各写各的必然对不上）。"""
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else "0.0.0"


def default_package() -> Path:
    return SHARE / f"{APP_NAME}-免安装版-v{app_version()}"


def classify(raw: bytes) -> str:
    """这批字节最可能是哪种编码：严格 UTF-8 解得动就算 UTF-8，解得动 GBK 就算 GBK。

    纯 ASCII 会落到 "utf-8" 那一格——所以判据必须配一条**含中文**的输出，否则两种编码的包
    都能通过（`--version` / `--help` 都带中文，正是为此选的）。
    """
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("gbk")
        return "gbk"
    except UnicodeDecodeError:
        return "other"


def normalize_codec(value: str | None) -> str:
    """取 `:` 前的编码名并归一（去空格、转小写、去掉 `-` 与 `_`）。"""
    head = (value or "").split(":")[0]
    return head.strip().lower().replace("-", "").replace("_", "")


def expected_encoding(env_value: str | None) -> str:
    """管道里这一格**应当**是什么编码：未设或点的是 UTF 系 → UTF-8；点的是别的编码 → 本机代码页。

    本机代码页在本机是 GBK，所以这条与 `app._env_wants_utf8()` 是同规则的**第二份实现**——
    两份必须对得上，用例正拿 `app._env_wants_utf8` 逐值核对，谁改了忘了另一份就红。
    """
    if env_value is None:
        return "utf-8"
    return "utf-8" if normalize_codec(env_value) in ("utf8", "u8", "cp65001") else "gbk"


def parse_selftest(text: str) -> dict:
    """把「自检结果.txt」拆成判据要的几项；不猜格式，缺字段就是 None / 0。"""
    version = (re.search(r"^版本:\s*(\S+)", text, re.M) or [None, None])[1]
    mode = (re.search(r"^模式:\s*(.+)$", text, re.M) or [None, None])[1]
    model = (re.search(r"^离线模型:\s*(\S+)", text, re.M) or [None, None])[1]
    glossary = (re.search(r"^术语表:\s*(\S+)", text, re.M) or [None, None])[1]
    return {
        "version": version,
        "mode": mode,
        "model": model,
        "glossary": glossary,
        "components_ok": len(re.findall(r"^组件.*正常", text, re.M)),
        "components_fail": len(re.findall(r"^组件.*失败", text, re.M)),
        "translate_failed": "翻译失败:" in text,
    }


def selftest_problems(info: dict, expect_models: bool, version: str) -> list[str]:
    """自检结果的**不合格清单**，空列表才算过。

    逐条列而不是一个 and 串：旧脚本只看 `^组件.*正常` 的条数，缺模型的包照样绿——现在
    「离线模型: 缺失」「翻译失败:」与组件失败各占一条，任何一条都能单独把包拦下。
    """
    problems = []
    if info["version"] != version:
        problems.append(f"版本对不上：{info['version']!r} ≠ {version!r}")
    if not info["mode"] or "保密" not in info["mode"]:
        problems.append(f"模式不是保密：{info['mode']!r}")
    if info["components_ok"] < MIN_COMPONENTS_OK:
        problems.append(f"组件正常只有 {info['components_ok']} 条（下限 {MIN_COMPONENTS_OK}）")
    if info["components_fail"]:
        problems.append(f"组件失败 {info['components_fail']} 条")
    if expect_models and info["model"] != "已找到":
        problems.append(f"离线模型：{info['model']!r}（保密版必须「已找到」）")
    if info["translate_failed"]:
        problems.append("自检里出现「翻译失败:」")
    return problems


def run_exe(pkg: Path, args: list[str], env_value: str | None = None,
            cwd: Path | None = None, env_extra: dict[str, str] | None = None) -> tuple[int, bytes]:
    """在包目录里跑 exe，输出走管道（`isatty()` 假）——被测的正是目的地分流。

    `cwd` 可以指到别处（试译与功能验收那两项要把输入与成品放在临时目录），但 `exe` 本体仍取自
    包目录。`env_extra` 用来另指 `TRANSLATOR_DATA_DIR`（把日志与缓存也搬出封版包）。
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
    if env_value is not None:
        env["PYTHONIOENCODING"] = env_value
    env.update(env_extra or {})
    child = subprocess.run([str(pkg / f"{APP_NAME}.exe")] + args, cwd=str(cwd or pkg),
                           capture_output=True, timeout=TIMEOUT, env=env)
    return child.returncode, child.stdout + child.stderr


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     {detail}"))


def gate_tree(pkg: Path, expect_models: bool, expect_files: int = MIN_PACKAGE_FILES) -> bool:
    files = [p for p in pkg.rglob("*") if p.is_file()]
    check(f"1 包目录有 exe 且文件数 ≥ {expect_files}", (pkg / f"{APP_NAME}.exe").is_file()
          and len(files) >= expect_files, f"实际 {len(files)} 个文件")
    if expect_models:
        check("1b 离线模型 model.bin 在包内", (pkg / MODEL_MARK).is_file(),
              f"{pkg / MODEL_MARK} 不存在——这个包不能当保密版发")
    return bool(files)


def gate_zip(pkg: Path, expect_models: bool) -> Path | None:
    suffix = "-含离线模型.zip" if expect_models else ".zip"
    zip_path = Path(str(pkg) + suffix)
    check(f"2 交付 zip 在（{zip_path.name}）", zip_path.is_file(), f"{zip_path} 不存在")
    return zip_path if zip_path.is_file() else None


def _zip_digests(zf: zipfile.ZipFile) -> dict[str, str]:
    """zip 里每个文件的 md5，**键去掉顶层目录前缀**。

    `build_dist.py --zip` 是整目录压的，条目名长成 `翻译工具-免安装版-v1.9.2/_internal/...`；
    直接拿它跟包内相对路径比，两边各 1623 条、交集 0 条，看着像「包全丢了」。
    """
    infos = {info.filename: info for info in zf.infolist() if not info.is_dir()}
    tops = {name.split("/", 1)[0] for name in infos}
    prefix = ""
    if len(tops) == 1 and all("/" in name for name in infos):
        prefix = next(iter(tops)) + "/"
    return {name[len(prefix):]: hashlib.md5(zf.read(infos[name])).hexdigest()
            for name in infos if not is_byproduct(name[len(prefix):])}


def _disk_digests(pkg: Path) -> dict[str, str]:
    return {path.relative_to(pkg).as_posix(): hashlib.md5(path.read_bytes()).hexdigest()
            for path in pkg.rglob("*")
            if path.is_file() and not is_byproduct(path.relative_to(pkg).as_posix())}


def md5_roundtrip(pkg: Path, zip_path: Path) -> None:
    """zip 与目录逐条目比对名字与 MD5：往返不一致就是「发出去的包 ≠ 验收过的包」。"""
    with zipfile.ZipFile(zip_path) as zf:
        in_zip = _zip_digests(zf)
    on_disk = _disk_digests(pkg)
    skipped = sum(1 for path in pkg.rglob("*")
                  if path.is_file() and is_byproduct(path.relative_to(pkg).as_posix()))
    only_zip = sorted(set(in_zip) - set(on_disk))
    only_disk = sorted(set(on_disk) - set(in_zip))
    differ = sorted(k for k in set(in_zip) & set(on_disk) if in_zip[k] != on_disk[k])
    check(f"3 zip 与目录逐条目一致（共 {len(set(in_zip) | set(on_disk))} 条，"
          f"跳过运行副产物 {skipped} 个）",
          not only_zip and not only_disk and not differ,
          f"只在 zip {len(only_zip)} 个：{only_zip[:3]}；只在目录 {len(only_disk)} 个：{only_disk[:3]}；"
          f"内容不一致 {len(differ)} 个：{differ[:3]}")


def gate_encoding(pkg: Path) -> None:
    """两条对照判据：UTF-8 那一格证明修复进了包，GBK 那一格证明没把用户意图抢反。

    一律带 `--offline`：编码分流与联网与否无关，而不带它就以联网模式起了程序，
    会往**封版包目录**里写 `logs\\app.log`——验收脚本不该把成品弄脏。
    """
    for tag, args in (("version", ["--offline", "--version"]),
                      ("help", ["--offline", "--help"])):
        for env_value in (None, "utf-8", "gbk"):
            expect = expected_encoding(env_value)
            code, raw = run_exe(pkg, args, env_value)
            got = classify(raw)
            label = "未设" if env_value is None else env_value
            check(f"4 管道里 PYTHONIOENCODING={label} → {expect}（{tag}）",
                  code == 0 and got == expect,
                  f"退码 {code}，实际解成 {got}，头 80 字节 {raw[:80]!r}")


def expect_selftest_version(pkg: Path) -> str:
    """《自检结果.txt》里**应该**出现的版本号：优先信包目录名带的那个，目录名没带才信仓库的当前版本。

    「拿旧包做对照」是文档里写死的用法（命令行参数直接给那个旧包目录），而这一格原先拿
    `app_version()`（仓库里 `app.py` 的 VERSION）当期望值——旧包的 exe 自报 v1.9.2、仓库已经走到
    v1.9.3，于是**照文档跑对照就必红一条**（本轮第一次全量跑旧包对照时实测到的）。信目录名反而更强：
    目录名叫 v1.9.3 而 exe 自报 v1.9.2（旧构建混进新包）照样红，判据一点没松。
    """
    found = re.search(r"v\d+\.\d+\.\d+", pkg.name)
    return found.group() if found else f"v{app_version()}"


def gate_selftest(pkg: Path, expect_models: bool) -> None:
    import tempfile

    result = pkg / "自检结果.txt"
    if result.exists():
        result.unlink()
    # `--selftest` 自己会加载离线模型，按天元日志跟着 `data_root()` 走；另指到临时目录，
    # 封版包才真的只剩「自检结果.txt」这一份运行副产物（`app.py` 把它写死在程序目录）。
    with tempfile.TemporaryDirectory(prefix="封版自检-") as tmp:
        code, raw = run_exe(pkg, ["--offline", "--selftest"],
                            env_extra={"TRANSLATOR_DATA_DIR": str(Path(tmp) / "data")})
    text = result.read_text(encoding="utf-8", errors="replace") if result.exists() else ""
    info = parse_selftest(text)
    problems = selftest_problems(info, expect_models, expect_selftest_version(pkg))
    detail = (f"退码 {code}，文件{'长出' if text else '没长出'}，解析={info}，"
              f"回显头 120 字节={raw[:120]!r}")
    check("5 保密自检的内容判据（版本/模式/组件够数且零失败/离线模型/无翻译失败）",
          code == 0 and bool(text) and not problems, detail + f"\n     不合格={problems}")


def doc_drift(repo: Path, pkg: Path, readme: str, launchers: dict[str, str]) -> list[str]:
    """随包文本与**仓库这一份**逐内容比对（行尾不算），返回漂移清单（空＝同一份）。

    管的是本项目栽过两次的那类坑：《保密模式说明.txt》改完没重打包（v1.9.1 误打一次）、
    `build_dist.py --models --zip` 少带参数打出一个 1610 文件的空包。第 3 项 MD5 往返只证明
    「zip 与目录互为同一批文件」，**证明不了「目录与仓库是同一份」**——发出去的说明与代码各说
    各话时，前六格能全绿。
    比的是这三类**程序写死**的文本：《保密模式说明.txt》（仓库里那份拷进去）、《使用说明.txt》
    与五个启动器 .bat（由 `build_dist.py` 的 `README` / `LAUNCHERS` 现生成）。
    **《术语表.txt》故意不比**：那是用户自己会改的文件，包里那份改了是常态，报出来只会让闸门
    变成「永远红」。**行尾不参与比对**：`build_dist.py` 用 `write_text` 写《使用说明.txt》，在
    Windows 上 `\n` 会被写成 `\r\n`，比原始字节会恒红——比的是内容逐字节，不是换行符。
    """
    problems = []

    def norm(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    def one(rel: str, want: str) -> None:
        target = pkg / rel
        if not target.is_file():
            problems.append(f"{rel} 不在包里")
        elif norm(target.read_bytes()) != norm(want.encode("utf-8")):
            problems.append(f"{rel} 与仓库不是同一份（改过没重打包？）")

    one("保密模式说明.txt", (repo / "保密模式说明.txt").read_text(encoding="utf-8"))
    one("使用说明.txt", readme)
    for name, body in launchers.items():
        one(name, body)
    return problems


def is_current_package(pkg: Path) -> bool:
    """包目录名里带不带当前版本号：不带就是「拿旧包做对照」，文本漂移那条本就该 SKIP。"""
    return f"v{app_version()}" in pkg.name


def gate_docs(pkg: Path) -> None:
    if not is_current_package(pkg):
        print(f"SKIP 7 随包文本比对（{pkg.name} 不是当前版本 v{app_version()} 的包，"
              "旧包与仓库本就该不一样）")
        return
    import build_dist

    problems = doc_drift(ROOT, pkg, build_dist.README, build_dist.LAUNCHERS)
    check("7 随包文本与仓库逐字节一致（保密说明/使用说明/启动器）", not problems,
          f"漂移={problems}")


def tidy_streams() -> None:
    """把闸门自己的输出流按 app.py v1.9.2 的口径理顺，**任何一步失败都只是不改**。

    Round 17 实跑踩到的：8c 的名字里带 `›`，`verify_dist.py > 日志` 重定向到文件时
    stdout 仍是本机 GBK，整条闸门在打印那一行时 `UnicodeEncodeError` 死掉——一个验收
    工具因为「怎么启动」而半路崩，等于结果不可信。口径抄被测方：显式要 UTF 就照办、
    重定向且没点名别的编码就换 UTF-8、其余保住用户点的那种编码但用 `backslashreplace`
    兜底（顶多打出一个 `›` 的字面转义，不许因此中断验收）。
    """
    import os

    explicit = os.environ.get("PYTHONIOENCODING") or ""
    wants_utf8 = explicit.lower().replace("-", "").startswith("utf")
    keeps_theirs = bool(explicit) and not wants_utf8
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if wants_utf8 or not (stream.isatty() or keeps_theirs):
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            else:
                stream.reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            pass


def decode_output(raw: bytes) -> str:
    """回显解码：先按 UTF-8，解不动再按 GBK——两种都留一手，是因为「包退回 GBK」正是本闸门要拦的
    缺陷（v1.9.1）。若只按 UTF-8 加 `errors=replace`，GBK 字节会先变成一串乱码，「翻译失败」这类
    关键字就再也查不到，那一条判据成了假绿。
    """
    for codec in ("utf-8", "gbk"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def trial_problems(paragraphs: list[str], source_texts: list[str], out_text: str) -> list[str]:
    """离线试译的不合格清单：判据只用在**成品文件**上，不靠回显关键字（回显会假绿）。

    三条硬证：原文段一条不少（逐条相等）、译文段真的长出中文、回显里没有失败与编码崩溃。
    """
    problems = []
    missing = [s for s in source_texts if s not in paragraphs]
    if missing:
        problems.append(f"成品里少了 {len(missing)} 条原文段：{missing[:1]}")
    translated = [p for p in paragraphs if p and CJK_RE.search(p) and p not in source_texts]
    if not translated:
        problems.append("成品里没有任何中文译文段")
    if "翻译失败" in out_text:
        problems.append("回显里有「翻译失败」")
    if "UnicodeEncodeError" in out_text:
        problems.append("回显里有 UnicodeEncodeError（编码降级没兜住）")
    return problems


def gate_trial(pkg: Path) -> None:
    """真翻一份占位文档：证明这个包**能用**，而不只是能起。

    输入与成品都放在临时目录里，包目录只出 exe、不落任何新文件（`--offline` 已保证不写缓存，
    这一项连译文都不写进包里）。`TRANSLATOR_DATA_DIR` 也另指到临时目录——离线转换仍会往
    `logs\翻译日志_<日期>.txt` 追加「本次启动 / 模型已就绪」两行（不含任何文档内容），
    搬走之后封版包在闸门里真的一个字节都不动。夹具是自造的两句西文说明，不含任何真实文档。
    """
    import tempfile

    from docx import Document

    source_texts = [
        "The operator shall record every measurement taken during the test run and report the "
        "result before the next cycle begins. Use a helper tool, e.g. this one, and note the "
        "value shown in item 1.2.3.",
        "Nothing here is a real document; it exists only to exercise the translator.",
    ]
    with tempfile.TemporaryDirectory(prefix="封版试译-") as tmp:
        work = Path(tmp)
        doc = Document()
        for text in source_texts:
            doc.add_paragraph(text)
        fixture = work / "占位文档.docx"
        doc.save(str(fixture))
        code, raw = run_exe(pkg, ["--offline", "--convert", fixture.name], cwd=work,
                            env_extra={"TRANSLATOR_DATA_DIR": str(work / "data")})
        out = decode_output(raw)
        products = sorted(work.glob("*_双语.docx"))
        if not products:
            check("6 离线试译：退 0 且临时目录里长出成品", False,
                  f"退码 {code}，临时目录={sorted(p.name for p in work.iterdir())}，尾={out[-400:]}")
            return
        paras = [p.text for p in Document(str(products[0])).paragraphs]
        problems = trial_problems(paras, source_texts, out)
        check("6 离线试译：退 0、成品长出、原文逐条在、译文含中文、无失败与编码崩溃",
              code == 0 and not problems,
              f"退码 {code}，成品={products[0].name}，段落={len(paras)}，不合格={problems}，尾={out[-300:]}")


def echo_problems(out_text: str, want: list[str], forbid: list[str]) -> list[str]:
    """回显判据：该念的一条不少、不该出现的一条不许出现，缺哪条或多哪条都点出来。

    `forbid` 里放 `\\u203a` 这种降级写法时，命中的正是「编码分流没生效」那一种回归；
    与第 5/6 项同口径，返回**不合格清单**而不是布尔。
    """
    problems = [f"回显里缺「{w}」" for w in want if w not in out_text]
    problems += [f"回显里冒出「{b}」" for b in forbid if b in out_text]
    return problems


def baseline_problems(paras: list[str], baseline: list[str]) -> list[str]:
    """成品与基线逐条比：一样返回**空清单**，不一样返回一句能直接读的差在哪。

    返回清单而不是「相等时返回空串」是刻意的：其余判据（`selftest_problems` / `trial_problems` /
    `doc_drift`）全是「不合格清单」这个形态，相等时是空表。写成返回字符串再塞进 `[diff]`，
    相等时会得到 `[""]`——那是**真值**，好包会被报成 FAIL（本轮第一次实跑就是这么露出来的）。
    """
    if paras == baseline:
        return []
    if len(paras) != len(baseline):
        return [f"段落数 {len(paras)} vs 基线 {len(baseline)}"]
    index = next(i for i, (a, b) in enumerate(zip(baseline, paras)) if a != b)
    return [f"第 {index} 段起不同：基线={baseline[index][:40]!r} vs 本次={paras[index][:40]!r}"]


def chain_translation(paras: list[str], chain: str) -> str:
    """双语成品里译文紧跟原文段之后：取那条连排段对应的译文段（找不到返回空串）。"""
    for i, text in enumerate(paras):
        if text == chain:
            return paras[i + 1] if i + 1 < len(paras) else ""
    return ""


def pick_product(work: Path, stem: str) -> Path | None:
    """取工作目录里**刚长出来的那份**双语成品。

    `--convert` 遇到同名成品不覆盖，往后加 `(2)` `(3)` 续号——所以既不能按固定文件名找
    （第二轮起永远读回第一轮那份），也不能按名字排序（`双语(9).docx` 会排在 `双语.docx`
    前面，因为 `(` 的码位比 `.` 小）。按修改时间取最新才是「这一轮跑出来的那一份」。
    """
    hits = sorted(work.glob(f"{stem}_双语*.docx"), key=lambda p: p.stat().st_mtime_ns)
    return hits[-1] if hits else None


def data_dir_problems(data: Path, forbidden: list[str]) -> list[str]:
    """保密落盘判据：`cache\\` 不许长出、`logs\\app.log` 要么不存在要么零字节、按天日志不含原文与路径。

    把 `TRANSLATOR_DATA_DIR` 另指到临时目录是这一项能进闸门的关键——冻结包里 `data_root()`
    默认就是 exe 同级目录，不搬走就得往**封版成品**里写日志。实测（10-29 探针）另指之后
    按天日志跟着搬、`cache\\` 与 `app.log` 两边都不长，包目录只剩运行副产物。
    「按天日志没长出」也算不合格：那条判据若没人写日志就恒真，等于没写。
    """
    problems = []
    if (data / "cache").exists():
        problems.append("生成了 cache\\")
    app_log = data / "logs" / "app.log"
    if app_log.exists() and app_log.stat().st_size:
        problems.append(f"logs\\app.log 有 {app_log.stat().st_size} 字节")
    log_dir = data / "logs"
    diaries = sorted(log_dir.glob("*翻译日志*")) if log_dir.exists() else []
    body = diaries[-1].read_text(encoding="utf-8", errors="replace") if diaries else ""
    if not body:
        problems.append("按天日志没长出（漏原文这条就成了空判）")
    problems += [f"按天日志里含 {item[:20]!r}" for item in forbidden if item in body]
    return problems


@contextlib.contextmanager
def package_rules(pkg: Path):
    """临时把包里的《分段规则.json》换成闸门要的那几张表，退出时**原样放回**。

    规则表读的是 `app_root()`（exe 同级），跟 `cwd` 无关——实测过：`cwd` 在临时目录、
    数据目录另指，包目录里那份表照样生效。包是封版成品，用户自己写的那份（如果存在）
    必须一字不变地还回去。
    """
    path = pkg / RULES_FILE_NAME
    original = path.read_bytes() if path.exists() else None
    try:
        yield path
    finally:
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(original)


def gate_behavior(pkg: Path) -> None:
    """功能验收：v1.8.0 那张表的四种口径 + 保密落盘，全在冻结包上真跑（Round 17 收编）。

    第 1~7 格验的是「包完整、能起、能翻、随包说明对得上」，验不出这三条**承诺级**行为：
    空表逐字等于内置、坏表只丢那一条并出声、保密模式不落缓存不写应用日志。这些判据原先
    只躺在 `D:\\tmp\\accept_v193.py` 里——一次性脚本，换人接手就等于没有。
    夹具三段（普通长句 / 「›」连排 / 普通长句）与成品都在临时目录，`TRANSLATOR_DATA_DIR`
    另指临时子目录，日志与缓存都不碰封版包。
    **顺序是定的**：模板那项（8h）必须最后跑——`--dump-segment-rules` 长出来的是非空模板，
    留在包里会把前面的基线全带歪。跑七次转换、每次都要加载离线模型，`--quick` 连着第 5/6 项一起跳。
    """
    import json
    import tempfile

    from docx import Document

    source_texts = [PLACEHOLDER_PLAIN, PLACEHOLDER_CHAIN, PLACEHOLDER_PLAIN]

    with tempfile.TemporaryDirectory(prefix="封版功能-") as tmp:
        work = Path(tmp)
        data = work / "data"
        doc = Document()
        for text in source_texts:
            doc.add_paragraph(text)
        doc.save(str(work / "占位文档.docx"))

        def convert(*extra: str) -> tuple[int, str, list[str] | None]:
            """跑一次保密转换：退码、回显（两种编码都认）、成品段落（没长出就是 None）。

            每次先清掉上一轮的成品与随附报告：不清的话 `--convert` 会往 `(2)` `(3)` 续号，
            七次跑完目录里躺着七份，「这一轮读到的那份」就成了不确定的东西（第一次实跑
            就是这么把 8b~8f 变成自己跟自己比的）。
            """
            for stale in work.glob("占位文档_双语*"):
                if stale.is_file():
                    stale.unlink()
            code, raw = run_exe(pkg, ["--offline", "--convert", "占位文档.docx", *extra],
                                cwd=work, env_extra={"TRANSLATOR_DATA_DIR": str(data)})
            text = decode_output(raw)
            product = pick_product(work, "占位文档")
            paras = [p.text for p in Document(str(product)).paragraphs] if product else None
            return code, text, paras

        with package_rules(pkg) as rules:
            # 8a 无表基线：后面三格拿它的成品逐条比，所以这一格必须先跑、且包里确实没有表
            rules.unlink(missing_ok=True)
            code, out, base = convert()
            problems = trial_problems(base or [], source_texts, out) if base else ["成品没长出"]
            problems += echo_problems(out, [], ["分段规则"])
            check("8a 无表基线：退 0、成品长出、原文都在、译文含中文、且不念「分段规则」",
                  code == 0 and not problems,
                  f"退码 {code}，段落={len(base) if base else 0}，不合格={problems}，尾={out[-300:]}")

            # 8b 四栏全空 = 逐字不改（v1.8.0 最要紧的承诺）
            rules.write_text(json.dumps({key: [] for key in RULE_KEYS},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            code, out, paras = convert()
            problems = (["成品没长出"] if not paras
                        else baseline_problems(paras, base or []) + echo_problems(out, [], ["分段规则"]))
            check("8b 四栏全空的表：退 0、成品与无表基线**逐条相等**、且不念「分段规则」",
                  code == 0 and bool(paras) and not problems,
                  f"退码 {code}，不合格={problems}，尾={out[-300:]}")

            # 8c/8d 规则真的生效：回显念到 + 成品里那条连排段的译文真的换了写法。
            #   8d 的判据**不盯段落数相等、也不盯「翻没翻」**——离线档的把握门限本身会抖，
            #   拿它当判据下一轮就假红；可复核的只有「两次都长出中文、而那条译文逐字不同」。
            rules.write_text(json.dumps({"追加句末标点_需空白": ["›"]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            code, out, paras = convert("--chunk-chars=60")
            problems = (["成品没长出"] if not paras
                        else trial_problems(paras, source_texts, out)
                        + echo_problems(out, ["分段规则：须跟空白的标点 1 个", "›", RULES_FILE_NAME],
                                        ["\\u203a", "UnicodeEncodeError"]))
            check("8c 追加句末标点_需空白=[›]：退 0、念「须跟空白的标点 1 个」+ 文件名、"
                  "`›` 原样在场、无降级写法无编码崩溃",
                  code == 0 and bool(paras) and not problems,
                  f"退码 {code}，不合格={problems}，尾={out[-400:]}")
            with_on_table = paras
            rules.unlink(missing_ok=True)
            code, out, paras = convert("--chunk-chars=60")
            trans_r = chain_translation(with_on_table or [], PLACEHOLDER_CHAIN)
            trans_n = chain_translation(paras or [], PLACEHOLDER_CHAIN)
            problems = []
            if code or not paras:
                problems.append(f"退码 {code} 或成品没长出")
            else:
                problems += trial_problems(paras, source_texts, out)
                if not (trans_r and trans_n):
                    problems.append(f"连排段的译文缺失：带表={trans_r!r} 无表={trans_n!r}")
                elif trans_r == trans_n:
                    problems.append("带表与无表的连排段译文一模一样 = 那张表没落地")
                if not (CJK_RE.search(trans_r or "") and CJK_RE.search(trans_n or "")):
                    problems.append("连排段那两次译文里至少有一次不是中文")
            check("8d 成品级硬证：同参下两次都翻出中文，但**那条连排段的译文逐字不同**"
                  "（带表按 › 分片再拼，无表整条一片）",
                  not problems,
                  f"带表={trans_r!r}\n无表={trans_n!r}\n不合格={problems}\n尾={out[-300:]}")

            # 8e/8f/8g 坏表三种写法：念出来、只丢那一条、成品仍等于基线
            rules.write_text("{这不是 JSON", encoding="utf-8")
            code, out, paras = convert()
            problems = (["成品没长出"] if not paras
                        else echo_problems(out, ["不是合法 JSON"], [])
                             + baseline_problems(paras, base or []))
            check("8e 规则表不是合法 JSON：退 0、念「不是合法 JSON」、成品与无表基线**逐条相等**",
                  code == 0 and bool(paras) and not problems,
                  f"退码 {code}，不合格={problems}，尾={out[-400:]}")

            rules.write_text(json.dumps({"不切正则": ["(?=(坏", r"(?:zzz)\.$"]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            code, out, paras = convert()
            problems = (["成品没长出"] if not paras
                        else echo_problems(out, ["不是合法正则", "不许下刀的规则 1 条"], [])
                             + baseline_problems(paras, base or []))
            check("8f 一条坏正则：退 0、念「不是合法正则」、另一条照常生效（「不许下刀的规则 1 条」）、"
                  "成品等于基线",
                  code == 0 and bool(paras) and not problems,
                  f"退码 {code}，不合格={problems}，尾={out[-400:]}")

            rules.write_text(json.dumps({"追加句末标点": ["。。。"], "不切短语": ["e.g."]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            code, out, paras = convert()
            problems = (["成品没长出"] if not paras
                        else echo_problems(out, ["不是单个字符", "不切短语", "不许下刀的规则 1 条"], []))
            check("8g 追加句末标点写成多字符：退 0、念「不是单个字符」并指到「不切短语」、"
                  "另一条照常生效",
                  code == 0 and bool(paras) and not problems,
                  f"退码 {code}，不合格={problems}，尾={out[-400:]}")

            # 8h 模板：真长出四栏、再跑一次不许覆盖（这是 v1.8.0 那条「绝不毁掉用户已有文件」）
            rules.unlink(missing_ok=True)
            code, raw = run_exe(pkg, ["--offline", "--dump-segment-rules"], cwd=work,
                                env_extra={"TRANSLATOR_DATA_DIR": str(data)})
            out = decode_output(raw)
            first = rules.read_bytes() if rules.exists() else b""
            keys = sorted(json.loads(first.decode("utf-8"))) if first else []
            code2, raw2 = run_exe(pkg, ["--offline", "--dump-segment-rules"], cwd=work,
                                  env_extra={"TRANSLATOR_DATA_DIR": str(data)})
            out2 = decode_output(raw2)
            second = rules.read_bytes() if rules.exists() else b""
            problems = echo_problems(out, ["已生成分段规则模板"], [])
            problems += echo_problems(out2, ["没有动它"], [])
            if not first:
                problems.append(f"{RULES_FILE_NAME} 没长出")
            elif set(keys) < {"_说明", *RULE_KEYS}:
                problems.append(f"模板栏位不齐：{keys}")
            elif "e.g." not in json.loads(first.decode("utf-8")).get("不切短语", []):
                problems.append("模板的「不切短语」里不带 e.g. 例子")
            if first != second:
                problems.append("第二次跑把模板改了（承诺是绝不覆盖）")
            check("8h --dump-segment-rules：退 0、真长出四栏模板（含 e.g. 例子），再跑一次念「没有动它」"
                  "且字节一字不变",
                  code == 0 and code2 == 0 and not problems,
                  f"退码 {code}/{code2}，不合格={problems}，尾={out2[-300:]}")

            # 8i 保密落盘：全程跑完之后才算
            problems = data_dir_problems(data, [PLACEHOLDER_PLAIN[:40], "record every measurement",
                                                str(work), str(pkg)])
            check("8i 保密模式全程：不生成 cache\\、logs\\app.log 不存在或零字节、"
                  "按天日志不含原文与完整路径",
                  not problems, f"数据目录={sorted(p.name for p in data.rglob('*'))}，不合格={problems}")


def report_beside(product: Path) -> Path:
    """成品旁边那份报告的路径。与 `core/report.py` 的 `_report_path` 同一算法。"""
    return product.with_name(product.stem + REPORT_TAG + ".txt")


def line_report_problems(product: Path, out_text: str) -> list[str]:
    """逐行成品的报告判据：报告长在成品旁、首行绑的就是这一份成品、模板同一份、回显念到。

    只断言「报告存在」是不够的——报告若错绑到别人的成品上、或退化成另一份模板，
    用户照样看不出来。首行是 `《成品名》自检报告`，据此把报告与这一份成品钉死；
    结尾那句说明是文档四格式与逐行四前端共用同一份模板的凭证。
    """
    report = report_beside(product)
    if not report.is_file():
        return [f"成品旁没长出《{report.name}》"]
    body = report.read_text(encoding="utf-8", errors="replace")
    problems = []
    first = body.splitlines()[0] if body else ""
    if first != f"《{product.name}》自检报告":
        problems.append(f"报告首行没绑到这一份成品：{first!r}")
    if not any(mark in body for mark in ("未发现需要复核的疑点", "建议逐条人工复核")):
        problems.append("报告里既没有结论行也没有疑点段")
    if REPORT_FOOTER not in body:
        problems.append("报告缺与文档同一份模板的说明行")
    problems += echo_problems(out_text, ["自检报告："], [])
    return problems


def pick_line_product(work: Path, stem: str, suffix: str) -> Path | None:
    """取工作目录里刚长出来的那份逐行成品（同 `pick_product`：按修改时间，不按文件名排序）。

    `stem_双语*.json` 会同时匹配上《… 译后编辑清单.json》与《… 自检报告.txt》这类辅助物，
    而辅助物总是**比成品后写**——按 mtime 取最新会稳定地取到清单，读出来是一串字符串而不是
    对象数组（第一次实跑就在 9b 上炸成 `'str' object has no attribute 'get'`）。所以先按名字筛掉。
    """
    hits = sorted(
        (path for path in work.glob(f"{stem}_双语*{suffix}")
         if REPORT_TAG not in path.name and "译后编辑清单" not in path.name),
        key=lambda p: p.stat().st_mtime_ns,
    )
    return hits[-1] if hits else None


def gate_linereports(pkg: Path) -> None:
    """逐行成品的自检报告（第 9 格 a~c，Round 19 的正文承诺在冻结包里真跑一遍）。

    第 8 格验的是文档（.docx）的行为；v1.9.5 把《自检报告》推到字幕、JSON 脚本与 T++ 表格，
    仓库里那四条用例用的是假引擎与假前端——发出去的包里这条路径此前**一格都没有**。
    这里三种格式各真跑一次，判据压在报告文件上（存在、首行绑到本成品、模板同一份、回显念到），
    同时验成品结构没被报告接线弄坏：SRT 时间轴一字不动且块数不变、JSON 每行都写上译文键、
    xlsx 多出译文列且每行有值。夹具与成品全在临时目录，`TRANSLATOR_DATA_DIR` 另指。
    图片前端不进这一格：它要 OCR，判据会跟着识别结果抖，得不偿失。
    """
    import json
    import tempfile

    with tempfile.TemporaryDirectory(prefix="封版逐行-") as tmp:
        root = Path(tmp)

        def convert(work: Path, name: str) -> tuple[int, str]:
            code, raw = run_exe(pkg, ["--offline", "--convert", name], cwd=work,
                                env_extra={"TRANSLATOR_DATA_DIR": str(work / "data")})
            return code, decode_output(raw)

        # 9a 字幕：块数、时间轴是播放器的硬约束，改动一个字就整锅拒载
        work = root / "srt"
        work.mkdir()
        timings = ("00:00:01,000 --> 00:00:03,000", "00:00:04,000 --> 00:00:06,500")
        (work / "占位字幕.srt").write_text(
            "".join(f"{number}\n{timing}\n{text}\n\n"
                    for number, (timing, text) in enumerate(zip(timings, LINE_TEXTS), start=1)),
            encoding="utf-8",
        )
        code, out = convert(work, "占位字幕.srt")
        product = pick_line_product(work, "占位字幕", ".srt")
        problems = [] if product else ["成品没长出"]
        if product:
            body = product.read_text(encoding="utf-8-sig")
            problems += line_report_problems(product, out)
            problems += [f"时间轴被改了：{timing!r} 不在成品里" for timing in timings
                         if timing not in body]
            if body.count("-->") != len(timings):
                problems.append(f"块数变了：时间轴 {body.count('-->')} 条，应为 {len(timings)}")
            if not CJK_RE.search(body):
                problems.append("成品里没有中文译文")
        check("9a 字幕：退 0、成品与报告并排、报告首行绑本成品、时间轴一字不动、成品含中文",
              code == 0 and not problems,
              f"退码 {code}，不合格={problems}，尾={out[-300:]}")

        # 9b JSON 脚本：行数即元素数，每一行都要写上译文键
        work = root / "json"
        work.mkdir()
        rows = [{"names": [speaker], "message": text}
                for speaker, text in zip(("A", "B"), LINE_TEXTS)]
        (work / "占位脚本.json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        code, out = convert(work, "占位脚本.json")
        product = pick_line_product(work, "占位脚本", ".json")
        problems = [] if product else ["成品没长出"]
        if product:
            data = json.loads(product.read_text(encoding="utf-8"))
            problems += line_report_problems(product, out)
            if len(data) != len(rows):
                problems.append(f"元素数变了：{len(data)} vs {len(rows)}")
            problems += [f"第 {index + 1} 行没写上译文" for index, row in enumerate(data)
                         if not str(row.get("译文") or "").strip()]
        check("9b JSON 脚本：退 0、报告与成品并排且绑本成品、元素数不变、每行都有译文",
              code == 0 and not problems,
              f"退码 {code}，不合格={problems}，尾={out[-300:]}")

        # 9c T++ 表格：表头留着，译文落在译文列、一行不落
        from openpyxl import Workbook, load_workbook

        work = root / "xlsx"
        work.mkdir()
        book = Workbook()
        sheet = book.active
        sheet.append(["说话人", "原文"])
        for speaker, text in zip(("A", "B"), LINE_TEXTS):
            sheet.append([speaker, text])
        book.save(work / "占位表格.xlsx")
        code, out = convert(work, "占位表格.xlsx")
        product = pick_line_product(work, "占位表格", ".xlsx")
        problems = [] if product else ["成品没长出"]
        if product:
            sheet = load_workbook(product).active
            header = [str(cell.value or "") for cell in sheet[1]]
            problems += line_report_problems(product, out)
            if sheet.max_row != 3:
                problems.append(f"行数变了：{sheet.max_row} 行，应为表头 + 2 行")
            if "译文" not in header:
                problems.append(f"表头里没有译文列：{header}")
            else:
                column = header.index("译文") + 1
                problems += [f"第 {row} 行译文列是空的"
                             for row in range(2, sheet.max_row + 1)
                             if not str(sheet.cell(row=row, column=column).value or "").strip()]
        check("9c T++ 表格：退 0、报告与成品并排且绑本成品、表头留译文列、两行都有译文",
              code == 0 and not problems,
              f"退码 {code}，不合格={problems}，尾={out[-300:]}")



def annot_problems(product: Path, out_text: str) -> list[str]:
    """第 10 格的判据：对照版成品 PDF 里批注是否原样带过来。

    只看落盘后的文件。Annot 是弱引用，读的时候必须持住 Page；文档一关就什么也取不到
    （源树用例踩过这条，九条批注因此被误判成全部跳过）。
    """
    import pymupdf

    problems: list[str] = []
    doc = pymupdf.open(product)
    try:
        page = doc[0]
        by_type: dict[str, pymupdf.Annot] = {}
        for annot in page.annots() or []:
            by_type[annot.type[1]] = annot
        highlight = by_type.get("Highlight")
        if highlight is None:
            problems.append("成品里没有 Highlight 批注")
        else:
            points = [pymupdf.Point(point) for point in (highlight.vertices or [])]
            xs = sorted({round(point.x, 1) for point in points})
            ys = sorted({round(point.y, 1) for point in points})
            want_x = [float(ANNOT_BOX[0]), float(ANNOT_BOX[2])]
            want_y = [float(ANNOT_BOX[1]), float(ANNOT_BOX[3])]
            if xs != want_x or ys != want_y:
                problems.append(f"高亮热区偏了：x={xs} y={ys}，期望 x={want_x} y={want_y}")
        note = by_type.get("Text")
        if note is None:
            problems.append("成品里没有 Text 便签")
        else:
            content = (note.info or {}).get("content")
            if content != ANNOT_NOTE:
                problems.append(f"便签内容没带过来：{content!r}")
    finally:
        doc.close()
    if "2 条批注一并保留" not in out_text:
        problems.append("回显没有报「2 条批注一并保留」")
    return problems


def gate_pdf_annots(pkg: Path) -> None:
    """第 10 格：冻结包里对照版真的把批注带到成品上（v1.9.12 的正文承诺）。

    夹具与成品都在临时目录，`TRANSLATOR_DATA_DIR` 另指，包目录不新增文件。
    """
    import tempfile

    import pymupdf

    with tempfile.TemporaryDirectory(prefix="封版批注-") as tmp:
        work = Path(tmp)
        fixture = work / "占位批注.pdf"
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text(pymupdf.Point(72, 100), LINE_TEXTS[0], fontsize=11)
        highlight = page.add_highlight_annot(pymupdf.Rect(*ANNOT_BOX))
        highlight.set_colors(stroke=(1, 1, 0))
        highlight.update()
        note = page.add_text_annot(pymupdf.Point(72, 140), ANNOT_NOTE)
        note.set_info(title="Gate")
        note.update()
        doc.save(fixture)
        doc.close()
        code, raw = run_exe(pkg, ["--offline", "--convert", fixture.name, "--layout=side"],
                            cwd=work, env_extra={"TRANSLATOR_DATA_DIR": str(work / "data")})
        out = decode_output(raw)
        products = sorted(work.glob("*_双语.pdf"))
        if not products:
            check("10 批注搬运：退 0 且临时目录里长出对照版 PDF", False,
                  f"退码 {code}，临时目录={sorted(p.name for p in work.iterdir())}，尾={out[-400:]}")
            return
        problems = annot_problems(products[0], out)
        check("10 批注搬运：高亮与便签都在、热区不偏、回显报 2 条",
              code == 0 and not problems,
              f"退码 {code}，成品={products[0].name}，不合格={problems}，尾={out[-300:]}")


def main(argv: list[str]) -> int:
    tidy_streams()
    quick = "--quick" in argv
    args = [a for a in argv if not a.startswith("--")]
    pkg = Path(args[0]).resolve() if args else default_package()
    if not pkg.is_dir():
        print(f"FAIL 0 包目录不存在：{pkg}")
        return 1
    expect_models = (pkg / MODEL_MARK).is_file()
    print(f"验收包：{pkg}（含离线模型={expect_models}，app 版本 v{app_version()}）")
    gate_tree(pkg, expect_models)
    zip_path = gate_zip(pkg, expect_models)
    if zip_path is not None:
        md5_roundtrip(pkg, zip_path)
    gate_docs(pkg)
    gate_encoding(pkg)
    if quick:
        print("SKIP 5/6/8/9/10 离线自检、试译、功能验收、逐行报告与批注搬运（--quick）"
              "——没验模型加载与真翻译，别当封版证据")
    else:
        gate_selftest(pkg, expect_models)
        if expect_models:
            gate_trial(pkg)
            gate_behavior(pkg)
            gate_linereports(pkg)
            gate_pdf_annots(pkg)
        else:
            print("SKIP 6/8/9/10 离线试译、功能验收、逐行报告与批注搬运（本包不含模型，转换必然失败）")
    print(f"\n合计：PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
