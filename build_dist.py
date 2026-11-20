"""把工具打成「同事的电脑不用装 Python」的免安装交付包。

用法（在项目目录里）：
    .venv\\Scripts\\python.exe build_dist.py            # 联网版包（不含离线模型）
    .venv\\Scripts\\python.exe build_dist.py --models   # 再加离线模型，保密版全包
    .venv\\Scripts\\python.exe build_dist.py --zip      # 打包完顺便压成 zip
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "翻译工具"
SITE = Path(sys.executable).resolve().parent.parent / "Lib" / "site-packages"
DIST = ROOT / "build" / "pyi"
SHARE = ROOT / "分发"
MODELS = ROOT / "models"


def _app_version() -> str:
    """从 app.py 读版本号，避免两处各写各的然后对不上。"""
    import re

    text = (ROOT / "app.py").read_text(encoding="utf-8")
    match = re.search(r'^VERSION\s*=\s*"([^"]+)"', text, re.M)
    return match.group(1) if match else "0.0.0"


#: 包名带版本号：分发时对方一眼能看出是哪版，也不会因为重名去删旧目录
#: （删旧包是不可逆操作，构建脚本不该替用户做这个决定）。
PACKAGE = SHARE / f"{APP_NAME}-免安装版-v{_app_version()}"

# 界面与样例都放在包根目录，用户看得见、改得了（术语表也在这里）
DATA_FILES = (
    "术语表.txt", "保密模式说明.txt", "fetch_multilingual_model.py",
    "LICENSE", "THIRD_PARTY_NOTICES.md",
)
SAMPLE_FILES = ("示例-英文报告.docx", "示例-文字版.pdf", "示例-扫描版.pdf")

# 只写 ASCII，中文交给 chcp 65001：.bat 用 GBK 存会在别的代码页机器上找不到 exe
LAUNCHERS = {
    "打开-联网版.bat": (
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "start \"\" \"翻译工具.exe\"\r\n"
    ),
    "打开-保密模式.bat": (
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "if not exist \"models\\opus-mt-en-zh-ct2\\model.bin\" (\r\n"
        "    echo [!] models folder missing. Copy the whole models folder next to this file.\r\n"
        "    pause\r\n    exit /b 1\r\n)\r\n"
        "start \"\" \"翻译工具.exe\" --offline\r\n"
    ),
    "批量翻译-把文件拖到这里.bat": (
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "if \"%~1\"==\"\" (\r\n    echo [!] Drop Word or PDF files onto this icon.\r\n"
        "    pause\r\n    exit /b 1\r\n)\r\n"
        "\"翻译工具.exe\" --convert %*\r\n"
        "if exist \"转换日志.txt\" type \"转换日志.txt\"\r\n"
        "pause\r\n"
    ),
    "批量翻译-保密模式-拖到这里.bat": (
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "if \"%~1\"==\"\" (\r\n    echo [!] Drop Word or PDF files onto this icon.\r\n"
        "    pause\r\n    exit /b 1\r\n)\r\n"
        "if not exist \"models\\opus-mt-en-zh-ct2\\model.bin\" (\r\n"
        "    echo [!] models folder missing. Copy the whole models folder next to this file.\r\n"
        "    pause\r\n    exit /b 1\r\n)\r\n"
        "\"翻译工具.exe\" --offline --convert %*\r\n"
        "pause\r\n"
    ),
    "自检.bat": (
        "@echo off\r\nchcp 65001 >nul\r\ncd /d \"%~dp0\"\r\n"
        "\"翻译工具.exe\" --selftest\r\n"
        "if exist \"自检结果.txt\" type \"自检结果.txt\"\r\n"
        "pause\r\n"
    ),
}

README = """文档翻译工具 · 免安装版

