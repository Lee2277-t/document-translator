"""自托管（单位内网/本机）翻译服务的配置与端点安全判定。

这一档解决的是「有 GPU 服务器但不在本机 Ollama 上」这一类场景：vLLM、
llama.cpp 的 ``llama-server``、LM Studio，或另一台机器上的 Ollama，都提供
OpenAI 兼容的 ``/v1/chat/completions``，所以翻译本体直接复用
``core.local_llm.LocalLLMTranslator``（它本来就吃 ``base_url``），本模块只管三件事：

1. **配置读写**——落在程序目录的 ``自定义引擎.json``。这里**不存 key**：
   需要 bearer key 的服务走 ``api_keys.local.json`` 的 ``custom.key``，
   与 deepl 同一个「永不进 git」的文件，避免两个地方各存一半秘密。
2. **端点口径判定**——把用户填的 URL 归一（补协议、去尾斜杠），并判断这台
   机器到底在**本机 / 内网 / 公网**。这个分类是合规判断的根据：公网端点
   等同「文档出境」，必须过与付费档同样的确认闸门；明文 HTTP 的公网端点
   直接拒绝——文档原文会在互联网上裸奔。
3. **模型名探测**——自托管服务通常只挂一个模型，且名字由部署方自己起
   （``/v1/models`` 返回什么就是什么）。用户不填模型时从服务上抓，否则
   ``LocalLLMTranslator`` 会拿默认的 qwen2.5:7b 去匹配，探不到模型就静默
   回退免费云端，用户完全看不出自己配置的档位其实没生效。

保密模式（``--offline``）下这一档整体不可用：端点是用户填的任意网络地址，
没法保证它不出本机；``--offline`` 的硬承诺是「只用 ``models/`` 里的模型」，
所以由调用方（``app.py``）在入口直接拒绝，不在此处判断。
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import TranslationError, app_root

#: 配置文件名（程序目录，UTF-8，允许中文服务名）。
CONFIG_FILE: Path = app_root() / "自定义引擎.json"

#: api_keys.local.json 里存放自托管服务 bearer key 的 provider 名。
CUSTOM_PROVIDER = "custom"

#: 端点分类 → 界面与日志里念给用户听的「数据去哪儿」。
SCOPE_TEXT = {
    "local": "只到本机",
    "lan": "只到单位内网",
    "public": "发往公网服务器",
}

_URL_HEAD_RE = re.compile(r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)?(?P<rest>.*)$")
# 主机名两种形态：方括号包起来的 IPv6（``[::1]``，里面全是冒号），
# 以及不含冒号的普通域名/IP。少了前一支，http://[::1]:8000/v1 会被当成「看不出主机名」。
_AUTHORITY_RE = re.compile(r"^(?P<host>\[[^\]]+\]|[^/:?#]+)(?::\d+)?(?:[/?#]|$)")


def config_path() -> str:
    return str(CONFIG_FILE)


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------


@dataclass
class CustomEngineConfig:
    """一条自托管服务的配置。``model`` 留空表示「从服务的 /v1/models 里抓」。"""

    enabled: bool = False
    name: str = "单位自托管服务"
    base_url: str = ""
    model: str = ""
    #: 探测到的可用模型缓存下来，避免每次翻译都发一次 GET /models。
    discovered: list[str] = field(default_factory=list)

    def usable(self) -> bool:
        return self.enabled and bool(self.base_url.strip())

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "discovered": list(self.discovered),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CustomEngineConfig":
        return cls(
            enabled=bool(data.get("enabled")),
            name=str(data.get("name") or "单位自托管服务"),
            base_url=str(data.get("base_url") or "").strip(),
            model=str(data.get("model") or "").strip(),
            discovered=[str(x) for x in (data.get("discovered") or [])],
        )


def load_config(path: Path | None = None) -> CustomEngineConfig:
    """读配置。文件不存在 / 内容坏了都返回「未启用」的空配置——
    配置缺失不是故障，界面照常起，只是这一档没打开。"""
    target = path or CONFIG_FILE
    if not target.is_file():
        return CustomEngineConfig()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CustomEngineConfig()
    if not isinstance(data, dict):
        return CustomEngineConfig()
    try:
        config = CustomEngineConfig.from_dict(data)
    except (TypeError, ValueError):
        return CustomEngineConfig()
    if config.base_url:
        # 历史配置里的写法可能不规范（少了协议、带了尾斜杠），读回来就归一一次。
        # 彻底不能解析的 URL 视为未启用，而不是让翻译中途炸。
        try:
            config.base_url = normalize_base_url(config.base_url)
        except TranslationError:
            return CustomEngineConfig()
    return config


def save_config(config: CustomEngineConfig, path: Path | None = None) -> Path:
    """写配置。URL 在这里校验——坏配置宁可写不进去，也不要留在盘上让
    下一次启动读到半截。"""
    target = path or CONFIG_FILE
    config.base_url = normalize_base_url(config.base_url) if config.base_url else ""
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    target.write_text(payload + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# URL 归一与端点判定
# ---------------------------------------------------------------------------


def normalize_base_url(raw: str) -> str:
    """把用户填的地址归一成 ``http(s)://host[:port][/路径]``，去掉尾斜杠。

    容忍三种日常写法：``localhost:8080/v1``（没写协议）、``http://x/v1/``
    （带尾斜杠）、``192.168.1.20:8000``（只有 IP 和端口）。协议漏填时按
    http 补——但补完之后是公网还是拒绝，由 :func:`require_usable` 判定。
    """
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise TranslationError(
            "自托管服务地址没填。填形如 http://192.168.1.20:8000/v1 的地址"
            "（vLLM、llama.cpp 的 llama-server、LM Studio、另一台机器上的 Ollama 都可以）。"
        )
    head = _URL_HEAD_RE.match(text)
    scheme = (head.group("scheme") or "").rstrip("://").lower() if head else ""
    rest = head.group("rest") if head else text
    if scheme and scheme not in ("http", "https"):
        raise TranslationError(
            f"不支持的协议 {scheme}://。自托管服务地址只能是 http:// 或 https:// 开头。"
        )
    if not scheme:
        scheme = "http"
    if "@" in rest.split("/")[0]:
        raise TranslationError(
            "地址里不要写账号密码（user:pass@host）。密钥请放到 api_keys.local.json 的 custom.key。"
        )
    authority = _AUTHORITY_RE.match(rest)
    if not authority:
        raise TranslationError(f"这个地址看不出主机名：{text}")
    host = authority.group("host").strip()
    if not host or ".." in host:
        raise TranslationError(f"这个地址的主机名不合法：{text}")
    return f"{scheme}://{rest}".rstrip("/")


def endpoint_host(base_url: str) -> str:
    """从已归一的地址里取主机名（小写，去掉方括号）。取不出来的话返回空串。"""
    match = _AUTHORITY_RE.match((base_url or "").split("://", 1)[-1])
    if not match:
        return ""
    return match.group("host").strip().strip("[]").lower()


def endpoint_scope(base_url: str) -> str:
    """返回 ``local`` / ``lan`` / ``public``：这台服务器相对用户在哪。

    判定只看地址字面，不做 DNS 解析——所以 ``myserver.corp.com``（内网域名
    但带点）会被算成公网。这不是漏洞而是**保守方向**：算成公网就多问一次
    确认，算成本机则可能悄悄把文档送出边界。内网域名请写成 IP。
    """
    host = endpoint_host(base_url)
    if not host:
        return "public"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # 不是字面 IP：按名字判本机/内网
        if host == "localhost" or host.endswith(".localhost"):
            return "local"
        if host.endswith((".local", ".internal", ".intra")):
            return "lan"
        if "." not in host and ":" not in host:
            # 单机容器/内网 NetBIOS 名（如 ``mt-server``、``1a2b3c``）
            return "lan"
        return "public"
    if address.is_loopback:
        return "local"
    if address.is_private or address.is_link_local:
        return "lan"
    return "public"


def is_private_endpoint(base_url: str) -> bool:
    return endpoint_scope(base_url) in ("local", "lan")


def require_usable(base_url: str) -> str:
    """校验「这个地址现在可以被使用」，返回归一后的地址；不合格抛人话错误。

    唯一的硬红线：**公网 + 明文 HTTP** 直接拒。自托管服务在外网暴露时
    套一层 https（Caddy 自动证书、反向代理都行），不要求用户接受明文出境。
    """
    url = normalize_base_url(base_url)
    if endpoint_scope(url) == "public" and url.startswith("http://"):
        raise TranslationError(
            f"这个地址在公网且是明文 HTTP：{url}\n"
            "文档原文会不加密地在互联网上跑一遍，任何一跳都能看。\n"
            "请改用 https:// 地址；若服务只在内网，请把地址写成内网 IP（如 192.168.x.x）。"
        )
    return url


def describe(config: CustomEngineConfig) -> str:
    """一句「数据去哪儿」的说明，界面日志与确认弹窗共用。"""
    if not config.base_url:
        return "自托管引擎：还没配置服务地址"
    scope = SCOPE_TEXT[endpoint_scope(config.base_url)]
    model = config.model or "自动从服务读取模型名"
    return f"自托管引擎（{config.name}）：{config.base_url}，模型 {model}，译文请求{scope}"


# ---------------------------------------------------------------------------
# 模型名探测
# ---------------------------------------------------------------------------


def discover_models(base_url: str, timeout: float = 3.0) -> list[str]:
    """GET ``{base_url}/models`` 拿服务上挂的模型名（OpenAI 兼容服务的标准入口）。

    失败不抛错，返回空列表：调用方的语义是「能不能替用户填模型名」，
    探不到就让用户自己写，不该把配置界面搞成一崩到底。
    """
    import requests

    url = normalize_base_url(base_url)
    try:
        response = requests.get(f"{url}/models", timeout=timeout)
        if response.status_code != 200:
            return []
        data = response.json()
    except Exception:
        return []
    items = data.get("data") if isinstance(data, dict) else None
    names: list[str] = []
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            names.append(str(item["id"]))
    return names


def pick_model(config: CustomEngineConfig, models: list[str]) -> str:
    """决定实际送进翻译器的模型名：用户填的最大，其次是服务只挂一个时直接用它。

    多个模型且用户没填时返回空串，让上层去问用户——猜一个模型名的后果是
    「配了 A 服务实际在调 B 模型」，比报错难查。
    """
    if config.model.strip():
        return config.model.strip()
    if len(models) == 1:
        return models[0]
    return ""
