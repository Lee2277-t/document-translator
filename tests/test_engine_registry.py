"""引擎登记表（A2）与自托管档（F8/X2）的行为钉子。

覆盖四类最容易出事的地方：

1. **档位清单的唯一真相**：界面单选、CLI 校验、模式文字都从
   ``core.engine_registry`` 里长出来，所以断言的是「表 ↔ 界面 ↔ CLI」三者
   对得上，而不是各测各的。
2. **保密模式口径**：联网四档在 ``--offline`` 下必须全灭（可见性 + CLI 拒绝），
   这是合规红线；``--local-llm`` 是唯一「降级但不报错」的组合，必须出声。
3. **端点安全判定**：本机 / 内网 / 公网三分、公网明文 HTTP 直接拒、
   公网要确认、内网不拦——判定错了就是「以为数据没出单位其实出了」。
4. **构造路由**：选了哪档就必须构造哪个类，参数（缓存、记忆、并发、质量、
   base_url、模型名）不能接错。类在函数体内 import，所以 patch 模块属性
   就能断言「用了对的构造器」。
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import app
import core.custom_engine as custom_engine
import core.engine_registry as registry
from core.common import TranslationError

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


# ---------------------------------------------------------------------------
# 夹具：把配置文件与 api_keys 的确认标记都钉到 tmp 里
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_file(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "自定义引擎.json"
    monkeypatch.setattr(custom_engine, "CONFIG_FILE", target)
    return target


@pytest.fixture()
def fake_translators(monkeypatch):
    """把四档翻译器换成只记参数的假类，断言「选了哪档就构造哪档」。"""
    created: dict[str, dict] = {}

    class _Fake:
        label = "fake"

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created[self.kind] = kwargs

        def translate_texts(self, texts):
            return ["译文"] * len(texts)

    class FakeCloud(_Fake):
        kind = "cloud"

    class FakeLocalLLM(_Fake):
        kind = "local_llm"

    class FakeDeepL(_Fake):
        kind = "deepl"

    class FakeOffline(_Fake):
        kind = "offline"

    import core.deepl as deepl_module
    import core.local_llm as llm_module
    import core.local_translator as local_module
    import core.translator as translator_module

    monkeypatch.setattr(translator_module, "Translator", FakeCloud)
    monkeypatch.setattr(llm_module, "LocalLLMTranslator", FakeLocalLLM)
    monkeypatch.setattr(deepl_module, "DeepLTranslator", FakeDeepL)
    monkeypatch.setattr(local_module, "LocalTranslator", FakeOffline)
    return created


def _params(**overrides):
    kwargs = dict(log=lambda message: None, style="standard", jobs=1)
    kwargs.update(overrides)
    return registry.EngineParams(**kwargs)


# ---------------------------------------------------------------------------
# 1. 档位清单
# ---------------------------------------------------------------------------


def test_radio_choices_are_four_online_tiers() -> None:
    assert registry.radio_choices(False) == [
        ("cloud", "免费云端"),
        ("local-llm", "本地大模型（更强）"),
        ("paid", "付费 API"),
        ("custom", "自托管服务"),
    ]


def test_radio_choices_empty_in_confidential_mode() -> None:
    """保密模式没有「换一档」这回事：渲染四选一等于邀请用户违规。"""
    assert registry.radio_choices(True) == []
    assert registry.visible_keys(True) == [registry.OFFLINE]


def test_no_online_tier_is_visible_offline() -> None:
    """登记表自身的合规不变量：除保密档外，任何一档都不允许在离线模式存在。"""
    offenders = [
        spec.key
        for spec in registry.SPECS.values()
        if spec.key != registry.OFFLINE and spec.offline_visible
    ]
    assert offenders == []


def test_every_tier_has_mode_text_and_short_mode() -> None:
    for key in registry.SPECS:
        assert registry.mode_text(key)
        assert registry.SHORT_MODES[key]


def test_unknown_tier_lists_available_ones() -> None:
    with pytest.raises(TranslationError) as exc:
        registry.get("google")
    assert "未知的翻译引擎档位：google" in str(exc.value)
    assert "cloud" in str(exc.value) and "custom" in str(exc.value)


# ---------------------------------------------------------------------------
# 2. CLI 档位合成
# ---------------------------------------------------------------------------


def test_resolve_cli_defaults_to_cloud() -> None:
    assert registry.resolve_cli() == "cloud"


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"paid": "deepl"}, "paid"),
        ({"engine": "local-llm"}, "local-llm"),
        ({"custom": True}, "custom"),
        ({"offline": True}, "offline"),
    ],
)
def test_resolve_cli_single_switch(kwargs, expected) -> None:
    assert registry.resolve_cli(**kwargs) == expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"paid": "deepl", "engine": "local-llm"},
        {"paid": "deepl", "custom": True},
        {"engine": "local-llm", "custom": True},
        {"paid": "deepl", "engine": "local-llm", "custom": True},
    ],
)
def test_conflicting_switches_are_rejected(kwargs) -> None:
    """v1.6.2 之前是「写在后面的赢」，交接稿记为已知小缺口；现在一律报错。"""
    with pytest.raises(TranslationError) as exc:
        registry.resolve_cli(**kwargs)
    assert "只能给一个" in str(exc.value)


def test_resolve_cli_rejects_unknown_paid() -> None:
    with pytest.raises(TranslationError) as exc:
        registry.resolve_cli(paid="chatgpt")
    assert "--paid=deepl" in str(exc.value)


@pytest.mark.parametrize("kwargs", [{"paid": "deepl"}, {"custom": True}])
def test_offline_rejects_tiers_that_send_text_over_network(kwargs) -> None:
    with pytest.raises(TranslationError) as exc:
        registry.resolve_cli(offline=True, **kwargs)
    assert "保密模式" in str(exc.value)


def test_offline_downgrades_local_llm_with_notice() -> None:
    """本机 Ollama 不构成出境，但静默换成小模型会让用户以为还在用大模型。"""
    notices: list[str] = []
    key = registry.resolve_cli(
        offline=True, engine="local-llm", notice=notices.append
    )
    assert key == "offline"
    assert len(notices) == 1 and "已忽略 --local-llm" in notices[0]


# ---------------------------------------------------------------------------
# 3. 构造路由
# ---------------------------------------------------------------------------


def test_build_cloud_passes_cache_tm_jobs(fake_translators) -> None:
    tm = object()
    registry.build("cloud", _params(cache_path="c.sqlite", tm=tm, jobs=3))
    kwargs = fake_translators["cloud"]
    assert kwargs["cache_path"] == "c.sqlite"
    assert kwargs["tm"] is tm and kwargs["jobs"] == 3


def test_build_local_llm_keeps_fallback_and_quality(fake_translators) -> None:
    registry.build("local-llm", _params(quality=False, model="qwen2.5:7b"))
    kwargs = fake_translators["local_llm"]
    assert kwargs["fallback"] == "local"
    assert kwargs["quality"] is False and kwargs["model"] == "qwen2.5:7b"


def test_build_offline_announces_no_tm(fake_translators) -> None:
    logs: list[str] = []
    registry.build("offline", _params(log=logs.append, tm=object()))
    assert "offline" in fake_translators
    assert "翻译记忆不参与" in logs[0]
    # 保密档根本不接 tm/cache 参数：落盘就是违规，不该有传错的可能
    assert "tm" not in fake_translators["offline"]


def test_build_paid_needs_key(monkeypatch) -> None:
    import core.api_keys as api_keys

    monkeypatch.setattr(api_keys, "load_key", lambda provider: None)
    with pytest.raises(TranslationError) as exc:
        registry.build("paid", _params())
    assert "未配置 DeepL key" in str(exc.value)


def test_build_paid_passes_cache_flag_style(fake_translators, monkeypatch) -> None:
    import core.api_keys as api_keys

    monkeypatch.setattr(api_keys, "load_key", lambda provider: {"key": "k"})
    registry.build("paid", _params(cache_path=None))
    # DeepLTranslator 用 cache=False 表示「不缓存」，与 Translator 的 None 同义
    assert fake_translators["deepl"]["cache"] is False


@pytest.mark.parametrize(
    "key,kind",
    [
        ("cloud", "cloud"),
        ("local-llm", "local_llm"),
        ("paid", "deepl"),
        ("offline", "offline"),
        ("custom", "local_llm"),
    ],
)
def test_stop_event_reaches_every_tier(
    key, kind, fake_translators, config_file, monkeypatch
) -> None:
    """「停止」按钮对每一档都得管用：stop_event 要由登记表一路透传到翻译器。

    这条是 A2 重构自己带出来的风险——以前散在 app.py 里各档各写一行，收拢成
    表以后少写一个参数就静默失效（点了停止但继续翻），而且不报错。按档逐个钉。
    """
    import threading

    import core.api_keys as api_keys
    import core.custom_engine as ce

    monkeypatch.setattr(api_keys, "load_key", lambda provider: {"key": "k"})
    monkeypatch.setattr(ce, "discover_models", lambda url, timeout=3.0: [])
    config_file.write_text(
        json.dumps(
            {
                "enabled": True,
                "base_url": "http://127.0.0.1:8000/v1",
                "model": "unit-model",
            }
        ),
        encoding="utf-8",
    )
    stop = threading.Event()
    registry.build(key, _params(stop_event=stop))
    assert fake_translators[kind]["stop_event"] is stop


def test_build_custom_without_config_says_where_to_configure(
    fake_translators, config_file
) -> None:
    with pytest.raises(TranslationError) as exc:
        registry.build("custom", _params())
    assert "配置自托管" in str(exc.value)
    assert "local_llm" not in fake_translators


def test_build_custom_uses_configured_endpoint_and_single_model(
    fake_translators, config_file, monkeypatch
) -> None:
    config_file.write_text(
        json.dumps(
            {
                "enabled": True,
                "name": "单位GPU服务器",
                "base_url": "http://192.168.1.20:8000/v1",
                "model": "",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        custom_engine, "discover_models", lambda url, timeout=3.0: ["qwen3:32b"]
    )
    logs: list[str] = []
    registry.build("custom", _params(log=logs.append))
    kwargs = fake_translators["local_llm"]
    assert kwargs["base_url"] == "http://192.168.1.20:8000/v1"
    assert kwargs["model"] == "qwen3:32b"
    assert any("自托管服务" in line for line in logs)
    # 探测到的模型名回写配置，省掉下次启动再问一遍服务
    assert json.loads(config_file.read_text(encoding="utf-8"))["discovered"] == [
        "qwen3:32b"
    ]


def test_build_custom_with_many_models_does_not_guess(
    config_file, fake_translators, monkeypatch
) -> None:
    config_file.write_text(
        json.dumps(
            {"enabled": True, "base_url": "http://10.0.0.5:8000/v1", "model": ""}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        custom_engine,
        "discover_models",
        lambda url, timeout=3.0: ["a", "b", "c"],
    )
    logs: list[str] = []
    registry.build("custom", _params(log=logs.append))
    assert fake_translators["local_llm"]["model"] is None
    assert any("没报告模型名" in line for line in logs)


def test_build_custom_command_line_url_does_not_overwrite_config(
    config_file, fake_translators, monkeypatch
) -> None:
    config_file.write_text(
        json.dumps({"enabled": True, "base_url": "http://127.0.0.1:11434/v1"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(custom_engine, "discover_models", lambda url, timeout=3.0: [])
    registry.build(
        "custom", _params(base_url="http://192.168.9.9:8000/v1", model="m")
    )
    kwargs = fake_translators["local_llm"]
    assert kwargs["base_url"] == "http://192.168.9.9:8000/v1"
    assert kwargs["model"] == "m"
    # 一次性试别的机器不该把配置文件里的地址改掉
    assert json.loads(config_file.read_text(encoding="utf-8"))["base_url"] == (
        "http://127.0.0.1:11434/v1"
    )


def test_build_custom_refuses_public_plain_http(config_file, fake_translators) -> None:
    config_file.write_text(
        json.dumps({"enabled": True, "base_url": "http://mt.example.com/v1"}),
        encoding="utf-8",
    )
    with pytest.raises(TranslationError) as exc:
        registry.build("custom", _params())
    assert "明文 HTTP" in str(exc.value)


def test_custom_fallback_follows_endpoint_scope(
    config_file, fake_translators, monkeypatch
) -> None:
    """回退方向跟着数据边界走：内网自托管服务连不上时绝不改发免费云端。

    选这一档的人要的就是「文档只到我们自己那台机器」，服务一挂就悄悄换成免费
    云端等于把这条承诺废掉；公网端点已经确认过可以出境，回退云端不算越界。
    """
    monkeypatch.setattr(custom_engine, "discover_models", lambda url, timeout=3.0: [])

    def build(base_url: str) -> dict:
        config_file.write_text(
            json.dumps({"enabled": True, "base_url": base_url}), encoding="utf-8"
        )
        fake_translators.clear()
        registry.build("custom", _params())
        return fake_translators["local_llm"]

    lan = build("http://192.168.1.20:8000/v1")
    assert lan["fallback"] == "local"
    assert lan["target_label"] == "单位自托管服务"
    assert "绝不会把原文改发到联网翻译服务" in lan["unavailable_hint"]
    assert build("http://127.0.0.1:11434/v1")["fallback"] == "local"
    public = build("https://mt.example.com/v1")
    assert public["fallback"] == "cloud"
    assert "这一档连的是公网" in public["unavailable_hint"]


# ---------------------------------------------------------------------------
# 4. 端点判定与配置读写
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("localhost:8080/v1", "http://localhost:8080/v1"),
        ("http://127.0.0.1:11434/v1/", "http://127.0.0.1:11434/v1"),
        ("https://mt.example.com/v1", "https://mt.example.com/v1"),
        ("  192.168.1.20:8000  ", "http://192.168.1.20:8000"),
    ],
)
def test_normalize_base_url(raw, expected) -> None:
    assert custom_engine.normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw,needle",
    [
        ("", "自托管服务地址没填"),
        ("ftp://a.example/x", "不支持的协议"),
        ("http://user:pass@10.0.0.1/v1", "账号密码"),
        ("::::", "看不出主机名"),
        ("http://a..b/v1", "主机名不合法"),
    ],
)
def test_normalize_base_url_rejects_junk(raw, needle) -> None:
    with pytest.raises(TranslationError) as exc:
        custom_engine.normalize_base_url(raw)
    assert needle in str(exc.value)


@pytest.mark.parametrize(
    "url,scope",
    [
        ("http://127.0.0.1:11434/v1", "local"),
        ("http://localhost:11434/v1", "local"),
        ("http://[::1]:8000/v1", "local"),
        ("http://192.168.1.20:8000/v1", "lan"),
        ("http://10.1.2.3:8000/v1", "lan"),
        ("http://172.16.5.6:8000/v1", "lan"),
        ("http://169.254.1.1:8000/v1", "lan"),
        ("http://mt-server:8000/v1", "lan"),
        ("http://gpu.internal:8000/v1", "lan"),
        ("http://8.8.8.8:8000/v1", "public"),
        ("http://172.32.0.1:8000/v1", "public"),
        ("https://mt.example.com/v1", "public"),
    ],
)
def test_endpoint_scope(url, scope) -> None:
    assert custom_engine.endpoint_scope(url) == scope


def test_public_plain_http_is_refused_https_is_allowed() -> None:
    with pytest.raises(TranslationError):
        custom_engine.require_usable("http://mt.example.com/v1")
    assert custom_engine.require_usable("https://mt.example.com/v1").endswith("/v1")
    # 内网明文照常允许：单位内网段不给每个自托管服务配证书是不现实的请求
    assert custom_engine.require_usable("http://192.168.1.20:8000/v1")


def test_describe_says_where_text_goes(config_file) -> None:
    config = custom_engine.CustomEngineConfig(
        enabled=True, name="GPU", base_url="http://192.168.1.20:8000/v1", model="q"
    )
    text = custom_engine.describe(config)
    assert "GPU" in text and "只到单位内网" in text and "q" in text
    empty = custom_engine.describe(custom_engine.CustomEngineConfig())
    assert "还没配置" in empty


def test_save_then_load_roundtrip_keeps_chinese_name(config_file) -> None:
    config = custom_engine.CustomEngineConfig(
        enabled=True, name="三号GPU机", base_url="192.168.1.20:8000/v1"
    )
    custom_engine.save_config(config)
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["name"] == "三号GPU机"  # 不能被转成 \uXXXX
    loaded = custom_engine.load_config()
    assert loaded.base_url == "http://192.168.1.20:8000/v1"
    assert loaded.usable() and loaded.name == "三号GPU机"


def test_corrupt_or_bad_config_degrades_to_disabled_instead_of_crashing(
    config_file,
) -> None:
    config_file.write_text("{坏掉的 json", encoding="utf-8")
    assert custom_engine.load_config().usable() is False
    config_file.write_text(
        json.dumps({"enabled": True, "base_url": "ftp://x/v1"}), encoding="utf-8"
    )
    assert custom_engine.load_config().usable() is False


def test_discover_models_returns_ids_and_swallows_errors(monkeypatch) -> None:
    class _Response:
        status_code = 200

        def json(self):
            return {"data": [{"id": "qwen3:32b"}, {"id": "deepseek-r1:14b"}, {"x": 1}]}

    class _Bad:
        status_code = 500

        def json(self):
            return {}

    responses = iter([_Response(), _Bad()])
    monkeypatch.setattr(
        custom_engine, "discover_models", custom_engine.discover_models
    )
    import requests

    monkeypatch.setattr(requests, "get", lambda url, timeout=None: next(responses))
    assert custom_engine.discover_models("http://10.0.0.1:8000/v1") == [
        "qwen3:32b",
        "deepseek-r1:14b",
    ]
    assert custom_engine.discover_models("http://10.0.0.1:8000/v1") == []

    def boom(url, timeout=None):
        raise OSError("连不上")

    monkeypatch.setattr(requests, "get", boom)
    assert custom_engine.discover_models("http://10.0.0.1:8000/v1") == []


@pytest.mark.parametrize(
    "model,models,expected",
    [
        ("user-picked", ["a", "b"], "user-picked"),
        ("", ["only-one"], "only-one"),
        ("", ["a", "b"], ""),
        ("", [], ""),
    ],
)
def test_pick_model(model, models, expected) -> None:
    config = custom_engine.CustomEngineConfig(model=model)
    assert custom_engine.pick_model(config, models) == expected


# ---------------------------------------------------------------------------
# 5. CLI 闸门（--custom 的三道闸）
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_gate_env(monkeypatch, config_file):
    """把档位开关与确认标记都换成可控的假实现，只测 _custom_gate 的判断。"""
    import core.api_keys as api_keys

    state = {"acknowledged": False, "saved": []}
    monkeypatch.setattr(app, "CLI_CUSTOM", False)
    monkeypatch.setattr(app, "CLI_CUSTOM_URL", "")
    monkeypatch.setattr(app, "CLI_YES_I_KNOW_CUSTOM", False)
    monkeypatch.setattr(app, "OFFLINE", False)
    monkeypatch.setattr(api_keys, "is_acknowledged", lambda provider="deepl": state["acknowledged"])
    monkeypatch.setattr(
        api_keys, "save_acknowledgement", lambda provider="deepl": state["saved"].append(provider)
    )
    return state


def _write_config(base_url: str, enabled: bool = True) -> None:
    custom_engine.save_config(
        custom_engine.CustomEngineConfig(enabled=enabled, base_url=base_url)
    )


def test_custom_gate_passes_when_no_flags(cli_gate_env) -> None:
    assert app._custom_gate() is None


def test_custom_gate_refuses_offline(cli_gate_env, capsys) -> None:
    monkeypatch_offline = True
    app.OFFLINE = monkeypatch_offline
    try:
        app.CLI_CUSTOM = True
        assert app._custom_gate() == 2
    finally:
        app.OFFLINE = False
    assert "保密模式不能用自托管服务" in capsys.readouterr().out


def test_custom_gate_without_config_prints_how_to_configure(
    cli_gate_env, capsys
) -> None:
    app.CLI_CUSTOM = True
    assert app._custom_gate() == 3
    out = capsys.readouterr().out
    assert "还没配置自托管服务" in out and "自定义引擎.json" in out


def test_custom_gate_rejects_public_plain_http(cli_gate_env, capsys) -> None:
    _write_config("http://mt.example.com/v1")
    app.CLI_CUSTOM = True
    assert app._custom_gate() == 3
    assert "明文 HTTP" in capsys.readouterr().out


def test_custom_gate_allows_lan_without_confirmation(cli_gate_env) -> None:
    _write_config("http://192.168.1.20:8000/v1")
    app.CLI_CUSTOM = True
    assert app._custom_gate() is None
    assert app.CLI_CUSTOM is True


def test_custom_gate_public_https_needs_explicit_acknowledgement(
    cli_gate_env, capsys
) -> None:
    _write_config("https://mt.example.com/v1")
    app.CLI_CUSTOM = True
    assert app._custom_gate() == 4
    out = capsys.readouterr().out
    assert "公网" in out and "--yes-i-know-custom" in out

    app.CLI_YES_I_KNOW_CUSTOM = True
    assert app._custom_gate() is None
    assert cli_gate_env["saved"] == ["custom"]


def test_custom_gate_public_https_skips_prompt_when_already_acknowledged(
    cli_gate_env,
) -> None:
    cli_gate_env["acknowledged"] = True
    _write_config("https://mt.example.com/v1")
    app.CLI_CUSTOM = True
    assert app._custom_gate() is None


def test_custom_url_alone_selects_the_tier(cli_gate_env) -> None:
    app.CLI_CUSTOM_URL = "http://10.0.0.7:8000/v1"
    assert app._custom_gate() is None
    assert app.CLI_CUSTOM is True


def test_cli_translator_builds_custom_tier_from_url(cli_gate_env, fake_translators, monkeypatch) -> None:
    """命令行只给 --custom-url= 也必须真的打到那台服务上（不是悄悄用云端）。"""
    monkeypatch.setattr(custom_engine, "discover_models", lambda url, timeout=3.0: [])
    app.CLI_CUSTOM_URL = "http://10.0.0.7:8000/v1"
    app.CLI_CUSTOM = True
    translator = app._build_cli_translator(lambda message: None)
    assert translator.kind == "local_llm"
    assert translator.kwargs["base_url"] == "http://10.0.0.7:8000/v1"


def test_cli_translator_still_honours_offline(fake_translators, monkeypatch) -> None:
    monkeypatch.setattr(app, "OFFLINE", True)
    translator = app._build_cli_translator(lambda message: None)
    assert translator.kind == "offline"


# ---------------------------------------------------------------------------
# 6. 界面接线（子进程真构造窗口：同进程反复建 Tk root 会抖）
# ---------------------------------------------------------------------------


def _run_gui_child(body: str, tmp: Path) -> list[str]:
    """在子进程里真建一次界面并执行 body（body 是已缩进的 Python 源码）。

    ``_engine_radios_of`` 定义在脚本头里而不是本模块：子进程 exec 的是拼出来的
    字符串，看不见 pytest 进程里的函数。``body`` 整段缩进四格是为了在本文件里
    好读，拼进脚本前 dedent 回顶格。
    """
    script = (
        "import sys, json\n"
        "from pathlib import Path\n"
        'sys.argv = ["app"]\n'
        "import app, tkinter as tk\n"
        "import core.custom_engine as custom_engine\n"
        "import core.api_keys as api_keys\n"
        "from unittest import mock\n"
        "def _engine_radios_of(application):\n"
        "    found = {}\n"
        "    target = str(application.engine_var)\n"
        "    def walk(widget):\n"
        "        for child in widget.winfo_children():\n"
        "            if isinstance(child, tk.Radiobutton) and str(child.cget('variable')) == target:\n"
        "                found[child.cget('value')] = child.cget('text')\n"
        "            walk(child)\n"
        "    walk(application.root)\n"
        "    return found\n"
        f'custom_engine.CONFIG_FILE = Path(r"{tmp}") / "自定义引擎.json"\n'
        "problems = []\n"
        + textwrap.dedent(body.strip("\n"))
        + "\nprint('\\n'.join(problems))\n"
    )
    result = subprocess.run(
        [str(PYTHON), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    return [line for line in result.stdout.splitlines() if line.strip()]


@pytest.fixture()
def gui_tmp(tmp_path: Path) -> Path:
    return tmp_path


_BODY_RENDER = r"""
    application = app.TranslatorApp()
    try:
        found = _engine_radios_of(application)
        if sorted(found) != ["cloud", "custom", "local-llm", "paid"]:
            problems.append("界面单选与登记表对不上: %r" % (found,))
        if found.get("custom") != "自托管服务":
            problems.append("自托管档文字不对: %r" % found.get("custom"))
        if not hasattr(application, "custom_config_link"):
            problems.append("没有「配置自托管…」入口")
        # 切到自托管档：没有配置 → 弹引导 + 弹回免费云端
        with mock.patch.object(app.messagebox, "showinfo") as guide:
            application.engine_var.set("custom")
            application._on_engine_change()
        if not guide.called:
            problems.append("未配置自托管服务却没弹引导")
        if application.engine_var.get() != "cloud":
            problems.append("闸门没过却没弹回云端: %r" % application.engine_var.get())
        # 配了内网地址：直接放行，右下角模式文字说清数据只到内网
        custom_engine.save_config(
            custom_engine.CustomEngineConfig(
                enabled=True, name="GPU", base_url="http://192.168.1.20:8000/v1"
            )
        )
        with mock.patch.object(app.messagebox, "askyesno") as ask, \
             mock.patch.object(app.messagebox, "showinfo") as guide:
            application.engine_var.set("custom")
            application._on_engine_change()
        if ask.called or guide.called:
            problems.append("内网地址不该弹任何窗")
        if application.engine_var.get() != "custom":
            problems.append("内网地址应放行: %r" % application.engine_var.get())
        registry_footer = app.engine_registry.get("custom").footer
        if application.mode_label.cget("text") != registry_footer:
            problems.append("右下角模式文字没跟随档位: %r" % application.mode_label.cget("text"))
        logs = "".join(str(item) for item in application.log_text.get("1.0", "end").splitlines())
        if "只到单位内网" not in logs:
            problems.append("日志没说清数据只到内网")
        # 质量引擎开关只在吃上下文的档位出现
        if application.quality_switch.winfo_manager() != "grid":
            problems.append("自托管档没显示质量引擎开关")
        application.engine_var.set("paid")
        with mock.patch.object(application, "_confirm_paid_engine", lambda: True):
            application._on_engine_change()
        if application.quality_switch.winfo_manager() == "grid":
            problems.append("付费档不该显示质量引擎开关")
    finally:
        application.root.destroy()
