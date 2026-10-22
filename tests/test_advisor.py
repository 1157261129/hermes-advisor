"""advisor 插件的自检脚本。

不依赖 Hermes 宿主：直接 ``python3 tests/test_advisor.py`` 可运行，也能在 pytest 下收集。
宿主模块（``tools.registry`` / ``agent.auxiliary_task_config``）与配置读取都用替身注入，
每个用例前通过 ``_reset()``（pytest 下是 ``setup_function``）恢复，不污染进程状态。
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import advisor  # noqa: E402  （先插路径再导入被测模块）

_ORIGINAL_LOAD_CONFIG = advisor._load_config
_STUBBED_MODULES = ("tools", "tools.registry", "agent", "agent.auxiliary_task_config")

_CONFIGURED = {
    "model": {"provider": "opencode-go", "default": "deepseek-v4.1-flash"},
    "agent": {"reasoning_effort": "medium"},
    "auxiliary": {"advisor": {"provider": "opencode-go", "model": "deepseek-v4.1-flash"}},
}
_UNCONFIGURED = {"auxiliary": {"advisor": {"provider": "auto", "model": "auto"}}}


def _reset() -> None:
    """恢复被替身替换的模块与配置读取，清空快照，清掉会干扰执行器识别的环境变量。"""
    advisor._load_config = _ORIGINAL_LOAD_CONFIG
    advisor._STORE.clear()
    for name in _STUBBED_MODULES:
        sys.modules.pop(name, None)
    for name in ("HERMES_INFERENCE_MODEL", "HERMES_INFERENCE_PROVIDER"):
        os.environ.pop(name, None)


def setup_function(function: Any = None) -> None:
    """pytest 的 xunit 钩子：每个用例前复位（自带的 main() 也会显式调用）。"""
    _reset()


def _use_config(config: Dict[str, Any]) -> None:
    """把给定配置注入被测模块（避免依赖真实 HERMES_HOME）。"""
    advisor._load_config = lambda: config


def _stub_registry(schemas: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """注入 ``tools.registry`` 替身：无缓存装饰器 + 工具注册表读取。"""

    class _Registry:
        """只实现插件用到的两个查询接口。"""

        def __init__(self, data: Dict[str, Dict[str, Any]]) -> None:
            """持有工具名 → schema 的映射。"""
            self._data = data

        def get_all_tool_names(self) -> List[str]:
            """返回全部工具名。"""
            return list(self._data)

        def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
            """返回工具的原始 schema。"""
            return self._data.get(name)

    module = types.ModuleType("tools.registry")
    module.no_cache_check_fn = lambda fn: fn
    module.registry = _Registry(schemas or {})
    package = types.ModuleType("tools")
    package.registry = module
    sys.modules["tools"] = package
    sys.modules["tools.registry"] = module


def _stub_auxiliary_route(block: Dict[str, Any]) -> None:
    """注入 ``agent.auxiliary_task_config`` 替身，模拟网关把 auxiliary 冻结进会话的场景。"""
    module = types.ModuleType("agent.auxiliary_task_config")
    module._get_auxiliary_task_config = lambda task: dict(block)
    package = types.ModuleType("agent")
    package.auxiliary_task_config = module
    sys.modules["agent"] = package
    sys.modules["agent.auxiliary_task_config"] = module


def _tool(name: str, description: str = "does a thing") -> Dict[str, Any]:
    """构造一个 chat 形态的工具定义。"""
    return {
        "type": "function",
        "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }


class FakeLlm:
    """记录调用参数并按顺序返回预设结果的假 ``ctx.llm``。"""

    def __init__(self, responses: Optional[List[Any]] = None) -> None:
        """``responses`` 里的元素为审阅文本，或需要抛出的异常。"""
        self.calls: List[Dict[str, Any]] = []
        self._responses = list(responses if responses is not None else ["do X next"])

    def complete(self, **kwargs: Any) -> Any:
        """记录关键字参数，返回带 text/provider/model/usage 的结果对象。"""
        self.calls.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            text=item, provider="anthropic", model="claude-opus-4-5",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15, cost_usd=0.01),
        )


class FakeCtx:
    """最小可用的 ``PluginContext`` 替身，用来断言注册结果。"""

    def __init__(self, settings: Optional[Dict[str, Any]] = None, llm: Optional[FakeLlm] = None) -> None:
        """初始化为空的注册收集器。"""
        self.settings = settings or {}
        self.llm = llm or FakeLlm()
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.middleware: Dict[str, Any] = {}
        self.commands: Dict[str, Any] = {}
        self.aux_tasks: Dict[str, Dict[str, Any]] = {}
        self.prompt_sections: Dict[str, Any] = {}
        self.hooks: Dict[str, Any] = {}
        self.unload_hooks: List[Any] = []

    def get_config(self, key: str, default: Any = None) -> Any:
        """只支持本插件用到的顶层键。"""
        return self.settings.get(key, default)

    def register_auxiliary_task(self, key: str, **kwargs: Any) -> None:
        """记录 auxiliary 任务注册。"""
        self.aux_tasks[key] = kwargs

    def register_middleware(self, kind: str, callback: Any) -> None:
        """记录中间件注册。"""
        self.middleware[kind] = callback

    def register_tool(self, **kwargs: Any) -> None:
        """记录工具注册。"""
        self.tools[kwargs["name"]] = kwargs

    def register_command(self, name: str, handler: Any, **kwargs: Any) -> None:
        """记录斜杠命令注册。"""
        self.commands[name] = {"handler": handler, **kwargs}

    def register_system_prompt_section(self, section_id: str, content: Any, **kwargs: Any) -> None:
        """记录系统提示段落注册。"""
        self.prompt_sections[section_id] = content

    def register_hook(self, hook_name: str, callback: Any) -> None:
        """记录生命周期钩子注册。"""
        self.hooks[hook_name] = callback

    def on_unload(self, callback: Any) -> None:
        """记录卸载回调。"""
        self.unload_hooks.append(callback)


def _request(messages: List[Dict[str, Any]], tools: Optional[List[str]] = None) -> Dict[str, Any]:
    """构造一个最小 provider 请求载荷。"""
    return {"messages": messages, "tools": [_tool(name) for name in (tools or [])]}


def _snapshot(messages: Optional[List[Dict[str, Any]]] = None, tools: Optional[List[str]] = None) -> Any:
    """构造一个快照对象。"""
    return advisor.Snapshot(
        messages=messages or [{"role": "user", "content": "task"}],
        tools=tuple(_tool(name) for name in (tools or [])),
        provider="opencode-go", model="deepseek-v4.1-flash",
    )


# ---------------------------------------------------------------- 捕获与路由

def test_capture_is_isolated_per_session_and_task() -> None:
    """两个会话的快照互不串线。"""
    _reset()
    _use_config(_CONFIGURED)
    advisor.capture_snapshot(_request([{"role": "user", "content": "session A"}]), session_id="a", task_id="t1")
    advisor.capture_snapshot(_request([{"role": "user", "content": "session B"}]), session_id="b", task_id="t2")
    assert advisor._STORE.get(("a", "t1")).messages[0]["content"] == "session A"
    assert advisor._STORE.get(("b", "t2")).messages[0]["content"] == "session B"
    assert advisor._STORE.get(("a", "t2")) is None


def test_capture_refreshes_on_each_request_and_returns_none() -> None:
    """每次请求都刷新快照，包含本轮新发生的工具结果；中间件不改写请求。"""
    _reset()
    _use_config(_CONFIGURED)
    key = {"session_id": "s", "task_id": "t"}
    assert advisor.capture_snapshot(
        _request([{"role": "user", "content": "run the tests"}], tools=["terminal", "read_file"]), **key) is None
    second = _request([
        {"role": "user", "content": "run the tests"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "terminal", "arguments": "{\"command\": \"pytest -q\"}"}}]},
        {"role": "tool", "name": "terminal", "content": "1 failed, 3 passed"},
    ], tools=["terminal", "read_file"])
    assert advisor.capture_snapshot(second, **key) is None
    snapshot = advisor._STORE.get(("s", "t"))
    assert len(snapshot.messages) == 3, "快照必须包含本轮已发生的工具结果"
    assert [tool["function"]["name"] for tool in snapshot.tools] == ["terminal", "read_file"]


def test_capture_drops_snapshot_when_unconfigured() -> None:
    """未配置审阅模型时不保留快照（关闭即零成本）。"""
    _reset()
    _use_config(_UNCONFIGURED)
    advisor.capture_snapshot(_request([{"role": "user", "content": "hi"}]), session_id="s", task_id="t")
    assert advisor._STORE.get(("s", "t")) is None


def test_capture_drops_stale_snapshot_when_payload_is_unusable() -> None:
    """拿到不可用的请求载荷时清掉旧快照，宁可报「无上下文」也不冒充当前上下文。"""
    _reset()
    _use_config(_CONFIGURED)
    advisor.capture_snapshot(_request([{"role": "user", "content": "old"}]), session_id="s", task_id="t")
    assert advisor._STORE.get(("s", "t")) is not None
    advisor.capture_snapshot({"input": []}, session_id="s", task_id="t")
    assert advisor._STORE.get(("s", "t")) is None


def test_route_prefers_session_frozen_auxiliary_config() -> None:
    """网关冻结的 auxiliary 配置优先于磁盘配置（磁盘可能已经被改掉）。"""
    _reset()
    _use_config(_UNCONFIGURED)
    _stub_auxiliary_route({"provider": "anthropic", "model": "claude-opus-4-5"})
    assert advisor.advisor_route() == ("anthropic", "claude-opus-4-5")

    _reset()
    _use_config(_CONFIGURED)
    assert advisor.advisor_route() == ("opencode-go", "deepseek-v4.1-flash")


def test_advisor_configured_treats_auto_as_unset() -> None:
    """``model: auto`` / ``provider: auto`` 都算未配置，避免退回主模型自我审阅。"""
    _reset()
    assert advisor.advisor_configured("auto", "auto") is False
    assert advisor.advisor_configured("", "") is False
    assert advisor.advisor_configured("", "auto") is False
    assert advisor.advisor_configured("anthropic", "auto") is True
    assert advisor.advisor_configured("auto", "claude-opus-4-5") is True


def test_executor_identity_prefers_call_level_env() -> None:
    """``hermes -m`` 只设环境变量、不改配置：执行器身份必须优先取环境覆盖。"""
    _reset()
    _use_config(_CONFIGURED)
    os.environ["HERMES_INFERENCE_PROVIDER"] = "anthropic"
    os.environ["HERMES_INFERENCE_MODEL"] = "claude-opus-4-5"
    assert advisor.executor_identity()[:2] == ("anthropic", "claude-opus-4-5")


# ---------------------------------------------------------------- 渲染与清单

def test_render_covers_tool_calls_results_and_images() -> None:
    """渲染覆盖工具调用、工具结果、多模态降级，且不做长度裁剪。"""
    long_result = "x" * 20_000
    text = advisor.render_transcript([
        {"role": "system", "content": "be careful"},
        {"role": "user", "content": [{"type": "text", "text": "看这张图"},
                                     {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "/tmp/x"}}}]},
        {"role": "tool", "name": "read_file", "content": long_result},
    ])
    assert "[system]\nbe careful" in text
    assert "[image omitted]" in text
    assert "[assistant -> read_file]" in text and "/tmp/x" in text
    assert "[tool result: read_file]" in text
    assert long_result in text, "不得裁剪：信息完整性优先，超限由调用方显式报错"


def test_strip_inflight_advisor_call_keeps_assistant_text() -> None:
    """尾部未完成的 advisor 调用被剔除，助手文本与其他并行调用保留。"""
    tail = {"role": "assistant", "content": "thinking", "tool_calls": [
        {"function": {"name": "advisor", "arguments": "{}"}},
        {"function": {"name": "terminal", "arguments": "{}"}},
    ]}
    stripped = advisor.strip_inflight_advisor_call([{"role": "user", "content": "go"}, tail])
    assert [advisor._tool_call_name(call) for call in stripped[-1]["tool_calls"]] == ["terminal"]
    assert stripped[-1]["content"] == "thinking"

    only_advisor = {"role": "assistant", "content": "text before call",
                    "tool_calls": [{"function": {"name": "advisor"}}]}
    kept = advisor.strip_inflight_advisor_call([{"role": "user", "content": "go"}, only_advisor])
    assert kept[-1]["content"] == "text before call", "整条删掉会丢掉助手文本，上游不这么做"
    assert kept[-1]["tool_calls"] == []

    empty = {"role": "assistant", "tool_calls": [{"function": {"name": "advisor"}}]}
    assert advisor.strip_inflight_advisor_call([{"role": "user", "content": "go"}, empty]) == [
        {"role": "user", "content": "go"}]

    untouched = [{"role": "user", "content": "go"}]
    assert advisor.strip_inflight_advisor_call(untouched) is untouched


def test_inventory_uses_full_registry_not_deferred_bridge() -> None:
    """清单取整个注册表（上游 getAllTools 语义），不受 tool_search 折叠影响。"""
    _reset()
    _stub_registry({
        "read_file": {"description": "read a file", "parameters": {"type": "object"}},
        "terminal": {"description": "run a command", "parameters": {"type": "object"}},
    })
    text = advisor.inventory_text(tuple(_tool(name) for name in ("tool_search", "tool_call")))
    assert text.startswith("## Available Executor Tools")
    assert "### read_file" in text and "### terminal" in text
    assert "tool_search" not in text, "注册表可用时不使用请求里的桥工具"
    assert text.index("### read_file") < text.index("### terminal")


def test_inventory_falls_back_to_request_tools() -> None:
    """注册表不可用时退回请求里的工具定义。"""
    _reset()
    text = advisor.inventory_text((_tool("terminal", "run a command"),))
    assert "### terminal" in text and "run a command" in text
    assert advisor.inventory_text(()) == ""


def test_review_messages_shape() -> None:
    """审阅请求 = system 提示 + 工具清单 + 以 user 角色收尾的 transcript。"""
    _reset()
    _stub_registry({"terminal": {"description": "run", "parameters": {}}})
    messages = advisor.build_review_messages(_snapshot(tools=["terminal"]), "fix the build")
    assert [message["role"] for message in messages] == ["system", "user", "user"]
    assert messages[0]["content"] == advisor.SYSTEM_PROMPT
    assert messages[1]["content"].startswith("## Available Executor Tools")
    body = messages[2]["content"]
    assert "Executor model: opencode-go/deepseek-v4.1-flash" in body
    assert "Current user request: fix the build" in body
    assert body.endswith(advisor.ADVISOR_NUDGE)


# ---------------------------------------------------------------- 门控

def test_blocklist_matching() -> None:
    """blocklist 支持字符串条目、对象条目、min_effort 阈值与冒号写法归一。"""
    _reset()
    executor = "opencode-go/deepseek-v4.1-flash"
    assert advisor.executor_blocked([executor], executor, "medium") is True
    assert advisor.executor_blocked(["opencode-go:deepseek-v4.1-flash"], executor, "medium") is True
    assert advisor.executor_blocked(["other/model"], executor, "medium") is False
    assert advisor.executor_blocked([{"model": executor}], executor, "medium") is True
    assert advisor.executor_blocked([{"model": executor, "min_effort": "high"}], executor, "medium") is False
    assert advisor.executor_blocked([{"model": executor, "min_effort": "high"}], executor, "high") is True
    assert advisor.executor_blocked([{"model": executor, "min_effort": "minimal"}], executor, "none") is False
    assert advisor.executor_blocked([{"model": executor, "min_effort": "bogus"}], executor, "max") is False
    assert advisor.executor_blocked([{"min_effort": "low"}], executor, "max") is False
    assert advisor.executor_blocked("not-a-list", executor, "max") is False


def test_check_fn_hides_tool_when_unconfigured_or_blocked() -> None:
    """未配置或命中 blocklist 时工具不可用；正常配置下可用。"""
    _reset()
    ctx = FakeCtx()
    check = advisor.make_check_fn(ctx)

    _use_config(_UNCONFIGURED)
    assert check() is False

    _use_config(_CONFIGURED)
    assert check() is True

    ctx.settings["disabled_for_models"] = ["opencode-go/deepseek-v4.1-flash"]
    assert check() is False


def test_prompt_section_accepts_readonly_mapping() -> None:
    """宿主传的是 MappingProxyType：按 Mapping 读取，blocklist 才能命中会话身份。"""
    _reset()
    ctx = FakeCtx(settings={"disabled_for_models": ["anthropic/claude-opus-4-5"]})
    section = advisor.make_prompt_section(ctx)
    _use_config(_CONFIGURED)

    blocked = MappingProxyType({"provider": "anthropic", "model": "claude-opus-4-5"})
    allowed = MappingProxyType({"provider": "opencode-go", "model": "deepseek-v4.1-flash"})
    assert section(blocked) == ""
    assert section(allowed) == advisor.GUIDANCE_SNIPPET

    _use_config(_UNCONFIGURED)
    assert section(allowed) == ""


# ---------------------------------------------------------------- 调用

def test_consult_success_envelope() -> None:
    """成功路径返回审阅文本与宿主归属信息。"""
    _reset()
    _use_config(_CONFIGURED)
    ctx = FakeCtx()
    payload = advisor.consult(ctx, _snapshot(tools=["terminal"]), "fix the build")
    assert payload["text"] == "do X next"
    assert payload["advisor"]["model"] == "claude-opus-4-5"
    assert payload["advisor"]["usage"]["total_tokens"] == 15
    call = ctx.llm.calls[0]
    assert call["task"] == "advisor" and call["purpose"] == "advisor"


def test_consult_retries_empty_response_once() -> None:
    """空回答只重试一次；两次都空则返回上游的空响应急文案。"""
    _reset()
    _use_config(_CONFIGURED)
    retried = FakeCtx(llm=FakeLlm(["   ", "second time lucky"]))
    assert advisor.consult(retried, _snapshot())["text"] == "second time lucky"
    assert len(retried.llm.calls) == 2

    empty = FakeCtx(llm=FakeLlm(["", ""]))
    payload = advisor.consult(empty, _snapshot())
    assert payload["text"] == advisor.ERR_EMPTY_RESPONSE
    assert payload["error"] == advisor.ERR_EMPTY_RESPONSE_DETAIL
    assert len(empty.llm.calls) == 2, "只重试一次，不允许循环"


def test_consult_failure_paths() -> None:
    """异常路径返回可读错误，绝不抛出。"""
    _reset()
    _use_config(_CONFIGURED)
    failed = advisor.consult(FakeCtx(llm=FakeLlm([RuntimeError("boom")])), _snapshot())
    assert failed["text"] == "Advisor call threw: boom"
    assert failed["error"] == "boom"


def test_consult_refuses_when_route_was_removed() -> None:
    """会话开始后撤掉路由：明确报「未配置」，不退化成用主模型审阅自己，也不发请求。"""
    _reset()
    _use_config(_UNCONFIGURED)
    ctx = FakeCtx()
    payload = advisor.consult(ctx, _snapshot())
    assert payload["text"] == advisor.ERR_NO_MODEL
    assert payload["error"] == advisor.ERR_NO_MODEL_SELECTED
    assert ctx.llm.calls == []


def test_consult_refuses_when_snapshot_executor_is_blocked() -> None:
    """blocklist 按快照里的真实执行器判定：check_fn 只在构造时评估过，调用时必须再拦一次。"""
    _reset()
    _use_config(_CONFIGURED)
    ctx = FakeCtx(settings={"disabled_for_models": ["opencode-go/deepseek-v4.1-flash"]})
    payload = advisor.consult(ctx, _snapshot())
    assert payload["error"] == advisor.ERR_BLOCKED_DETAIL
    assert "opencode-go/deepseek-v4.1-flash" in payload["text"]
    assert ctx.llm.calls == []


def test_handler_without_snapshot_does_not_call_llm() -> None:
    """没有快照时直接返回错误，不发起付费调用。"""
    _reset()
    ctx = FakeCtx()
    payload = json.loads(advisor.make_handler(ctx)({}, session_id="s", task_id="t"))
    assert payload["text"] == advisor.ERR_NO_CONTEXT
    assert payload["error"] == advisor.ERR_NO_CONTEXT_DETAIL
    assert ctx.llm.calls == []


def test_handler_passes_user_task_through() -> None:
    """有快照时把 ``user_task`` 传给审阅请求并返回 JSON 结果。"""
    _reset()
    _use_config(_CONFIGURED)
    advisor.capture_snapshot(_request([{"role": "user", "content": "do it"}]), session_id="s", task_id="t")
    ctx = FakeCtx()
    payload = json.loads(advisor.make_handler(ctx)({}, session_id="s", task_id="t", user_task="do it"))
    assert payload["text"] == "do X next"
    assert "Current user request: do it" in ctx.llm.calls[0]["messages"][-1]["content"]


def test_on_session_end_drops_that_session_only() -> None:
    """会话结束只清该会话的快照。"""
    _reset()
    _use_config(_CONFIGURED)
    advisor.capture_snapshot(_request([{"role": "user", "content": "a"}]), session_id="a", task_id="t")
    advisor.capture_snapshot(_request([{"role": "user", "content": "b"}]), session_id="b", task_id="t")
    advisor.on_session_end(session_id="a")
    assert advisor._STORE.get(("a", "t")) is None
    assert advisor._STORE.get(("b", "t")) is not None


# ---------------------------------------------------------------- 注册

def test_register_wires_everything() -> None:
    """注册函数接好 auxiliary 槽位、中间件、零参数工具、/advisor、提示段落与卸载回调。"""
    _reset()
    _stub_registry({"terminal": {"description": "run", "parameters": {}}})
    _use_config(_CONFIGURED)
    ctx = FakeCtx()
    advisor.register(ctx)

    assert "advisor" in ctx.aux_tasks
    assert ctx.middleware["llm_request"] is advisor.capture_snapshot
    tool = ctx.tools["advisor"]
    assert tool["schema"]["parameters"] == {"type": "object", "properties": {}, "required": []}
    assert tool["check_fn"]() is True
    assert "advisor" in ctx.commands
    assert "on_session_end" in ctx.hooks
    assert ctx.prompt_sections["advisor-guidance"]({}) == advisor.GUIDANCE_SNIPPET
    assert ctx.unload_hooks == [advisor._STORE.clear]
    assert advisor.status_text(ctx).startswith("Advisor")


def main() -> int:
    """按定义顺序执行所有 ``test_*`` 函数；返回进程退出码。"""
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        _reset()
        try:
            fn()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # 让导入/类型错误也显式失败，而不是静默通过
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"ok   {name}")
    _reset()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