怎么用（三种都行，选一种顺手的）
1. 双击「打开-联网版.bat」（或直接双击 翻译工具.exe），把文件拖进窗口，点开始翻译。
   支持 Word / PDF / PPT / EPUB 文档、字幕与 Galgame 脚本（.srt / .vtt / .json / .xlsx）、
   图片（.png / .jpg / .bmp / .webp，OCR 识别后把中文叠回原位置）。
2. 一次要翻好几个文件：把文件（或整个文件夹）拖到「批量翻译-把文件拖到这里.bat」图标上，
   成品就出现在原文件旁边，名字带「_双语」。涉密文件请改用
   「批量翻译-保密模式-拖到这里.bat」（需要 models 文件夹，见下）。
3. 涉密文件绝对不要走联网版：双击「打开-保密模式.bat」，它只用这台电脑上的离线模型，
   翻译不联网、不存缓存。按天记录运行元信息日志，译文和复核文件保存在原文件旁。

翻出来的成品长什么样：原文一句不动，中文紧跟在后面（Word/PPT/EPUB）；PDF 默认是左边原文、
右边中文的对照版，也可以选「仅中文」（页面尺寸与原件一致，方便打印）或「原位重排」
（在原件上把原文换成中文）；字幕与脚本按原格式写回译文；图片把中文叠在原位置。
PDF 里的可点链接（网址、跳到第几页）与批注（高亮、便签、方框、手绘、图章等）都会带到成品上，
热区与批注仍压在原来那段文字上；表单控件与自定义图章只在「原位重排」里留得住，
对照版与仅中文版会跳过并在日志里报条数。
中英混排的段落里夹着的英文子句（4 个词以上）会单独翻出来再拼回去，不会整句留在原文。
Word 的脚注/尾注、批注、文本框里的外文也会翻；带修订标记的文档只翻「插入」的文字，
「删除」的文字不译。
EPUB 按章节原文一段、译文一段；表格与列表的译文接在原单元格/原条目内部，
不会把你的表格撑出一列、也不会多出列表项；书名与目录导航保持原样。

只想要其中一部分（v1.6.8 起 PDF，v1.7.0 起 PPT 与 EPUB）：界面「PDF 成品」那一行右边的
「范围」框里写 1-3,5 就是第 1、2、3、5 项；-3 是前 3 项，8- 是第 8 项到最后一项，留空是整份文档；
序号从 1 数、与阅读器或 PPT 页脚显示的一致，全角逗号、顿号、波浪线都认。命令行对应 --pages=1-3,5。
同一个数字在三种文件里数的是不同的东西：PDF 数页、PPT 数幻灯片张、EPUB 数正文章（按目录顺序），
上界各按自己那份文件的总数校验，报错也跟着按「页/张/章」说。
裁法语义有两种，日志都会当场说清：PDF 的「左右对照」「仅中文」是重新拼页，成品里就只有选中的
这些页；PDF「原位重排」、PPT、EPUB 都不重建容器——未选中的原样留在成品里，成品页数/张数/章数
不变，只是其中若干不翻（EPUB 没选中的章连字节都不动）。写错范围当场报错，不会闷头把整份翻完；
超出数量直接说「这份 PDF 只有 N 页」「这份 PPT 只有 N 张」「这份 EPUB 只有 N 章」，不做静默夹取。
Word、Excel、字幕、图片没有可挑的序号界，填了范围也是整份翻。
重新拼页那两种版式里书签跟着裁：指向未选中页面的书签从成品目录里舍弃（成品里没有对应页可跳），
并报告舍弃了几条；「原位重排」不重建页面，书签按原件留着。

翻完想调某一小段：成品旁边有一份《xx 译后编辑清单.json》，点主界面的「译后编辑」按钮
就能逐段看原文、改译文、写回成品——只改这段文字，格式不动，源文件不碰。
想多要一个说法：选中某段点「要备选译文」，让引擎再摇一条；双击备选即填进译文框，
仍要点「保存写回」才会写进成品（本地大模型档才会给出不同说法，离线小模型与免费云端
每次结果相同，程序会直接说「给不出不同说法」，不糊弄）。