"""


def test_gui_custom_tier_rendering_and_gates(gui_tmp) -> None:
    problems = _run_gui_child(_BODY_RENDER, gui_tmp)
    assert problems == []


_BODY_PUBLIC = r"""
    application = app.TranslatorApp()
    try:
        state = {"ack": False, "saved": []}
        api_keys.is_acknowledged = lambda provider="deepl": state["ack"]
        api_keys.save_acknowledgement = lambda provider="deepl": state["saved"].append(provider)
        custom_engine.save_config(
            custom_engine.CustomEngineConfig(
                enabled=True, base_url="https://mt.example.com/v1"
            )
        )
        # 公网未确认 → 弹涉密警示；选否弹回云端，且不落标记
        with mock.patch.object(app.messagebox, "askyesno", lambda *a, **k: False):
            application.engine_var.set("custom")
            application._on_engine_change()
        if application.engine_var.get() != "cloud":
            problems.append("公网警示选否应弹回云端")
        if state["saved"]:
            problems.append("选否却落了确认标记")
        # 选是 → 放行并落 custom 的确认标记（与 deepl 分开记）
        with mock.patch.object(app.messagebox, "askyesno", lambda *a, **k: True):
            application.engine_var.set("custom")
            application._on_engine_change()
        if application.engine_var.get() != "custom":
            problems.append("公网警示选是应放行")
        if state["saved"] != ["custom"]:
            problems.append("确认标记没落在 custom: %r" % state["saved"])
        # 明文公网 → 没有「知道了就用」这个选项，直接错误
        custom_engine.save_config(
            custom_engine.CustomEngineConfig(
                enabled=True, base_url="http://mt.example.com/v1"
            )
        )
        with mock.patch.object(app.messagebox, "showerror") as showerror:
            application.engine_var.set("custom")
            application._on_engine_change()
        if not showerror.called:
            problems.append("公网明文 HTTP 没报错")
        if application.engine_var.get() != "cloud":
            problems.append("公网明文 HTTP 却放行了")
    finally:
        application.root.destroy()
