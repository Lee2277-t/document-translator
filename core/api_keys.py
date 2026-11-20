"""付费 API 的 key 存放与用量记账（C3-a）。

为什么 key 放在 ``api_keys.local.json`` 而不是环境变量或硬编码
--------------------------------------------------------------
环境变量在 Windows 批处理场景对最终用户不友好（要改系统属性、要重开终端，
而且免安装包根本没有"配环境变量"的入口）；硬编码会进 git，key 随仓库
永远流传。本地 json + .gitignore 排除是「明确不入库 + 用户可手改」的平衡点：
用户用记事本就能改，程序用 ``load_key`` 读，读不到就是没配。

计费为什么双口径
----------------
本地流水（``cache/paid_usage.jsonl``）可能因漏记少算（进程被杀、磁盘满），
而请求里带 ``show_billed_characters=true`` 拿到的 ``billed_characters``
才是 DeepL 真收的钱。两个口径都记、都展示，用户对账时以官方口径为准。
用量查询 ``GET /v2/usage`` 返回的是 DeepL 账户页的真实月度用量，
由 ``core/deepl.py`` 在报告时取用，失败只降级不报错。

本模块只做「读 key / 记流水 / 首次确认标记」，不发任何网络请求。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .common import app_root, data_root

#: key 配置文件。.gitignore 已排除，永远不进库；与 术语表.txt 一样放在
#: 程序根目录，用户看得见、改得了、删得掉。
KEYS_FILE: Path = app_root() / "api_keys.local.json"

#: 付费引擎用量流水（JSONL，一行一次成功请求）。cache/ 已在 .gitignore，
#: 且付费档本来就联网，写盘没有保密问题。
USAGE_FILE: Path = data_root() / "cache" / "paid_usage.jsonl"

#: DeepL key 的两种形态：Pro 是一串 UUID；Free 在 UUID 后加 ``:fx`` 后缀。
#: 域名选择（api.deepl.com / api-free.deepl.com）就靠这个后缀判断。
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: 首次使用确认标记写在 key 文件的 provider 条目下（``deepl.acknowledged``）。
ACKNOWLEDGED_KEY = "acknowledged"


def _read_all() -> dict[str, Any]:
    """读整个 key 文件。不存在 / 坏 JSON / 不是对象都返回空 dict，不抛。"""
    try:
        raw = KEYS_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_entry(provider: str) -> dict[str, Any] | None:
    entry = _read_all().get(provider)
    return entry if isinstance(entry, dict) else None


def looks_like_key(key: str) -> bool:
    """key 形态校验：``:fx`` 结尾的 Free key 或裸 UUID 的 Pro key。

    空 key 当没配（返回 False）。形态不对时提示文案要说明两种形态，
    否则用户拿着网页上复制的整段（带引号、带空格）来问为什么不能用。
    """
    text = (key or "").strip()
    if not text:
        return False
    if text.endswith(":fx"):
        return UUID_RE.match(text[:-3]) is not None
    return UUID_RE.match(text) is not None


def load_key(provider: str) -> dict | None:
    """读取某个付费引擎的配置，返回 ``{"key": ..., "monthly_char_budget": ...}``。

    文件不存在 / JSON 坏 / 没有该 provider / key 为空，全部返回 None——
    调用方（CLI 闸门、DeepLTranslator）只需要区分「有配置」和「没配置」，
    不值得为四种失败各写一套异常。``monthly_char_budget`` 缺省 0 = 不设
    本地预算上限（用户可能买了 Pro，本地不该替云端拦）。
    """
    entry = _read_entry(provider)
    if entry is None:
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    try:
        budget = int(entry.get("monthly_char_budget") or 0)
    except (TypeError, ValueError):
        budget = 0
    return {"key": key, "monthly_char_budget": max(0, budget)}


def save_acknowledgement(provider: str = "deepl") -> None:
    """写入首次使用确认标记（``provider.acknowledged = true``）。

    v1.5.0 的 CLI 在用户显式给出 ``--yes-i-know`` 后调用本函数；
    v1.5.1 的 GUI 确认对话框复用同一标记。写盘失败静默——确认标记只是
    省一次警示，不该让翻译失败。
    """
    data = _read_all()
    entry = data.get(provider)
    if not isinstance(entry, dict):
        entry = {}
        data[provider] = entry
    entry[ACKNOWLEDGED_KEY] = True
    try:
        KEYS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


def is_acknowledged(provider: str = "deepl") -> bool:
    """是否已确认过付费档涉密警示。没配 key 时也算未确认（返回 False）。"""
    entry = _read_entry(provider)
    return bool(entry and entry.get(ACKNOWLEDGED_KEY))


def record_usage(engine: str, chars: int, billed: int, file: str = "") -> None:
    """追加一行用量流水（每次成功请求一行）。

    ``chars`` 是本次送检字符数（本地口径），``billed`` 是 DeepL 响应里的
    ``billed_characters``（真实计费口径）。写盘失败静默：记账是给用户对账
    用的辅助物，不能因为写不进去让翻译整体失败。
    """
    now = datetime.now()
    row = {
        "ts": now.isoformat(timespec="seconds"),
        "engine": str(engine),
        "chars": int(chars),
        "billed": int(billed),
        "file": str(file or ""),
        "month": now.strftime("%Y-%m"),
    }
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(USAGE_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def month_usage(engine: str, month: str) -> dict:
    """汇总某引擎某月的本地流水，返回 ``{"chars": N, "billed": M, "calls": K}``。

    文件不存在 / 某行坏 JSON 都跳过——流水是追加写的，坏行只影响那一行，
    不该让整份对账表读不出来。
    """
    totals = {"chars": 0, "billed": 0, "calls": 0}
    try:
        with open(USAGE_FILE, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("engine") != engine or row.get("month") != month:
                    continue
                try:
                    totals["chars"] += int(row.get("chars") or 0)
                    totals["billed"] += int(row.get("billed") or 0)
                except (TypeError, ValueError):
                    continue
                totals["calls"] += 1
    except OSError:
        pass
    return totals


def usage_file() -> str:
    """流水文件路径（测试与报告展示用）。"""
    return str(USAGE_FILE)


def keys_file() -> str:
    """key 文件路径（CLI 引导文案用）。"""
    return str(KEYS_FILE)