方向（v1.5.8 起支持中译英）：界面「目标语言」默认「中文」，选「英文」就做中译英，
成品变成「原文 + 英文」对照，已经是英文的段落自动跳过；命令行对应 --to=en。
术语表.txt 不用改写法，中译英时程序会拿等号右边的中文去匹配原文、把左边外文锁进译文。
注意：保密模式下中译英必须装了多语种模型（models\\nllb-200-zh-ct2），英文小模型只做英译中。

翻译引擎档位（联网版界面里选）
- 免费云端：默认，无需安装任何东西；
- 本地大模型：用本机 Ollama（如 qwen2.5:7b）结合文档上下文翻译。
  在 Ollama 中安装并使用本地模型。服务不可用时仅回退本机离线小模型，缺少模型时提示安装。
  这一档自带「质量引擎」：术语一致性校验、上下文连贯、发现问题自动校改，
  自检报告里按 Critical / Major / Minor 分级列出待人工复核的位置。
- 单位自托管服务：界面点「配置自托管…」填单位自己的翻译服务地址（OpenAI 兼容接口，
  如 vLLM、llama-server、另一台机器上的 Ollama），模型名会自动从服务读取。
  地址写本机或内网 IP 时，译文请求只在这台机器或单位内网里跑，服务连不上时也只改走
  本机离线小模型，不会把原文发到公网；地址写成公网域名时这一档按云端对待（第一次用
  要确认一次），服务连不上时回退免费云端。涉密文件请一律用保密模式，不要选这一档。

批量与重试参数（v1.6.9，只在命令行调，界面上没有这些输入框）
- --batch-items=10 一次送多少段，--batch-chars=2000 一次送多少字，
  --chunk-chars=800 单段最长多少字，--retry-delays=0.5,1,2 遇到限流后逐次等多久（最多 6 档）。
- 只能调小不能调大：每一档的上限是它自己的硬限（免费云端 Edge 40 段 / 6000 字、微软 10 段 / 820 字、
  备用 450 字；付费 DeepL 50 段；保密模式的本机模型 24 段 / 6000 字 / 1200 字）。
  写大了会夹回这一档的上限，并把「你写了几、上限是几、现在按几」在日志里念出来，不会闷声改掉。
- 机器内存小、或者被限流得太狠时才需要动这些。小内存机器把 --batch-items= 写小（如 8）、
  --chunk-chars= 写小（如 600）能压低内存峰值，代价是慢一些。
- 保密模式下 --retry-delays= 这一档用不上（跑的是本机模型，没有网络请求就没有退避可退），
  程序会明说「这一档用不上」；请求尺寸那三个开关在保密模式是生效的，改它们不改变「内容不出本机」。

翻译记忆门槛（v1.9.10，同样只在命令行调）
- --tm-threshold=0.95 规定「相近但不完全一致」的旧译文要像到什么程度才复用，默认 0.92。
  只能往高调：1.0 表示只认完全一致（不启用近似复用）；写低于 0.92 会被夹回 0.92 并念出来，
  因为再像得少一点的句子往往意思已经变了，用错比不用更糟。近似复用过的段落会在成品旁
  《自检报告》里逐条列出（带相似度），方便对照。保密模式不接翻译记忆，这一项对它无意义。

断句规则（v1.8.0，一份可编辑的《分段规则.json》，界面上同样没有输入框）
- 什么时候算「一句话说完了」，本来写死在程序里。两种真实文档会踩穿它：
  缩写后面照样是空格（e.g. this one、No. 3）被劈成两句；编号 1.2.3. 被当成句子结尾，
  离线小模型会把「1.2.3 系统概述」译成「系统 1.2.3」。