"""


def test_gui_public_endpoint_confirmation_and_hard_refusal(gui_tmp) -> None:
    problems = _run_gui_child(_BODY_PUBLIC, gui_tmp)
    assert problems == []


_BODY_DIALOG = r"""
    application = app.TranslatorApp()
    try:
        dialog = app.CustomEngineDialog(application.root, application)
        # 地址一改，「数据去哪儿」就跟着改；公网明文直接标红且不放行
        dialog.url_var.set("http://192.168.1.30:8000/v1")
        dialog._refresh_hint()
        if "只到单位内网" not in dialog.scope_label.cget("text"):
            problems.append("内网地址的提示文字不对: %r" % dialog.scope_label.cget("text"))
        dialog.url_var.set("http://mt.example.com/v1")
        dialog._refresh_hint()
        if "不允许保存" not in dialog.scope_label.cget("text"):
            problems.append("公网明文没有被标红拒绝")
        dialog.url_var.set("ftp://a/v1")
        dialog._refresh_hint()
        if "不支持的协议" not in dialog.scope_label.cget("text"):
            problems.append("非法协议没有即时提示")
        # 保存内网地址：写文件 + 档位自动拨到自托管
        dialog.url_var.set("http://192.168.1.31:8000/v1")
        dialog.name_var.set("二号GPU机")
        dialog._save()
        saved = json.loads(custom_engine.CONFIG_FILE.read_text(encoding="utf-8"))
        if saved["base_url"] != "http://192.168.1.31:8000/v1" or not saved["enabled"]:
            problems.append("配置没写对: %r" % saved)
        if application.engine_var.get() != "custom":
            problems.append("保存后档位没拨到自托管: %r" % application.engine_var.get())
        dialog.window.destroy()
    finally:
        application.root.destroy()
