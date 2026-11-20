from __future__ import annotations

import hashlib
import html as html_lib
import http.client
import http.cookiejar
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Callable

from .common import CancelledError, TranslationError, check_cancel
from . import direction, glossary, mixed, segment
from .glossary import hit_rows
from .no_translate import intro, lock_and_mask, unlock_and_restore
from .style import STANDARD, polish
from .retry import BackoffPolicy, TransientError, http_error, retry, status_of
from .concurrency import (
    DEFAULT_JOBS,
    JobGovernor,
    TokenBucket,
    clamp_jobs,
    run_ordered,
)
from .tuning import cap_int

if TYPE_CHECKING:  # 只在类型检查时引入：运行时靠 app.py 注入实例，不为一两个
    from .tm import TranslationMemory  # 类型名让翻译器多背一层导入。

#: 近似复用明细的构造器（v1.9.9）：四个引擎共用同一个形状，
#: 报告那边只认这一种，不必逐档适配。
from .tm import fuzzy_hit  # noqa: E402  (放在类型块后，保持「运行时依赖只有这一处」可读)

BING_CHUNK = 900
MYMEMORY_CHUNK = 450
BING_BATCH_CHARS = 820
BING_BATCH_ITEMS = 10
BING_HOSTS = ("https://cn.bing.com", "https://www.bing.com")
EDGE_HOST = "edge.microsoft.com"
EDGE_PATH = "/translate/translatetext"
EDGE_CHUNK = 4000
EDGE_BATCH_CHARS = 6000
EDGE_BATCH_ITEMS = 40
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097f]")
THAI_RE = re.compile(r"[\u0e00-\u0e7f]")
GREEK_RE = re.compile(r"[\u0370-\u03ff]")
FOREIGN_RE = re.compile(
    r"[A-Za-z\u00c0-\u024f\u0370-\u03ff\u0400-\u04ff\u0530-\u058f"
    r"\u0590-\u05ff\u0600-\u06ff\u0900-\u097f\u0e00-\u0e7f"
    r"\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af\u1100-\u11ff]"
)
URL_RE = re.compile(r"^(?:https?://|ftp://|www\.)\S+$", re.I)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+$", re.I)
#: 译入英文时「值得翻」的文字：凡是非拉丁文字都算（中文、日文、韩文、
#: 俄文、阿拉伯文…）。英文段落本身不在其中。
NON_LATIN_RE = re.compile(
    r"[\u0370-\u03ff\u0400-\u04ff\u0530-\u058f\u0590-\u05ff\u0600-\u06ff"
    r"\u0900-\u097f\u0e00-\u0e7f\u3040-\u30ff\u31f0-\u31ff\uac00-\ud7af"
    r"\u1100-\u11ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
LATIN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")

#: 公式/统计量判定用的字符集。参考 Zotero 生态的 PDF2zh——学术文献翻译把公式
#: 当正文送进引擎必翻坏（`E = mc²` 会被拆成句子），所以这类片段一律保留原文。
MATH_SYMBOLS = (
    "=+−\u00b1\u00d7\u00f7\u2260\u2248\u2264\u2265\u2192\u2190\u2194"
    "\u21d2\u21d4\u2200\u2203\u2208\u2209\u222a\u2229\u221e\u221d\u221a"
    "\u222b\u2211\u220f\u2202\u2207\u2220\u2234\u2235\u2261\u226b\u226a"
)
SUPERSCRIPT_CHARS = "\u00b2\u00b3\u00b9\u2070\u2074\u2075\u2076\u2077\u2078\u2079\u207a\u207b\u207d\u207e\u207f\u1d43\u1d47"
SUBSCRIPT_CHARS = "\u2080\u2081\u2082\u2083\u2084\u2085\u2086\u2087\u2088\u2089\u208a\u208b\u2090\u2093"
_WORD_TOKEN_RE = re.compile(r"[A-Za-z\u00c0-\u024f]{3,}")


def looks_like_formula(text: str) -> bool:
    """看起来是公式、统计量或化学式的片段：几乎没有自然语言词汇，却有一堆算子。

    判定刻意保守——要求「长度 ≥3 的字母词 ≤1 个」且「算子/上下标/希腊字母 ≥2 个」。
    正常英文句子里有大把三字母以上的词，不会被误伤；`Total = 1,500` 这种只有一个
    算子、还带实词的表格串也放过去了。误判成公式的代价是「该翻的没翻」，所以宁可
    漏判也不误判。
    """
    t = normalize_source(text)
    if not t or len(t) > 200:
        return False
    if len(_WORD_TOKEN_RE.findall(t)) > 1:
        return False
    marks = sum(t.count(ch) for ch in MATH_SYMBOLS)
    marks += sum(t.count(ch) for ch in SUPERSCRIPT_CHARS + SUBSCRIPT_CHARS)
    marks += len(GREEK_RE.findall(t))
    return marks >= 2


def normalize_source(text: str) -> str:
    return text.replace("\u00a0", " ").replace("\u200b", "").strip()


def _should_translate_to_english(t: str) -> bool:
    """中译英的抽段判定：源语是非拉丁文字才翻。

    纯英文段落必须跳过——送进引擎最好的结果是原样回显，最坏是被"改写"一遍，
    两者都是无谓改动。混排段落用 4:1 的拉丁占比做闸门：「Python 是一种编程
    语言」要翻（拉丁 6 / 非拉丁 5）；「This is a test 中」这种尾巴带一个汉字
    的英文句子不翻。闸门只在拉丁字数 ≥8 时生效，短混排（「API 接口」）照常翻。
    """
    non_latin = len(NON_LATIN_RE.findall(t))
    if not non_latin:
        return False
    latin = len(LATIN_RE.findall(t))
    if latin >= 8 and latin > non_latin * 4:
        return False
    return True


def is_protected_source(text: str) -> bool:
    """整段不译的类型：URL / 邮箱 / 版本号 / 公式。

    单独提出来给混排切分用：一段「中文 + 一行公式」不能因为夹着外文就
    把公式送去翻译。语义与 ``should_translate`` 里的短路完全一致。
    """
    t = normalize_source(text)
    return bool(URL_RE.match(t) or EMAIL_RE.match(t) or VERSION_RE.match(t)) or looks_like_formula(t)


def should_translate(text: str) -> bool:
    t = normalize_source(text)
    if len(t) < 2:
        return False
    if is_protected_source(t):
        return False
    if direction.is_english():
        if not _should_translate_to_english(t):
            return False
    else:
        if not FOREIGN_RE.search(t):
            return False
        cjk = len(CJK_RE.findall(t))
        foreign = len(FOREIGN_RE.findall(t))
        if cjk and not KANA_RE.search(t) and not HANGUL_RE.search(t):
            if cjk / max(cjk + foreign, 1) >= 0.4:
                return False
    if not re.search(r"\s", t):
        symbols = sum(t.count(ch) for ch in "{}[]();=<>_\\/|")
        if len(t) > 12 and symbols >= 2:
            return False
    return True


def is_translatable(text: str) -> bool:
    """正文判定之外再查一次标题词条：「目次」「別添資料」这类纯汉字日文标题
    会被 should_translate 当成中文直接跳过。"""
    if should_translate(text):
        return True
    normalized = normalize_source(text)
    term = direction.lookup_heading(normalized)
    return bool(term and term != normalized)


def guess_lang_pair(text: str) -> str:
    target = direction.mymemory_code()
    if KANA_RE.search(text):
        return f"ja|{target}"
    if HANGUL_RE.search(text):
        return f"ko|{target}"
    if CYRILLIC_RE.search(text):
        return f"ru|{target}"
    if ARABIC_RE.search(text):
        return f"ar|{target}"
    if THAI_RE.search(text):
        return f"th|{target}"
    if DEVANAGARI_RE.search(text):
        return f"hi|{target}"
    if GREEK_RE.search(text):
        return f"el|{target}"
    # 译入英文时，落到这里的通常是中文（拉丁/西里尔等已各自命中）。
    if direction.is_english():
        return f"zh|{target}"
    return f"en|{target}"


def finish_text(text: str, style: str) -> str:
    """译后处理闸门：``style.polish`` 只对中文成品有意义。

    它会做中英之间的空格整理、全角标点归一，套在英文句子上等于把
    "Hello, world." 改成 "Hello，world。"——方向为 en 时必须整段跳过。
    """
    return polish(text, style) if direction.use_chinese_postprocess() else text


def split_for_api(text: str, limit: int) -> list[str]:
    """超长段落送翻前的拆片：先按自然句边界拆，单句仍超长再安全下刀。

    切句与切点规则都在 ``core/segment.py``（v1.6.4，G1）——原先这里只认
    ``[.!?;:]`` 加空白、兜底只会 ``rfind(" ")``，中文原文两条都落空，长段会被
    正好从词中间劈成两片。拼片语义保持不变：句子之间用空格接，超预算就另起一片。
    """
    text = normalize_source(text)
    if len(text) <= limit:
        return [text] if text else []

    chunks: list[str] = []
    current = ""
    for sentence in segment.split_sentences(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        while len(sentence) > limit:
            piece, sentence = segment.safe_cut(sentence, limit)
            if not piece:
                break
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        if not sentence:
            continue
        if len(current) + 1 + len(sentence) <= limit:
            current = f"{current} {sentence}".strip()
        else:
            if current:
                chunks.append(current)
            current = sentence
    if current:
        chunks.append(current)
    return chunks


class EdgeEngine:
    """Microsoft Edge's keyless translate endpoint: one request carries many
    paragraphs, so it needs neither scraped tokens nor sentinel splitting."""

    #: 取消信号：由 ``Translator`` 构造后透传。引擎内部的退避重试要认它——
    #: 用户点了「停止」之后，不该还有一段在退避里睡 0.6 秒。
    stop_event = None

    #: 请求尺寸（段数 / 单请求字符 / 单段字符）：这三个数是 Edge 接口的硬限，
    #: 由 ``Translator`` 按用户的 ``--batch-items=`` 等开关**收紧**后覆盖到实例上
    #: （只减不加，见 core/tuning.py）。写成类属性是为了单独 new 一个引擎时
    #: 仍旧拿到硬限默认值。
    batch_items = EDGE_BATCH_ITEMS
    batch_chars = EDGE_BATCH_CHARS
    chunk_chars = EDGE_CHUNK

    #: 退避序列（首发之后逐次的等待秒数）。序列长度决定重试次数，
    #: 由 ``Translator`` 用 ``--retry-delays=`` 覆盖；默认 1 档 = 首发 + 1 次重试。
    retry_delays = (0.6,)

    def __init__(self) -> None:
        self._conn: http.client.HTTPSConnection | None = None

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _connection(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(EDGE_HOST, timeout=30)
        return self._conn

    def _request_once(self, texts: list[str]) -> list[str]:
        path = urllib.parse.urlencode(
            {"from": "", "to": direction.web_code(), "isEnterpriseClient": "false"}
        )
        connection = self._connection()
        connection.request(
            "POST",
            f"{EDGE_PATH}?{path}",
            body=json.dumps(texts, ensure_ascii=False).encode("utf-8"),
            headers={
                "User-Agent": UA,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
            },
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8", "ignore")
        if response.status == 429:
            self._close_connection()
            # 带状态码抛出：429 属于「对方暂时不想理你」，交给统一退避重试；
            # 不带码的话上层只能靠文案猜，分类迟早会错。
            raise http_error(429, "Edge 翻译接口限流")
        if response.status != 200:
            raise http_error(
                response.status, f"Edge 翻译接口返回 HTTP {response.status}"
            )
        payload = json.loads(raw)
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise TranslationError("Edge 翻译返回格式异常")
        results: list[str] = []
        for item in payload:
            translations = (item or {}).get("translations") or []
            text = translations[0].get("text", "") if translations else ""
            if not text.strip():
                raise TranslationError("Edge 翻译返回空结果")
            results.append(text)
        return results

    def _request(self, texts: list[str]) -> list[str]:
        """带统一退避地发一次 Edge 请求。

        改前是手写 ``for attempt in range(2)`` + ``sleep(0.6)``，对**任何**
        异常都重试一次——包括 400/403 这类重试一万次也不会变的错。现在交给
        ``core.retry``：429/5xx/网络才重试，其余一次就判死；每次重试出声。
        """
        policy = BackoffPolicy(
            max_attempts=len(self.retry_delays) + 1,
            delays=self.retry_delays,
            max_delay=max(self.retry_delays),
        )

        def once() -> list[str]:
            try:
                return self._request_once(texts)
            except CancelledError:
                raise
            except Exception:
                # 连接可能已经半死（读到一半断了），必须换一条再试。
                self._close_connection()
                raise

        try:
            return retry(
                once,
                policy=policy,
                stop_event=getattr(self, "stop_event", None),
                label="Edge 翻译请求",
            )
        except CancelledError:
            raise
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"Edge 翻译请求失败：{type(exc).__name__}") from exc

    def _groups(self, texts: list[str]) -> list[list[str]]:
        groups: list[list[str]] = []
        current: list[str] = []
        length = 0
        for text in texts:
            if current and (
                length + len(text) > self.batch_chars
                or len(current) >= self.batch_items
            ):
                groups.append(current)
                current = []
                length = 0
            current.append(text)
            length += len(text)
        if current:
            groups.append(current)
        return groups

    def _send(self, texts: list[str]) -> list[str]:
        results: list[str] = []
        for group in self._groups(texts):
            results.extend(self._request(group))
        return results

    def translate(self, text: str) -> str:
        chunks = split_for_api(text, self.chunk_chars)
        if not chunks:
            raise TranslationError("Edge 翻译返回空结果")
        result = "".join(self._send(chunks))
        if not result.strip():
            raise TranslationError("Edge 翻译返回空结果")
        return result

    def translate_batch(self, texts: list[str]) -> list[str]:
        results: list[str] = []
        ready: list[str] = []
        for text in texts:
            if len(text) > self.chunk_chars:
                results.extend(self._send(ready))
                ready = []
                results.append(self.translate(text))
            else:
                ready.append(text)
        results.extend(self._send(ready))
        return results


def _echoes_source(source: str, translated: str) -> bool:
    """A batch reply that repeats the source on its own line and adds the real
    translation beside it means the batch framing leaked into the output."""
    head, _, tail = translated.partition("\n")
    if tail.strip() and head.strip().lower() == normalize_source(source).lower():
        return True
    head, _, tail = translated.rpartition("\n")
    return bool(head.strip()) and tail.strip().lower() == normalize_source(source).lower()


class BingEngine:
    #: 取消信号（由 ``Translator`` 透传，理由见 EdgeEngine 的同名注释）。
    stop_event = None
    #: 请求尺寸（同 EdgeEngine：820 字符 / 10 段是网页接口的实测上限，
    #: 单段 900 是 ``ttranslatev3`` 的长度上限；只允许由用户阈值收紧）。
    batch_items = BING_BATCH_ITEMS
    batch_chars = BING_BATCH_CHARS
    chunk_chars = BING_CHUNK

    #: 退避序列，口径同 EdgeEngine（默认 1 档 0.8 秒）。
    retry_delays = (0.8,)

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        self.cookie_jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.log = log or (lambda message: None)
        self.host = ""
        self.ig = ""
        self.iid = ""
        self.key = ""
        self.token = ""
        self.obtained_at = 0.0
        self.host_index = 0
        self._conn: http.client.HTTPSConnection | None = None
        self._conn_host = ""
        self._cookie_header = ""

    def _page_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

    def _api_headers(self) -> dict[str, str]:
        return {
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Referer": f"{self.host}/translator",
            "Origin": self.host,
            "X-Requested-With": "XMLHttpRequest",
            "Connection": "keep-alive",
            "Accept-Encoding": "identity",
            "Cookie": self._cookie_header,
        }

    def _close_connection(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None
        self._conn_host = ""

    def _connection(self) -> http.client.HTTPSConnection:
        hostname = urllib.parse.urlparse(self.host).hostname or "cn.bing.com"
        if self._conn is None or self._conn_host != hostname:
            self._close_connection()
            self._conn = http.client.HTTPSConnection(hostname, timeout=35)
            self._conn_host = hostname
        return self._conn

    def refresh(self) -> None:
        errors: list[str] = []
        for offset in range(len(BING_HOSTS)):
            index = (self.host_index + offset) % len(BING_HOSTS)
            host = BING_HOSTS[index]
            try:
                request = urllib.request.Request(
                    f"{host}/translator", headers=self._page_headers()
                )
                with self.opener.open(request, timeout=30) as response:
                    page = response.read().decode("utf-8", "ignore")
                ig = re.search(r'IG:"([^"]+)"', page)
                iid = re.search(r'data-iid="([^"]+)"', page)
                token_info = re.search(
                    r"params_AbusePreventionHelper\s*=\s*(\[[^\]]+\])", page
                )
                if not (ig and iid and token_info):
                    raise TranslationError("翻译页面凭据解析失败")
                values = json.loads(token_info.group(1))
                self.host = host
                self.ig = ig.group(1)
                self.iid = iid.group(1)
                self.key = str(values[0])
                self.token = values[1]
                self.obtained_at = time.time()
                self.host_index = index
                self._cookie_header = "; ".join(
                    f"{cookie.name}={cookie.value}" for cookie in self.cookie_jar
                )
                self._close_connection()
                return
            except Exception as exc:
                errors.append(f"{host.split('//')[-1]}: {type(exc).__name__}")
        raise TranslationError("无法连接微软翻译服务（" + "；".join(errors) + "）")

    def _ensure_token(self) -> None:
        if not self.token or time.time() - self.obtained_at > 2400:
            self.refresh()

    def _post_once(self, text: str) -> str:
        self._ensure_token()
        connection = self._connection()
        body = urllib.parse.urlencode(
            {
                "fromLang": "auto-detect",
                "to": direction.web_code(),
                "text": text,
                "token": self.token,
                "key": self.key,
            }
        ).encode("utf-8")
        path = f"/ttranslatev3?isVertical=1&IG={self.ig}&IID={self.iid}"
        connection.request("POST", path, body=body, headers=self._api_headers())
        response = connection.getresponse()
        status = response.status
        location = response.getheader("Location")
        raw = response.read().decode("utf-8", "ignore")
        if status in (301, 302, 303, 307, 308):
            target = urllib.parse.urlparse(location or "")
            new_host = f"https://{target.netloc}" if target.netloc else ""
            if new_host in BING_HOSTS and new_host != self.host:
                self.host = new_host
                self.host_index = BING_HOSTS.index(new_host)
                self.token = ""
                self._close_connection()
            raise TranslationError("微软翻译接口发生重定向")
        if status != 200:
            raise http_error(status, f"微软翻译接口返回 HTTP {status}")
        payload = json.loads(raw)
        if isinstance(payload, list) and payload:
            translations = payload[0].get("translations") or []
            if translations:
                translated = translations[0].get("text", "")
                if translated.strip():
                    return translated
        raise TranslationError("微软翻译返回格式异常")

    def _post(self, text: str) -> str:
        """带统一退避地发一次微软翻译请求（含失败后重取凭据）。

        与 ``EdgeEngine._request`` 同一套口径：429/5xx/网络才重试，参数错与
        key 错一次判死。退避期间顺手重取凭据（改前也是这么做的）——微软网页
        通道的凭据 40 分钟就过期，重试不换凭据等于再撞一次墙。
        """
        policy = BackoffPolicy(
            max_attempts=len(self.retry_delays) + 1,
            delays=self.retry_delays,
            max_delay=max(self.retry_delays),
        )

        def once() -> str:
            try:
                return self._post_once(text)
            except CancelledError:
                raise
            except Exception:
                self._close_connection()
                raise

        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            # 重取凭据失败不用管：_post_once 里的 _ensure_token 会再试一次，
            # 这里只负责「尽量让下一次请求带上新凭据」。
            try:
                self.refresh()
            except Exception:
                pass

        try:
            return retry(
                once,
                policy=policy,
                on_retry=on_retry,
                stop_event=getattr(self, "stop_event", None),
                label="微软翻译请求",
            )
        except CancelledError:
            raise
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"微软翻译请求失败：{type(exc).__name__}") from exc

    def translate(self, text: str) -> str:
        pieces = [self._post(chunk) for chunk in split_for_api(text, self.chunk_chars)]
        result = "".join(pieces)
        if not result.strip():
            raise TranslationError("微软翻译返回空结果")
        return result

    def translate_batch(self, texts: list[str]) -> list[str]:
        if len(texts) == 1:
            return [self.translate(texts[0])]
        total_chars = sum(len(text) for text in texts)
        if len(texts) > self.batch_items or total_chars > self.batch_chars:
            return [self.translate(text) for text in texts]

        marker = secrets.token_hex(3)
        separators = [
            f"[[[SEG_{marker}_{index}]]]" for index in range(1, len(texts))
        ]
        combined = texts[0]
        for separator, text in zip(separators, texts[1:]):
            combined += f"\n{separator}\n{text}"
        translated = self._post(combined)
        pattern = re.compile(
            r"\s*\[\[\[SEG_" + re.escape(marker) + r"_(\d+)\]\]\]\s*"
        )
        matches = list(pattern.finditer(translated))

        def fallback() -> list[str]:
            self.log("批量译文分段校验失败，改为逐段翻译")
            return [self.translate(text) for text in texts]

        if len(matches) != len(separators):
            return fallback()
        parts: list[str] = []
        cursor = 0
        expected = 1
        for match in matches:
            if int(match.group(1)) != expected:
                return fallback()
            parts.append(translated[cursor : match.start()].strip())
            cursor = match.end()
            expected += 1
        parts.append(translated[cursor:].strip())
        if len(parts) != len(texts) or any(not part for part in parts):
            return fallback()
        if any(_echoes_source(source, part) for source, part in zip(texts, parts)):
            return fallback()
        return parts


class MyMemoryEngine:
    #: 备用通道的退避策略：首发 + 1 次重试（它是最后一道兜底，重试太多次
    #: 只会让用户在「三条通道全挂」的等待里多耗几秒）。
    POLICY = BackoffPolicy(max_attempts=2, base_delay=0.4, max_delay=1.0)

    #: 取消信号（由 ``Translator`` 透传，理由见 EdgeEngine 的同名注释）。
    stop_event = None

    #: 单段字符上限（MyMemory 只有一条「每次一问」的 GET 接口，450 是它实测能
    #: 稳定返回的长度；同样只允许由用户阈值收紧，见 core/tuning.py）。
    chunk_chars = MYMEMORY_CHUNK

    #: 用户给的退避序列（``--retry-delays=``）；None = 沿用类上的指数退避。
    #: 这里不直接把序列塞进 POLICY，是因为默认那条是**带抖动**的指数退避，
    #: 换成显式序列就变成确定值——那是既有测试钉住的行为，不能被顺手改掉。
    retry_delays = None

    def _policy(self) -> BackoffPolicy:
        if self.retry_delays is None:
            return self.POLICY
        return BackoffPolicy(
            max_attempts=len(self.retry_delays) + 1,
            delays=self.retry_delays,
            max_delay=max(self.retry_delays),
        )

    def translate(self, text: str) -> str:
        pieces: list[str] = []
        for chunk in split_for_api(text, self.chunk_chars):
            pair = guess_lang_pair(chunk)
            query = urllib.parse.urlencode({"q": chunk, "langpair": pair})
            request = urllib.request.Request(
                f"https://api.mymemory.translated.net/get?{query}",
                headers={"User-Agent": UA, "Accept": "application/json"},
            )
            def fetch() -> dict:
                # HTTPError 必须排在 URLError 之前：它是 URLError 的子类，
                # 先抓父类就拿不到状态码了（而分类全靠状态码）。
                try:
                    with urllib.request.urlopen(request, timeout=25) as response:
                        return json.loads(response.read().decode("utf-8", "ignore"))
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        # 共享额度被打爆：按可重试处理，让统一退避去等；
                        # 等几次还不行就如实失败（次数由策略上限封顶）。
                        raise http_error(429, "备用翻译接口额度受限") from exc
                    raise http_error(
                        exc.code, f"备用翻译接口返回 HTTP {exc.code}"
                    ) from exc
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    # 网络类必须标成 TransientError：TranslationError 不带
                    # 状态码也不带类型标记，分类器会按保守口径拒绝重试。
                    raise TransientError("无法连接备用翻译接口") from exc

            payload = retry(
                fetch,
                policy=self._policy(),
                stop_event=getattr(self, "stop_event", None),
                label="备用翻译请求",
            )
            status = int(payload.get("responseStatus", 0) or 0)
            if status != 200:
                raise TranslationError(f"备用翻译接口返回 {status}")
            data = payload.get("responseData") or {}
            translated = html_lib.unescape(data.get("translatedText") or "")
            upper = translated.upper()
            if not translated.strip() or "MYMEMORY WARNING" in upper or "QUOTA" in upper:
                raise TranslationError("备用翻译接口额度受限")
            pieces.append(translated)
            time.sleep(0.08)
        result = "".join(pieces)
        if not result.strip():
            raise TranslationError("备用翻译接口返回空结果")
        return result


from .engine_base import call_batch, call_one, supports_context
from .ctx import TranslationContext


class TranslationCache:
    """译文缓存。

    键里带上 profile（术语表指纹 + 风格档位）。术语锁与风格润色都发生在
    翻译之后，缓存里存的是最终译文；术语表或风格一变，旧译文就全部作废，
    否则用户改了术语表重翻同一份文件会静默拿到旧译文。
    """

    def __init__(self, path: str | None, profile: str = "") -> None:
        self._mem: dict[str, str] = {}
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._profile = profile
        #: 保留原始路径：profile 变了要用同一个文件重建连接，不能新建一个库。
        self.path = path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                "key TEXT PRIMARY KEY, translated TEXT NOT NULL, "
                "created_at INTEGER NOT NULL)"
            )
            self._conn.commit()
        except Exception:
            self._conn = None

    def _key(self, engine: str, text: str) -> str:
        # 方向必须进键：同一句原文先译中文再译英文，不能拿到上一次的译文。
        raw = (
            f"{self._profile}\0{engine}\0{direction.cache_key_part()}\0{text}"
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def get(self, engine: str, text: str) -> str | None:
        key = self._key(engine, text)
        with self._lock:
            if key in self._mem:
                return self._mem[key]
            if self._conn is None:
                return None
            try:
                row = self._conn.execute(
                    "SELECT translated FROM translations WHERE key=?", (key,)
                ).fetchone()
                if row:
                    self._mem[key] = row[0]
                    return row[0]
            except Exception:
                self._conn = None
        return None

    def set(self, engine: str, text: str, translated: str) -> None:
        key = self._key(engine, text)
        with self._lock:
            self._mem[key] = translated
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    "INSERT OR REPLACE INTO translations(key, translated, created_at) "
                    "VALUES (?, ?, ?)",
                    (key, translated, int(time.time())),
                )
                self._conn.commit()
            except Exception:
                self._conn = None


class Translator:
    def __init__(
        self,
        cache_path: str | None = None,
        stop_event=None,
        log: Callable[[str], None] | None = None,
        style: str = STANDARD,
        tm: "TranslationMemory | None" = None,
        jobs: int = DEFAULT_JOBS,
        batch_items: int | None = None,
        batch_chars: int | None = None,
        chunk_chars: int | None = None,
        retry_delays: tuple[float, ...] | None = None,
    ) -> None:
        self.log = log or (lambda message: None)
        self.style = style
        self.edge = EdgeEngine()
        self.bing = BingEngine(self.log)
        self.mymemory = MyMemoryEngine()
        self._cache_path = cache_path
        self.cache = TranslationCache(cache_path, self._cache_profile())
        self.stop_event = stop_event
        # 透传取消信号给引擎层：引擎内部也有退避重试（Edge/Bing/备用），
        # 不给它们 stop_event，用户点「停止」后那一段还要把退避睡完。
        for engine in (self.edge, self.bing, self.mymemory):
            engine.stop_event = stop_event
        # 请求尺寸旋钮（--batch-items= / --batch-chars= / --chunk-chars=）：同一个
        # 用户值对三条通道分别夹到各自的硬限，夹到哪、为什么夹都要出声。
        # 上限写 None 表示这条通道没有那个维度——MyMemory 是「每次一问」的 GET
        # 接口，只有单段长度可调；给它挂一个没人读的 batch_* 属性，比少写两行
        # 代码更容易骗到后来人。
        for engine, name, items, chars, chunk in (
            (self.edge, "Edge", EDGE_BATCH_ITEMS, EDGE_BATCH_CHARS, EDGE_CHUNK),
            (self.bing, "微软", BING_BATCH_ITEMS, BING_BATCH_CHARS, BING_CHUNK),
            (self.mymemory, "备用", None, None, MYMEMORY_CHUNK),
        ):
            if items is not None:
                engine.batch_items = cap_int(
                    batch_items, label="单次请求段数",
                    engine=name, default=items, log=self.log,
                )
            if chars is not None:
                engine.batch_chars = cap_int(
                    batch_chars, label="单次请求字符数",
                    engine=name, default=chars, log=self.log,
                )
            engine.chunk_chars = cap_int(
                chunk_chars, label="单段字符数",
                engine=name, default=chunk, log=self.log,
            )
        # --retry-delays=：三条通道共用一套退避序列（默认 Edge 0.6 秒、微软 0.8 秒、
        # 备用 0.4 秒起指数退避，都是「首发 + 1 次重试」）。用户写 N 档就是首发 + N
        # 次重试，与付费档同口径；没写就一个字也不说。
        if retry_delays is not None:
            for engine in (self.edge, self.bing, self.mymemory):
                engine.retry_delays = retry_delays
            self.log(
                "三条在线通道的退避按你的设置改为 "
                + "、".join(f"{delay:g} 秒" for delay in retry_delays)
                + f"，即最多重试 {len(retry_delays)} 次。"
            )
        self._edge_limits = (self.edge.batch_items, self.edge.batch_chars, 0)
        self._bing_limits = (self.bing.batch_items, self.bing.batch_chars, 28)
        # ↑ 夹完的值在这里**留一份副本**，切批时用副本而不是回头问引擎要：
        # 测试里的假通道、以及 A2 注册表里挂进来的第三方引擎都只保证有
        # translate / translate_batch，回头问它们要 batch_items 会在最难复现
        # 的那条路径上 AttributeError。
        self.edge_disabled = False
        self.bing_disabled = False
        self._preflighted = False
        self._edge_failures_in_row = 0
        # 同为计数器，但它是 _batch_pass / _translate_one_raw 的**熔断状态**：
        # 漏初始化时症状不是统计少一个数，而是「Bing 连续失败第三次直接
        # AttributeError」，本该优雅转备用通道的请求会整批丢掉。
        self._bing_failures_in_row = 0
        #: 当前生效的上下文。阶段 3 的验证器会读它判断译文与语境是否相符。
        self._active_ctx: TranslationContext | None = None
        # 统计字段：summary() 与逐段计数都从这里读，必须全部先初始化，
        # 否则第一次调用 summary / 计数时 AttributeError。
        self.translated_count = 0
        self.skipped_count = 0
        self.cache_hits = 0
        self.failed_count = 0
        self.term_count = 0
        self.echo_count = 0
        self._edge_batch_failures = 0
        self._bing_batch_failures = 0
        self._edge_warned = False
        self._bing_warned = False
        self._failure_logs = 0
        #: 并发相关（默认 1 = 不并发，与改动前逐字节等价）。
        #: ``governor`` 按档位夹上限并在出事时自动降回 1；``_bucket`` 是
        #: 免费云端档的礼貌阀门——别人的免费接口，并发也得限速。
        self.jobs = clamp_jobs("cloud", jobs)
        self.governor = JobGovernor("cloud", jobs, log=self.log)
        self._bucket = TokenBucket()
        # 并发度说明必须在**构造时**就出声：免费云端档的策略是不并发，并行
        # 路径永远不会被执行，等到翻译开始再说就等于永远不说。
        self.governor.announce()
        #: 并发下多个 worker 会同时改计数器（``translated_count += 1`` 在
        #: GIL 下也不是原子的）。所有计数改动都过这把锁；网络请求不加锁，
        #: 否则并发就没有意义了。
        self._counter_lock = threading.Lock()
        #: 最近一次失败的状态码（供并发管家判断「是不是被限流了」）。
        self._last_status = 0
        #: 翻译记忆（可注入）。None 表示不使用——保密模式（--offline）走
        #: LocalTranslator，它压根不落盘，也就不该有记忆可读（见 app.py 装配）。
        #: 两级命中**分开计数**：``tm_exact`` 是完全一致（100% 直通，没进
        #: 引擎），``tm_fuzzy`` 是近似匹配（≥0.92，用了但要标"待确认"）。
        #: 混成一个数就没法回答"这次有多少是直接不用翻的"。
        self.tm = tm
        self.tm_exact = 0
        self.tm_fuzzy = 0
        #: 近似复用的明细（v1.9.9）：{段号, 原文, 相似度}。汇总里只说「N 段（待确认）」
        #: 用户找不到是哪几段，明细进自检报告后才有位置可依。
        self.tm_fuzzy_hits: list[dict] = []
        intro = glossary.intro()
        if intro:
            self.log(intro)
        #: 术语命中明细（本次运行）：每处一条 {段落号, 原文术语, 指定译法, 是否生效}。
        #: 只存内存、不落盘（保密模式零落盘承诺不受影响）；**缓存命中的段不记**——
        #: 报告反映的是本次运行的术语去向，不是历史缓存的去向。段落号取
        #: translate_texts 的 index+1，与排版器报告里「段落 N」的编号一致。
        #: 按 v1.3.0 的教训追加在 __init__ 末尾，不插进已有逻辑中间。
        self.term_hits: list[dict] = []

    def _cache_profile(self) -> str:
        """术语表指纹 + 风格档位。两者任何一个变了，缓存都必须整体换一批键。"""
        try:
            return f"{glossary.fingerprint()}:{self.style}"
        except Exception:
            return str(self.style)

    def _sync_cache_profile(self, ctx: TranslationContext | None) -> None:
        """把上下文里的**稳定**维度合并进缓存 profile。

        只取术语指纹 / 风格 / 目标语种 / 在场角色四项；场景摘要刻意排除
        （理由写在 core/ctx.py 模块说明里）：摘要每批次都在变，一旦进缓存键，
        长文档的命中率会被打到接近于零——为了那点精度把缓存彻底废掉不划算。
        """
        base = self._cache_profile()
        extra = "" if ctx is None or ctx.is_trivial() else ctx.cache_profile()
        profile = f"{base}|{extra}" if extra else base
        if profile != getattr(self.cache, "_profile", None):
            self.cache = TranslationCache(self._cache_path, profile)

    def summary(self) -> str:
        text = (
            f"翻译 {self.translated_count} 段，跳过 {self.skipped_count} 段，"
            f"缓存命中 {self.cache_hits} 段，失败 {self.failed_count} 段"
        )
        if self.term_count:
            text += f"，标题词条直译 {self.term_count} 段"
        if self.echo_count:
            text += f"，{self.echo_count} 段引擎只返回原文"
        if self.tm_exact:
            text += f"，翻译记忆直通 {self.tm_exact} 段（完全一致，未进引擎）"
        if self.tm_fuzzy:
            text += f"，记忆近似复用 {self.tm_fuzzy} 段（待确认）"
        locked = intro()
        if locked:
            text += f"，{locked}"
        if self.term_hits:
            failed = sum(1 for hit in self.term_hits if not hit["ok"])
            text += f"，术语命中 {len(self.term_hits)} 处，{failed} 处未生效"
        return text

    def _record_term_hit(
        self, paragraph: int, source: str, target: str, ok: bool
    ) -> None:
        """记一条术语命中。paragraph 是 1 起的段落号。"""
        self.term_hits.append(
            {"paragraph": paragraph, "source": source, "target": target, "ok": ok}
        )

    def preflight(self) -> None:
        probe = "Hello, how are you?"
        if not self.edge_disabled:
            try:
                result = self.edge.translate(probe)
                if result.strip() and result.strip() != probe:
                    self.log("已连接 Edge 免费翻译通道")
                    self._preflighted = True
                    return
            except Exception as exc:
                self.edge_disabled = True
                self.log(f"Edge 通道暂不可用（{type(exc).__name__}），尝试其他通道")
        if not self.bing_disabled:
            try:
                result = self.bing.translate(probe)
                if result.strip() and result.strip() != probe:
                    self.edge_disabled = True
                    self.log("已连接微软免费翻译通道")
                    self._preflighted = True
                    return
            except Exception as exc:
                self.bing_disabled = True
                self.log(f"微软通道暂不可用（{type(exc).__name__}），尝试备用通道")
        try:
            result = self.mymemory.translate(probe)
            if result.strip():
                self.log("已连接备用免费翻译通道")
                self._preflighted = True
                return
        except Exception as exc:
            raise TranslationError(
                "三条在线翻译通道都连不上（Edge、微软网页、备用接口），"
                "通常是本机网络屏蔽了这些服务。请稍后重试，"
                "或改用「保密模式」——它用本机离线模型，不需要联网。"
            ) from exc
        raise TranslationError(
            "在线翻译通道无响应。请改用「保密模式」（本机离线翻译，不需要网络）。"
        )

    def _make_batches(
        self,
        indices: list[int],
        texts: list[str],
        max_items: int,
        max_chars: int,
        separator_cost: int = 28,
    ) -> list[list[int]]:
        batches: list[list[int]] = []
        current: list[int] = []
        length = 0
        for index in indices:
            text = normalize_source(texts[index])
            size = len(text)
            if size > max_chars:
                if current:
                    batches.append(current)
                    current = []
                    length = 0
                batches.append([index])
                continue
            extra = size + (separator_cost if current else 0)
            if current and (
                length + extra > max_chars or len(current) >= max_items
            ):
                batches.append(current)
                current = []
                length = 0
                extra = size
            current.append(index)
            length += extra
        if current:
            batches.append(current)
        return batches

    def _cache_lookup(self, text: str) -> tuple[str | None, str]:
        for engine in ("edge", "bing", "mymemory"):
            cached = self.cache.get(engine, text)
            if cached is None:
                continue
            # An early run may have stored a channel's "translation" that was
            # just the source echoed back; that must not stay a permanent hit.
            if normalize_source(cached) == text:
                continue
            return cached, engine
        return None, ""

    def _batch_pass(
        self,
        indices: list[int],
        texts: list[str],
        results: list[str | None],
        engine,
        name: str,
        limits: tuple[int, int, int],
        resolved: int,
        progress: Callable[[int, int, str], None] | None,
        ctx: TranslationContext | None = None,
    ) -> tuple[list[int], int]:
        """Translate as many segments as the channel's batch API accepts and
        return the indices that must fall through to the next channel."""
        # 显式传 ctx 是为了测试；调用点没传就沿用本次 translate_texts 的上下文。
        if ctx is None:
            ctx = self._active_ctx
        max_items, max_chars, separator_cost = limits
        label = "Edge" if name == "edge" else "微软"
        remaining: list[int] = []
        batches = self._make_batches(
            indices, texts, max_items, max_chars, separator_cost
        )
        for batch in batches:
            check_cancel(self.stop_event)
            disabled = self.edge_disabled if name == "edge" else self.bing_disabled
            if disabled:
                remaining.extend(batch)
                continue
            plain = [normalize_source(texts[index]) for index in batch]
            # hits 收集「占位符, 原文匹配词, 官方译法」，供自检报告做
            # 「术语命中对照」；不传 hits 的调用方行为不变。
            prepared: list[tuple[str, dict[str, str], list[tuple[str, str, str]]]] = []
            for item in plain:
                hits: list[tuple[str, str, str]] = []
                # 免翻区（URL/邮箱/版本号…）先掩掉，再锁术语——顺序的
                # 理由见 core/no_translate.py 的「组合顺序」段。
                masked, mapping = lock_and_mask(item, hits)
                prepared.append((masked, mapping, hits))
            batch_texts = [masked for masked, _, _ in prepared]
            try:
                # 走统一入口：支持上下文的引擎会拿到 ctx，不支持的自动降级成
                # 只传文本。这里不能反过来写——给旧引擎硬塞参数会直接 TypeError。
                translated_list = call_batch(engine, batch_texts, ctx)
                if len(translated_list) != len(batch):
                    raise TranslationError("批量翻译返回数量不符")
                if any(not translated.strip() for translated in translated_list):
                    raise TranslationError("批量翻译存在空结果")
                if any(
                    _echoes_source(source, translated)
                    for source, translated in zip(batch_texts, translated_list)
                ):
                    raise TranslationError("批量翻译回显了原文")
                # 换回官方译法并按档位润色。缓存里存的是未掩蔽的原文，
                # 所以必须在写缓存之前还原。
                translated_list = [
                    finish_text(unlock_and_restore(item, mapping), self.style)
                    for item, (_, mapping, _hits) in zip(translated_list, prepared)
                ]
            except CancelledError:
                raise
            except Exception as exc:
                self.log(f"{label}批量翻译失败（{type(exc).__name__}），本批改用其他通道")
                if name == "edge":
                    self._edge_batch_failures += 1
                    self._edge_failures_in_row += 1
                    if self._edge_batch_failures >= 2:
                        self.edge_disabled = True
                        self.log("Edge 通道连续失败，后续内容改用其他通道")
                else:
                    self._bing_batch_failures += 1
                    self._bing_failures_in_row += 1
                    if self._bing_batch_failures >= 2:
                        self.bing_disabled = True
                        self.log("微软通道连续失败，后续内容改用其他通道")
                remaining.extend(batch)
                continue
            if name == "edge":
                self._edge_batch_failures = 0
                self._edge_failures_in_row = 0
            else:
                self._bing_batch_failures = 0
                self._bing_failures_in_row = 0
            stored = 0
            for index, translated, (_, _mapping, hits) in zip(
                batch, translated_list, prepared
            ):
                if normalize_source(translated) == normalize_source(texts[index]):
                    # The channel handed this line back untranslated. Keep it
                    # pending so another channel tries it, instead of caching
                    # the English as if it were the answer.
                    remaining.append(index)
                    continue
                results[index] = translated
                # 术语命中只在译文被真正接受时记：回显段会换通道重译，
                # 提前记会让同一段出现两条命中记录。
                if hits:
                    self.term_hits.extend(hit_rows(index + 1, hits, translated))
                self.cache.set(name, normalize_source(texts[index]), translated)
                self.translated_count += 1
                stored += 1
            resolved += stored
            if progress is not None:
                progress(min(resolved, len(texts)), len(texts), "")
        return remaining, resolved

    def translate_texts(
        self,
        texts: list[str],
        progress: Callable[[int, int, str], None] | None = None,
        ctx: TranslationContext | None = None,
    ) -> list[str]:
        # 上下文里的稳定维度（术语/风格/语种/角色）必须进入缓存键，
        # 否则换了术语或角色、却因为键不变拿到上一次的旧译文。
        self._sync_cache_profile(ctx)
        self._active_ctx = ctx
        total = len(texts)
        results: list[str | None] = [None] * total
        pending: list[int] = []
        resolved = 0

        for index, source in enumerate(texts):
            check_cancel(self.stop_event)
            normalized = normalize_source(source)
            if not normalized:
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            term = direction.lookup_heading(normalized)
            if term and term != normalized:
                results[index] = term
                self.term_count += 1
                # 整段恰好就是一条术语：译文直接是指定译法，必然「生效」。
                # 标题词表里的内置条目也照记——term_count 本来就把它们
                # 一并计数，报告口径与统计口径保持一致。
                self._record_term_hit(index + 1, normalized, term, True)
                resolved += 1
                continue
            if not should_translate(normalized):
                results[index] = source
                self.skipped_count += 1
                resolved += 1
                continue
            # 翻译记忆查询在缓存之前（理由见 translate_texts 里的同款注释）。
            if self.tm is not None:
                remembered = self.tm.match(normalized)
                if remembered is not None:
                    results[index] = remembered.target
                    if remembered.ratio >= 1.0:
                        self.tm_exact += 1
                    else:
                        # 模糊命中：用归用，但**绝不回写记忆**——近似句之间
                        # 的差异往往正是关键信息，复印出去等于把一处疑虑
                        # 撒到全文（见 core/tm.py 的取舍说明）。
                        self.tm_fuzzy += 1
                        self.tm_fuzzy_hits.append(
                            fuzzy_hit(index + 1, normalized, remembered)
                        )
                    resolved += 1
                    continue
            cached, _ = self._cache_lookup(normalized)
            if cached is not None:
                results[index] = cached
                self.cache_hits += 1
                resolved += 1
                continue
            pending.append(index)

        if pending:
            if not self._preflighted:
                self.preflight()
            if not self.edge_disabled:
                pending, resolved = self._batch_pass(
                    pending,
                    texts,
                    results,
                    self.edge,
                    "edge",
                    self._edge_limits,
                    resolved,
                    progress,
                )
            if pending and not self.bing_disabled:
                pending, resolved = self._batch_pass(
                    pending,
                    texts,
                    results,
                    self.bing,
                    "bing",
                    self._bing_limits,
                    resolved,
                    progress,
                )
            resolved = self._translate_pending(
                pending, texts, results, resolved, total, progress
            )

        # 混排补译放在进度报满之前：否则界面已经显示 100%，补译还在跑。
        mixed.fill_for_engine(
            self,
            texts,
            results,
            lambda spans: self.translate_texts(spans, ctx=self._active_ctx),
        )
        if progress is not None:
            progress(total, total, "")
        return [
            results[index] if results[index] is not None else texts[index]
            for index in range(total)
        ]

    def _translate_pending(
        self,
        pending: list[int],
        texts: list[str],
        results: list[str | None],
        resolved: int,
        total: int,
        progress: Callable[[int, int, str], None] | None,
    ) -> int:
        """逐段兜底翻译的入口：并发度 1 走串行（与改动前同构），>1 走并发。

        并发路径里 **worker 只做取回结果这一件纯活**，写结果 / 写缓存 /
        加计数 / 推进度全部在主线程按索引顺序做——这是「结果顺序严格对齐
        原文顺序」不变量的物理保证（详见 core/concurrency.py 模块说明）。
        """
        if self.jobs <= 1 or len(pending) <= 1:
            return self._translate_pending_serial(
                pending, texts, results, resolved, total, progress
            )
        return self._translate_pending_parallel(
            pending, texts, results, resolved, total, progress
        )

    def _translate_pending_serial(
        self,
        pending: list[int],
        texts: list[str],
        results: list[str | None],
        resolved: int,
        total: int,
        progress: Callable[[int, int, str], None] | None,
    ) -> int:
        """串行兜底：与改动前的 for 循环逐行同构，零回归。"""
        for index in pending:
            check_cancel(self.stop_event)
            source = normalize_source(texts[index])
            translated, ok, used_engine = self._translate_one(source, index=index)
            results[index] = translated
            if ok and used_engine:
                self.cache.set(used_engine, source, translated)
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")
        return resolved

    def _translate_pending_parallel(
        self,
        pending: list[int],
        texts: list[str],
        results: list[str | None],
        resolved: int,
        total: int,
        progress: Callable[[int, int, str], None] | None,
    ) -> int:
        """并发兜底：免费云端档，上限 2 且全局令牌桶限速。"""
        governor = self.governor
        governor.announce()

        def worker(_position: int, index: int) -> tuple[str, str, bool, str]:
            # 限速是全局的：几个线程抢同一个桶，不会越过每秒的请求上限。
            self._bucket.acquire(stop_event=self.stop_event)
            source = normalize_source(texts[index])
            translated, ok, used_engine = self._translate_one(source, index=index)
            return source, translated, ok, used_engine

        def on_done(position: int, value: object) -> None:
            nonlocal resolved
            if value is None:  # 出错了：on_error 已经出声并如实计过失败
                return
            index = pending[position]
            source, translated, ok, used_engine = value  # type: ignore[misc]
            results[index] = translated
            if ok and used_engine:
                self.cache.set(used_engine, source, translated)
            # 状态码**无论成败都要取走并清零**：它是「上一段最后那次失败」
            # 的残留，留着会把下一段一个完全没有状态码的失败归因成上一次的
            # 429，让本来只是偶发的错误被判成「服务端限流」而立刻降级。
            with self._counter_lock:
                status = self._last_status
                self._last_status = 0
            if ok:
                governor.note_success()
            else:
                # 失败段由 _translate_one_raw 自己计进 failed_count；
                # 这里只负责把状态码交给并发管家判断该不该降级（429 一次就降）。
                governor.note_failure(
                    TransientError("翻译通道失败", status) if status else None
                )
            self.jobs = governor.jobs
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")

        def on_error(position: int, exc: BaseException) -> None:
            nonlocal resolved
            index = pending[position]
            self.failed_count += 1
            self._failure_logs += 1
            self.log(
                f"第 {index + 1} 段翻译失败（{type(exc).__name__}），保留原文"
            )
            governor.note_failure(exc)
            self.jobs = governor.jobs
            resolved += 1
            if progress is not None:
                progress(min(resolved, total), total, "")

        run_ordered(
            pending,
            worker,
            jobs=self.jobs,
            stop_event=self.stop_event,
            on_done=on_done,
            on_error=on_error,
        )
        return resolved

    def _translate_one(
        self,
        text: str,
        ctx: TranslationContext | None = None,
        index: int | None = None,
    ) -> tuple[str, bool, str]:
        """单段翻译。用户术语先锁成占位符，译完换回官方译法再按档位润色。

        ``index`` 是 ``translate_texts`` 里的段号（0 起）。给了才记术语
        命中——直接调用（测试、命令行）没有段号上下文，不记，行为与之前
        完全一致。
        """
        if ctx is None:
            ctx = self._active_ctx
        hits: list[tuple[str, str, str]] = []
        masked, mapping = lock_and_mask(text, hits)
        translated, ok, engine = self._translate_one_raw(masked, ctx)
        if mapping:
            translated = unlock_and_restore(translated, mapping)
        final = finish_text(translated, self.style)
        if hits and index is not None:
            self.term_hits.extend(hit_rows(index + 1, hits, final))
        return final, ok, engine

    def _translate_one_raw(
        self, text: str, ctx: TranslationContext | None = None
    ) -> tuple[str, bool, str]:
        # A channel that answers with the source is not broken, it just gave up
        # on this segment; let the next channel try instead of shipping the
        # English through as if it were a translation.
        #
        # 并发改动后的新约束：网络调用（call_one）绝不加锁——那是并发的全部
        # 意义；但计数与熔断状态的「读—改—写」必须加锁。``count += 1`` 在
        # GIL 下也不是原子的，丢一次更新就让 summary() 说谎。
        if not self.edge_disabled:
            try:
                translated = call_one(self.edge, text, ctx)
            except CancelledError:
                raise
            except Exception as exc:
                warned = False
                disabled = False
                with self._counter_lock:
                    self._edge_failures_in_row += 1
                    self._last_status = status_of(exc) or self._last_status
                    if not self._edge_warned:
                        self._edge_warned = True
                        warned = True
                    if self._edge_failures_in_row >= 3:
                        self.edge_disabled = True
                        disabled = True
                if warned:
                    self.log(f"Edge 通道暂时不可用（{type(exc).__name__}），改用其他通道")
                if disabled:
                    self.log("Edge 通道连续失败，后续内容改用其他通道")
            else:
                with self._counter_lock:
                    self._edge_failures_in_row = 0
                if normalize_source(translated) != text:
                    with self._counter_lock:
                        self.translated_count += 1
                    return translated, True, "edge"
        if not self.bing_disabled:
            try:
                translated = call_one(self.bing, text, ctx)
            except CancelledError:
                raise
            except Exception as exc:
                warned = False
                disabled = False
                with self._counter_lock:
                    self._bing_failures_in_row += 1
                    self._last_status = status_of(exc) or self._last_status
                    if not self._bing_warned:
                        self._bing_warned = True
                        warned = True
                    if self._bing_failures_in_row >= 3:
                        self.bing_disabled = True
                        disabled = True
                if warned:
                    self.log(f"微软通道暂时不可用（{type(exc).__name__}），改用备用通道")
                if disabled:
                    self.log("微软通道连续失败，后续内容改用备用通道")
            else:
                with self._counter_lock:
                    self._bing_failures_in_row = 0
                if normalize_source(translated) != text:
                    with self._counter_lock:
                        self.translated_count += 1
                    return translated, True, "bing"
        try:
            translated = call_one(self.mymemory, text, ctx)
        except CancelledError:
            raise
        except Exception as exc:
            with self._counter_lock:
                self.failed_count += 1
                self._failure_logs += 1
                self._last_status = status_of(exc) or self._last_status
                logs = self._failure_logs
            if logs <= 5 or logs % 25 == 0:
                self.log(
                    f"第 {logs} 段翻译失败（{type(exc).__name__}），保留原文"
                )
            return text, False, ""
        if normalize_source(translated) != text:
            with self._counter_lock:
                self.translated_count += 1
            return translated, True, "mymemory"
        with self._counter_lock:
            self.echo_count += 1
        return text, True, ""