- 生成可编辑模板：翻译工具.exe --dump-segment-rules（工具目录下已存在时绝不覆盖你的）。
  临时指定另一份：--segment-rules=./某处/分段规则.json。四种栏位：
  「追加句末标点」单个字符、紧跟其后即切（中文式，不要求后面有空格）；
  「追加句末标点_需空白」同上但须跟空白（西文式）；
  「不切短语」候选切点前面以它结尾就不切（按字面匹配）；「不切正则」自己写正则，命中即不切。
- 没有这份文件、或者四栏都空 = 与以前的切句行为逐字一致，成品不会变。
- 表写坏了不拦翻译：认不出的写法（JSON 语法错、正则写错、标点不是单字符、条数超了）
  逐条在日志里念出来并跳过那一条，整份读不了就退回内置规则。
- 里面只有标点和正则，不含任何文档内容，保密模式照读；这份文件不进 git、也不随分享包外发。
- 命令行输出的编码按「有没有连着控制台」分两条路走（v1.9.0）：在 cmd 里眼看的那条保持本机
  代码页，GBK 装不下的字符（如 ›、≈）那一行会显示成 \\u203a 这样的写法——只是显示降级，
  成品照常生成、退码照常是 0，中文照常显示，编码本身没动；把输出重定向进文件、或交给另一个
  程序读时整条换成 UTF-8，脚本按 UTF-8 读就能拿到 › 原样，不用再猜这次是 GBK 还是 UTF-8。
  自己设过 PYTHONIOENCODING 的以你设的为准，程序不改你的编码，只把编不动的那个字符降级。

常见问题的自查
- 双击没反应 / 提示缺少 DLL：先跑「自检.bat」。提交问题时，只附上去除路径与隐私信息后的相关错误行。
- 某段翻得不对：用记事本打开「术语表.txt」，加一行「英文原文 = 你要的中文」，保存后重新翻译即可
  （联网版和保密模式都读这个文件）。
- 提示连不上翻译服务：本机网络屏蔽了免费通道，稍后再试，或改用保密模式。
- 查看术语库状态（含未决术语池）：命令行运行 翻译工具.exe --terms。
- 想知道命令行都能开什么：运行 翻译工具.exe --help（v1.9.1），一屏按九组列全每个开关干什么、
  默认是什么、哪一档用不上；-h 是同一个写法。这一屏不落盘、也不含任何文档内容。
- 命令行开关写了却「什么都没发生」：v1.6.7 起拼错的开关会直接报「未识别的参数」并退出，
  同时列出全部可用开关和最接近的写法。以前不认识的写法会被当成文件路径忽略，
  --offlin（少一个 e）更会以联网模式起界面——现在这种写法一律硬失败，不会静默放行。

保密模式还要求 models 文件夹
公开版默认不含翻译模型。按《保密模式说明.txt》获取模型，将 models 文件夹放在 EXE 旁。
多语种模型 NLLB 适用 CC BY-NC 4.0，请先阅读模型许可。

重要提醒
- 本工具免费云端档走的是微软/Edge 的免费网页翻译接口，属于非官方接口，随时可能变动；
  正文会发送到第三方服务器并可能被记录日志。含个人信息的材料应使用保密模式。