"""


def test_custom_engine_dialog_writes_config_and_selects_tier(gui_tmp) -> None:
    problems = _run_gui_child(_BODY_DIALOG, gui_tmp)
    assert problems == []


def test_offline_gui_has_no_custom_entry_at_all(gui_tmp) -> None:
    """涉密防线第一层在渲染时就成立：保密模式连「配置自托管…」都不该存在。"""
    script = (
        "import sys\n"
        'sys.argv = ["app", "--offline"]\n'
        "import app, tkinter as tk\n"
        "problems = []\n"
        "application = app.TranslatorApp()\n"
        "try:\n"
        "    if hasattr(application, 'engine_var'):\n"
        "        problems.append('保密模式出现了 engine_var')\n"
        "    if hasattr(application, 'custom_config_link'):\n"
        "        problems.append('保密模式出现了自托管配置入口')\n"
        "    texts = []\n"
        "    def walk(w):\n"
        "        for c in w.winfo_children():\n"
        "            if isinstance(c, tk.Radiobutton):\n"
        "                texts.append(c.cget('text'))\n"
        "            walk(c)\n"
        "    walk(application.root)\n"
        "    if any('自托管' in t for t in texts):\n"
        "        problems.append('保密模式渲染了自托管单选')\n"
        "    if any('付费' in t or '云端' in t for t in texts):\n"
        "        problems.append('保密模式渲染了联网单选: %r' % texts)\n"
        "finally:\n"
        "    application.root.destroy()\n"
        "print('\\n'.join(problems))\n"
    )
    result = subprocess.run(
        [str(PYTHON), "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(ROOT),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert [line for line in result.stdout.splitlines() if line.strip()] == []


def test_existing_engine_wiring_test_still_routes(fake_translators, monkeypatch) -> None:
    """A2 重构不能改变「选了档位落到对的构造器」这条老保证。"""
    monkeypatch.setattr(app, "OFFLINE", False)
    params = _params(cache_path=None, tm=None, quality=True)
    assert registry.build("cloud", params).kind == "cloud"
    assert registry.build("local-llm", params).kind == "local_llm"