- 机器翻译结果需要人工复核，尤其是数字、单位、否定句和专有名词。
"""


def pyinstaller_args() -> list[str]:
    # 构建目录带版本号：全新路径，PyInstaller 不需要删任何旧目录。
    # （删旧 dist 是批量删除操作，不该由构建脚本替用户决定；旧版本构建产物
    #  用户确认没用后自行清理 build/ 即可。）
    tag = f"v{_app_version()}"
    args = [
        str(ROOT / "app.py"),
        "--name", APP_NAME,
        "--noconfirm", "--windowed",
        "--icon", str(ROOT / "assets" / "icon.ico"),
        "--distpath", str(DIST / f"dist-{tag}"),
        "--workpath", str(DIST / f"work-{tag}"),
        "--specpath", str(DIST),
        # 数据文件：版面模型、OCR 模型、分词词表、离线翻译内核的动态库
        "--collect-data", "pymupdf.layout",
        "--collect-submodules", "pymupdf.layout",
        "--hidden-import", "pymupdf.layout",
        "--collect-data", "rapidocr_onnxruntime",
        "--collect-all", "ctranslate2",
        "--collect-all", "sentencepiece",
        "--collect-all", "langdetect",
        f"--add-data={SITE / 'wordninja'}{os.pathsep}wordninja",
        # PPT（python-pptx）与脚本/Excel（openpyxl）的依赖都是函数内惰性导入，
        # 静态分析不可靠，显式 collect-all 最稳。openpyxl 会带上 et_xmlfile。
        "--collect-all", "pptx",
        "--collect-all", "openpyxl",
    ]
    for module in ("matplotlib", "scipy", "pandas", "IPython", "pytest", "notebook"):
        args += ["--exclude-module", module]
    # 保留随包依赖的上游许可证与版权归属。
    for package in (
        "ctranslate2", "sentencepiece", "langdetect", "python-docx",
        "pymupdf", "rapidocr-onnxruntime", "pillow", "requests",
        "tkinterdnd2", "wordninja", "psutil", "pywin32", "python-pptx", "openpyxl",
    ):
        args += ["--recursive-copy-metadata", package]
    return args


def build() -> Path:
    from PyInstaller.__main__ import run

    run(pyinstaller_args())
    built = DIST / f"dist-v{_app_version()}" / APP_NAME
    if not built.is_dir():
        raise SystemExit("PyInstaller 没有产出目录")
    return built


def assemble(built: Path, with_models: bool) -> Path:
    # 版本化包名不会与旧包冲突，因此不删任何东西；旧包由用户自行清理。
    if PACKAGE.exists():
        raise SystemExit(
            f"{PACKAGE} 已存在。请先手动删除或改名旧的分发包，再重新构建。"
        )
    shutil.copytree(built, PACKAGE)
    for name in DATA_FILES:
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, PACKAGE / name)
    samples = PACKAGE / "样例"
    samples.mkdir(exist_ok=True)
    for name in SAMPLE_FILES:
        source = ROOT / "samples" / name
        if source.is_file():
            shutil.copy2(source, samples / name)
    shutil.copy2(ROOT / "assets" / "icon.ico", PACKAGE / "icon.ico")
    (PACKAGE / "使用说明.txt").write_text(README, encoding="utf-8")
    for name, body in LAUNCHERS.items():
        (PACKAGE / name).write_bytes(body.encode("utf-8"))
    if with_models:
        shutil.copytree(MODELS, PACKAGE / "models")
    return PACKAGE


def zip_package(root: Path, with_models: bool) -> Path:
    """分享包不含 models（750MB，用 U 盘另拷）也不含试译产物。"""
    archive = SHARE / f"{root.name}{'-含离线模型' if with_models else ''}.zip"
    if archive.exists():
        archive.unlink()
    skip_dirs = () if with_models else ("models",)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(root)
            if relative.parts[0] in skip_dirs or "_双语" in path.stem:
                continue
            if path.name in ("自检结果.txt", "转换日志.txt"):
                continue
            bundle.write(path, path.relative_to(SHARE))
    return archive


def main() -> int:
    argv = sys.argv[1:]
    built = build()
    root = assemble(built, "--models" in argv)
    # 中间产物（build/pyi/dist-v{ver}、work-v{ver}）一律保留：删旧构建是不可逆操作，
    # 不该由构建脚本替用户决定，且重名冲突已靠版本化目录消除。确认旧版没用后，
    # 用户自行清理 build/pyi 与 分发/ 下的旧版本目录即可。
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) / 1048576
    print(f"免安装版目录：{root}")
    print(f"体积：{size:.0f} MB")
    if "--zip" in argv:
        archive = zip_package(root, "--models" in argv)
        print(f"压缩包：{archive}（{archive.stat().st_size / 1048576:.0f} MB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
