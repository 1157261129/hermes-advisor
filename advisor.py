"""advisor 插件核心实现（移植 pi 扩展 ``@juicesharp/rpiv-advisor`` 2.13.9）。

执行器模型通过零参数 ``advisor()`` 工具，把当前**已解析上下文**交给一个更强的审阅模型，
取回计划、纠偏或停止信号。审阅模型走 Hermes 原生 ``auxiliary.advisor`` 槽位：认证、路由、
推理强度、超时与持久化全部由宿主负责，插件不接触凭据。

上下文来源是 ``llm_request`` middleware：每次 provider 请求前，宿主把装配完成的请求交给中间件，
其中 ``messages`` 已经是压缩/裁剪之后的语义，并包含本轮此前发生的工具调用与结果。插件只读该
请求（回调返回 ``None`` 表示不改写），按 ``(session_id, task_id)`` 保存快照，供工具调用时使用。

与上游的差异（均为宿主能力差异，不是功能裁剪）：

* 上下文以文本 transcript 形式外发，而不是原样转发 provider 消息数组——审阅模型可能是另一家
  provider，``tool_call_id``、``reasoning_content``、缓存标记等字段原样转发会被拒收；渲染后
  工具调用与结果的信息不丢，且不做任何长度裁剪（超出审阅模型能力时会显式报错）。
* 审阅模型的选择/推理强度改由 ``auxiliary.advisor`` 槽位承载（Hermes 已有的模型选择器、
  profile 与密钥体系），不再自建 ``advisor.json`` 与 ``/advisor`` 选择器。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: 审阅模型所用的 auxiliary 任务键（同时是 ``/advisor`` 与配置里的槽位名）。
TASK = "advisor"
#: 面向模型的工具名。
TOOL_NAME = "advisor"

# 快照保留上限（按 (session_id, task_id) 计），避免长驻网关进程无限累积历史。
_MAX_SNAPSHOTS = 16

# 推理强度排序；未设置、"none"、"off" 记为 0，永远不满足 min_effort 阈值（上游 fail-soft 语义）。
EFFORT_ORDINAL: Dict[str, int] = {
    "none": 0,
    "off": 0,
    "minimal": 1,
    "low": 2,
    "medium": 3,
    "high": 4,
    "xhigh": 5,
    "max": 6,
}

# 审阅者系统提示，取自上游 prompts/advisor-system.txt。
SYSTEM_PROMPT = (
    "You are an advisor model in an advisor-strategy pattern. An executor model is running a task "
    "end-to-end — calling tools, reading results, iterating toward a solution. When the executor hits "
    "a decision it cannot reasonably solve alone, it consults you for guidance. The executor's full "
    "tool inventory is prepended before the conversation so you can judge tool-choice correctness.\n\n"
    "You read the shared conversation context and return ONE of:\n"
    "- a plan (concrete next steps the executor should take),\n"
    "- a correction (the executor is going down a wrong path — redirect it),\n"
    "- a stop signal (the executor should halt and escalate to the user).\n\n"
    "You NEVER call tools. You NEVER produce user-facing output. Be concise, directive, and grounded in "
    "the shared context. Name files, functions, and line numbers where possible. No preamble, no "
    "apologies, no meta-commentary about being an advisor — just the guidance the executor needs."
)

# 保证 transcript 以 user 角色收尾（部分 provider 拒绝 assistant 前缀续写）。
ADVISOR_NUDGE = "Please advise on the executor's situation above."

# 系统提示里的精简指引。Hermes 默认把插件工具收进 tool_search 渐进披露，工具描述往往不在提示词里，
# 所以另注册一段 system prompt section（未配置时返回空串，整段不渲染，零 token 成本）。
GUIDANCE_SNIPPET = (
    "A stronger reviewer model is available as the `advisor` tool (zero parameters; if it is not in "
    "your tool list, reach it through tool_search/tool_call). Call it before substantive work, when "
    "stuck or looping, and once more before declaring the task done; restate its key guidance in your "
    "next visible reply."
)

# 工具描述 = 上游 ADVISOR_DESCRIPTION + 上游 DEFAULT_PROMPT_GUIDELINES。
# Hermes 插件没有独立的 promptSnippet/promptGuidelines 通道，因此并入 schema 描述。
ADVISOR_DESCRIPTION = (
    "Escalate to a stronger reviewer model for guidance. When you need stronger judgment — a complex "
    "decision, an ambiguous failure, a problem you're circling without progress — escalate to the "
    "advisor model for guidance, then resume. Takes NO parameters — when you call advisor(), your "
    "entire conversation history is automatically forwarded. The advisor sees the task, every tool call "
    "you've made, every result you've seen.\n\n"
    "Call `advisor` BEFORE substantive work — before writing, before committing to an interpretation, "
    "before building on an assumption. Orientation (finding files, fetching a source, seeing what's "
    "there) is not substantive work; writing, editing, and declaring an answer are.\n"
    "Also call `advisor` when you believe the task is complete. BEFORE this call, make your deliverable "
    "durable: write the file, save the result, commit the change. The advisor call takes time; if the "
    "session ends during it, a durable result persists and an unwritten one doesn't.\n"
    "Also call `advisor` when stuck — errors recurring, approach not converging, results that don't fit "
    "— or when considering a change of approach.\n"
    "On tasks longer than a few steps, call `advisor` at least once before committing to an approach and "
    "once before declaring done. On short reactive tasks where the next action is dictated by tool "
    "output you just read, you don't need to keep calling — the advisor adds most of its value on the "
    "first call, before the approach crystallizes.\n"
    "Give the advisor's advice serious weight. If you follow a step and it fails empirically, or you have "
    "primary-source evidence that contradicts a specific claim, adapt — a passing self-test is not "
    "evidence the advice is wrong, it's evidence your test doesn't check what the advice is checking.\n"
    "If you've already retrieved data pointing one way and the advisor points another, don't silently "
    "switch — surface the conflict in one more `advisor` call (\"I found X, you suggest Y, which "
    "constraint breaks the tie?\"). A reconcile call is cheaper than committing to the wrong branch.\n"
    "After each `advisor` result, put the advisor's key guidance into your next visible reply to the user "
    "before continuing — quote or paraphrase the plan, correction, or stop signal. The user often cannot "
    "see collapsed tool results; do not keep the advisor's words only in silent tool context."
)

# 失败文案与上游 messages.ts 保持一致，便于对照排查。
ERR_NO_MODEL = "No advisor model is configured. The user can enable one with the /advisor command."
ERR_NO_MODEL_SELECTED = "no advisor model selected"
ERR_EMPTY_RESPONSE = "Advisor returned no text content."
ERR_EMPTY_RESPONSE_DETAIL = "empty response"
ERR_NO_CONTEXT = (
    "No conversation context captured yet for this session. The advisor receives the resolved context "
    "as of the previous model request; retry after the next tool result."
)
ERR_NO_CONTEXT_DETAIL = "no context snapshot"
ERR_BLOCKED = (
    "Advisor is disabled for executor {model} at effort={effort}. Do not consult it; proceed on your "
    "own and tell the user you escalated without a reviewer."
)
ERR_BLOCKED_DETAIL = "disabled for current executor"

ADVISOR_SCHEMA: Dict[str, Any] = {
    "name": TOOL_NAME,
    "description": ADVISOR_DESCRIPTION,
    "parameters": {"type": "object", "properties": {}, "required": []},
}


@dataclass(frozen=True)
class Snapshot:
    """一次 provider 请求前捕获的有效上下文。

    ``messages`` 直接引用宿主 middleware 传入的请求副本，插件不再复制也不再改动它；
    ``tools`` 是该次请求里下发给执行器的工具定义，仅在工具注册表不可用时作为清单兜底。
    """

    messages: List[Dict[str, Any]]
    tools: Tuple[Dict[str, Any], ...]
    provider: str
    model: str


class SnapshotStore:
    """按 ``(session_id, task_id)`` 保存最近一次快照的有界存储。

    并发工具调用与多会话并存，读写都在锁内；超过 ``cap`` 时按写入顺序淘汰最旧键。
    """

    def __init__(self, cap: int = _MAX_SNAPSHOTS) -> None:
        """建一个最多保留 ``cap`` 个键的存储。"""
        self._cap = max(1, int(cap))
        self._items: "OrderedDict[Tuple[str, str], Snapshot]" = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: Tuple[str, str], snapshot: Snapshot) -> None:
        """写入（或覆盖）一个快照，并把它标为最新。"""
        with self._lock:
            self._items[key] = snapshot
            self._items.move_to_end(key)
            while len(self._items) > self._cap:
                self._items.popitem(last=False)

    def get(self, key: Tuple[str, str]) -> Optional[Snapshot]:
        """取该键当前快照，没有则返回 ``None``。"""
        with self._lock:
            return self._items.get(key)

    def drop(self, key: Tuple[str, str]) -> None:
        """丢弃该键快照（关闭审阅模型或载荷不可用时使用，避免沿用旧上下文）。"""
        with self._lock:
            self._items.pop(key, None)

    def drop_session(self, session_id: str) -> None:
        """丢弃该会话下所有任务的快照（会话结束时清场）。"""
        with self._lock:
            for key in [key for key in self._items if key[0] == session_id]:
                self._items.pop(key, None)

    def clear(self) -> None:
        """清空全部快照（插件卸载时使用）。"""
        with self._lock:
            self._items.clear()


#: 进程级单例：middleware 回调与工具处理函数以此共享快照。
_STORE = SnapshotStore()


def _content_to_text(content: Any) -> str:
    """把消息内容（字符串、多模态内容块或任意对象）渲染为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "").strip()
            if kind in ("text", "input_text", "output_text"):
                parts.append(str(block.get("text") or ""))
            elif kind in ("image", "image_url", "input_image"):
                # 只做文本审阅；图片不转发，避免把二进制塞进审阅请求。
                parts.append("[image omitted]")
            else:
                parts.append(f"[{kind or 'part'} omitted]")
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _tool_call_name(call: Any) -> str:
    """取一条工具调用的函数名（兼容 ``{function: {name}}`` 与扁平形态）。"""
    if not isinstance(call, dict):
        return ""
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    return str(function.get("name") or call.get("name") or "").strip()


def _render_tool_call(call: Any) -> str:
    """把一条 assistant 工具调用渲染成 ``name(args)`` 形式的一行。"""
    if not isinstance(call, dict):
        return ""
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    name = _tool_call_name(call) or "tool"
    raw_args = function.get("arguments", call.get("arguments"))
    if isinstance(raw_args, str):
        args_text = raw_args
    elif raw_args is None:
        args_text = ""
    else:
        args_text = json.dumps(raw_args, ensure_ascii=False)
    return f"[assistant -> {name}] {args_text}"


def _render_message(message: Any) -> str:
    """把一条消息渲染成一个 transcript 块；无法识别的形态退化为一行 JSON。"""
    if not isinstance(message, dict):
        return ""
    role = str(message.get("role") or "").strip()
    if not role:
        # Responses API 形态（function_call / function_call_output / …）：保留类型与内容。
        kind = str(message.get("type") or "item")
        return f"[{kind}] {json.dumps(message, ensure_ascii=False)}"
    body = _content_to_text(message.get("content")).strip()
    if role == "tool" and body:
        name = str(message.get("name") or "").strip()
        return f"[tool result{': ' + name if name else ''}]\n{body}"
    blocks: List[str] = []
    if body:
        blocks.append(f"[{role}]\n{body}")
    for call in message.get("tool_calls") or []:
        rendered = _render_tool_call(call)
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks)


def render_transcript(messages: List[Dict[str, Any]]) -> str:
    """渲染整份 transcript（不裁剪：超出审阅模型能力时由调用方显式报错）。"""
    return "\n\n".join(
        block for block in (_render_message(message) for message in messages or []) if block)


def strip_inflight_advisor_call(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """去掉尾部 assistant 消息里尚未产生结果的 ``advisor`` 调用。

    对应上游 ``stripInflightAdvisorCall``：孤儿 tool_call 会被多数 provider 拒收。只删该调用，
    助手文本保留；文本也没有时才整条消息移除。正常情况下快照取自本轮调用之前，这里是防御性兜底。
    """
    if not messages:
        return messages
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "assistant":
        return messages
    calls = last.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return messages
    kept = [call for call in calls if _tool_call_name(call) != TOOL_NAME]
    if len(kept) == len(calls):
        return messages
    if kept:
        return messages[:-1] + [{**last, "tool_calls": kept}]
    if _content_to_text(last.get("content")).strip():
        return messages[:-1] + [{**last, "tool_calls": []}]
    return messages[:-1]


def _request_tools(request: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    """从请求载荷里取出工具定义（``tools``），只保留字典项。"""
    tools = request.get("tools") if isinstance(request, dict) else None
    if not isinstance(tools, list):
        return ()
    return tuple(tool for tool in tools if isinstance(tool, dict))


def _render_inventory_entry(name: str, description: str, parameters: Any) -> str:
    """渲染清单里的一个工具块（名字 + 描述 + 稳定序列化的参数）。"""
    params_text = json.dumps(parameters if parameters is not None else {}, ensure_ascii=False, sort_keys=True)
    return f"### {name}\n{description}\n\nParameters: {params_text}"


def inventory_text(fallback_tools: Tuple[Dict[str, Any], ...] = ()) -> str:
    """完整工具清单（上游 ``getAllTools()`` 语义：整个注册表，不是当前激活子集）。

    优先读工具注册表——默认配置下 ``tool_search`` 会把插件工具收进桥，请求里的 ``tools`` 只剩
    三个桥工具，不能代表执行器能力。注册表不可用时退回本次请求里的工具定义。
    """
    entries: List[str] = []
    try:
        from tools.registry import registry

        for name in sorted(registry.get_all_tool_names()):
            schema = registry.get_schema(name) or {}
            entries.append(_render_inventory_entry(
                name, str(schema.get("description") or "").strip(), schema.get("parameters")))
    except Exception as exc:
        logger.debug("advisor: 工具注册表不可用，退回请求内的工具定义: %s", exc)
    if not entries:
        for tool in fallback_tools:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(function.get("name") or "").strip()
            if name:
                entries.append(_render_inventory_entry(
                    name, str(function.get("description") or "").strip(), function.get("parameters")))
    if not entries:
        return ""
    return "## Available Executor Tools\n\n" + "\n\n---\n\n".join(sorted(entries))


def _payload_messages(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """从请求载荷里取出消息列表；chat 形态用 ``messages``，Responses 形态用 ``input``。"""
    for key in ("messages", "input"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            return value
    return None


def capture_snapshot(
    request: Dict[str, Any], *, session_id: str = "", task_id: str = "",
    provider: str = "", model: str = "", **_: Any,
) -> None:
    """``llm_request`` middleware 回调：按会话记录本次请求的有效上下文。

    只做观测：永远返回 ``None``，不替换宿主请求。未配置审阅模型、或本次请求拿不到消息载荷时
    丢弃旧快照——宁可让工具报「无上下文」，也不拿上一轮的快照冒充当前上下文。
    """
    key = (str(session_id or ""), str(task_id or ""))
    try:
        if not advisor_configured(*advisor_route()):
            _STORE.drop(key)
            return
        messages = _payload_messages(request)
        if not messages:
            _STORE.drop(key)
            logger.debug("advisor: 本次 provider 请求没有可用的消息载荷，已丢弃旧快照")
            return
        _STORE.put(
            key,
            Snapshot(
                messages=messages,
                tools=_request_tools(request),
                provider=str(provider or ""),
                model=str(model or ""),
            ),
        )
    except Exception as exc:  # middleware 契约是 fail-open，记录后放行原请求
        logger.warning("advisor: 上下文捕获失败: %s: %s", type(exc).__name__, exc)


def build_review_messages(snapshot: Snapshot, user_task: str = "") -> List[Dict[str, str]]:
    """把快照组装成审阅请求：系统提示 + 工具清单 + 以 user 角色收尾的 transcript。"""
    context: List[str] = []
    if snapshot.model:
        executor = f"{snapshot.provider}/{snapshot.model}".strip("/")
        context.append(f"Executor model: {executor}")
    if user_task.strip():
        context.append("Current user request: " + user_task.strip())
    transcript = render_transcript(strip_inflight_advisor_call(snapshot.messages))
    body = "\n\n".join(part for part in (*context, transcript, ADVISOR_NUDGE) if part)
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    inventory = inventory_text(snapshot.tools)
    if inventory:
        messages.append({"role": "user", "content": inventory})
    messages.append({"role": "user", "content": body})
    return messages


def _usage_dict(usage: Any) -> Dict[str, Any]:
    """把宿主的 usage 对象摊平成可 JSON 序列化的字典（缺失字段省略）。"""
    if usage is None:
        return {}
    result: Dict[str, Any] = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
        value = getattr(usage, field, None)
        if value is not None:
            result[field] = value
    return result


def _advisor_envelope(text: str, result: Any, started: float, **extra: Any) -> Dict[str, Any]:
    """构造工具结果 envelope：审阅文本置顶，其余为宿主归属信息。"""
    return {
        "text": text,
        **extra,
        "advisor": {
            "provider": getattr(result, "provider", "") or "",
            "model": getattr(result, "model", "") or "",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "usage": _usage_dict(getattr(result, "usage", None)),
        },
    }


def consult(ctx: Any, snapshot: Snapshot, user_task: str = "", entries: Any = None) -> Dict[str, Any]:
    """调用审阅模型并返回工具结果 envelope；失败路径同样返回字典，从不抛异常。

    进入时再做一次 fail-closed 判定：``check_fn`` 只在 agent 构造时评估一次，配置在本会话开始后
    被撤掉、或执行器被写进 blocklist 时，工具本身必须拒绝调用，而不是默默用主模型审阅自己。
    """
    route_provider, route_model = advisor_route()
    if not advisor_configured(route_provider, route_model):
        return {"text": ERR_NO_MODEL, "error": ERR_NO_MODEL_SELECTED}
    blocklist = ctx.get_config("disabled_for_models", default=[]) if entries is None else entries
    _, _, effort = executor_identity()
    executor_key = f"{snapshot.provider}/{snapshot.model}".strip("/")
    if executor_blocked(blocklist, executor_key, effort):
        return {
            "text": ERR_BLOCKED.format(model=executor_key or "(unknown)", effort=effort or "unset"),
            "error": ERR_BLOCKED_DETAIL,
        }
    started = time.monotonic()
    try:
        messages = build_review_messages(snapshot, user_task)
        result = ctx.llm.complete(messages=messages, task=TASK, purpose="advisor")
        text = (getattr(result, "text", "") or "").strip()
        if not text:
            # 上游 R6.4：空回答只重试一次，且复用同一份消息，避免与首次调用分叉。
            result = ctx.llm.complete(messages=messages, task=TASK, purpose="advisor")
            text = (getattr(result, "text", "") or "").strip()
            if not text:
                logger.warning("advisor: 审阅模型连续两次返回空内容")
                return _advisor_envelope(
                    ERR_EMPTY_RESPONSE, result, started, error=ERR_EMPTY_RESPONSE_DETAIL)
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        logger.warning("advisor: 审阅调用失败: %s: %s", type(exc).__name__, message)
        return {"text": f"Advisor call threw: {message}", "error": message}
    return _advisor_envelope(text, result, started)


def _load_config() -> Dict[str, Any]:
    """读取宿主配置（只读缓存，禁止修改返回值）。失败时返回空字典，按「未配置」处理。"""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def advisor_route() -> Tuple[str, str]:
    """有效 ``auxiliary.advisor`` 路由 ``(provider, model)``。

    优先经宿主解析器读取——网关会话会把 ``auxiliary`` 冻结进 ContextVar，磁盘配置可能已经变了；
    解析器不可用时退回磁盘配置。
    """
    block: Any = {}
    try:
        from agent.auxiliary_task_config import _get_auxiliary_task_config

        block = _get_auxiliary_task_config(TASK)
    except Exception:
        block = {}
    if not isinstance(block, dict) or not block:
        auxiliary = _load_config().get("auxiliary")
        block = auxiliary.get(TASK) if isinstance(auxiliary, dict) else {}
    if not isinstance(block, dict):
        return "", ""
    return str(block.get("provider") or "").strip(), str(block.get("model") or "").strip()


def advisor_configured(provider: str, model: str) -> bool:
    """审阅模型是否被明确选中：指定了非 ``auto`` 的 model，或 provider 不是 ``auto``/空。

    ``model: auto`` 是「没有偏好」的哨兵，宿主管线会退回主模型——那等于让主模型审阅自己，
    必须按未配置处理。
    """
    model_ok = model.strip().lower() not in ("", "auto")
    provider_ok = provider.strip().lower() not in ("", "auto")
    return model_ok or provider_ok


def executor_identity() -> Tuple[str, str, str]:
    """取执行器当前的 ``(provider, model, reasoning_effort)``，用于 blocklist 匹配。

    优先读调用级环境覆盖（``hermes -m`` / TUI 启动会设置），再退回配置；``-m`` 不改配置文件，
    只看配置会把执行器认错。
    """
    config = _load_config()
    model_block = config.get("model") if isinstance(config.get("model"), dict) else {}
    provider = (os.getenv("HERMES_INFERENCE_PROVIDER") or "").strip() or str(
        model_block.get("provider") or "").strip()
    model = (os.getenv("HERMES_INFERENCE_MODEL") or "").strip() or str(
        model_block.get("default") or model_block.get("model") or "").strip()
    agent_block = config.get("agent") if isinstance(config.get("agent"), dict) else {}
    effort: Any = agent_block.get("reasoning_effort")
    if isinstance(effort, dict):
        # 允许按模型区分的字典形态：先精确键，再退到全局默认。
        effort = effort.get(f"{provider}/{model}") or effort.get(model) or effort.get("default")
    if not isinstance(effort, str) or not effort.strip():
        fallback = model_block.get("reasoning_effort")
        effort = fallback if isinstance(fallback, str) else ""
    return provider, model, effort.strip()


def _canonical_model_key(value: str) -> str:
    """把 ``provider:model`` 与 ``provider/model`` 归一成斜杠形式（小写）。"""
    key = (value or "").strip().lower()
    if "/" not in key and ":" in key:
        provider, _, model = key.partition(":")
        key = f"{provider}/{model}"
    return key


def executor_blocked(entries: Any, model_key: str, effort: str) -> bool:
    """``disabled_for_models`` 是否命中当前执行器（与上游 ``isModelBlocked`` 语义一致）。

    字符串条目命中即禁用；对象条目需 ``model`` 匹配，带 ``min_effort`` 时按推理强度阈值比较
    （达到或超过即命中；执行器强度为 0/未设置时永不命中）。无法识别的条目按上游语义丢弃。
    """
    if not isinstance(entries, list):
        return False
    target = _canonical_model_key(model_key)
    if not target or target == "/":
        return False
    level = EFFORT_ORDINAL.get((effort or "").strip().lower(), 0)
    for entry in entries:
        if isinstance(entry, str):
            if _canonical_model_key(entry) == target:
                return True
            continue
        if not isinstance(entry, dict):
            continue
        candidate = entry.get("model")
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        if _canonical_model_key(candidate) != target:
            continue
        threshold = entry.get("min_effort", entry.get("minEffort"))
        if not isinstance(threshold, str) or not threshold.strip():
            return True
        required = EFFORT_ORDINAL.get(threshold.strip().lower())
        if required is None:
            # 阈值本身无法排序时跳过该条目，避免「未知」被当成最低档而误命中。
            continue
        if level and level >= required:
            return True
    return False


def advisor_state(entries: Any) -> Tuple[str, str]:
    """返回 ``(state, 说明)``，供 ``check_fn``、``/advisor`` 与提示段落共用同一套判断。"""
    if not advisor_configured(*advisor_route()):
        return "off", "no reviewer configured (auxiliary.advisor is unset or auto)"
    provider, model, effort = executor_identity()
    if executor_blocked(entries, f"{provider}/{model}", effort):
        return "blocked", f"disabled for executor {provider}/{model} (effort={effort or 'unset'})"
    return "active", "active"


def make_check_fn(ctx: Any):
    """构造工具可用性判断：未配置审阅模型、或执行器被 blocklist 命中时不出现在工具集里。"""

    def advisor_available() -> bool:
        """``check_fn``：配置缺失或执行器被禁用时返回 ``False``，工具从提示词中完全消失。"""
        state, reason = advisor_state(ctx.get_config("disabled_for_models", default=[]))
        if state != "active":
            logger.debug("advisor: 工具不可用（%s）", reason)
        return state == "active"

    return advisor_available


def make_prompt_section(ctx: Any):
    """构造系统提示段落：只在审阅模型已配置且当前会话执行器未被禁用时渲染。

    宿主传入的是 ``MappingProxyType``（`hermes_cli/plugins_dispatch.py`），必须按 ``Mapping``
    判断，不能只认 ``dict``，否则会话身份会被丢成空映射、blocklist 形同虚设。
    """

    def render_section(session_info: Any) -> str:
        """返回指引文本；返回空串时宿主不渲染该段（不占 token）。"""
        info: Mapping[str, Any] = session_info if isinstance(session_info, Mapping) else {}
        if not advisor_configured(*advisor_route()):
            return ""
        provider = str(info.get("provider") or "").strip()
        model = str(info.get("model") or "").strip()
        _, _, effort = executor_identity()
        entries = ctx.get_config("disabled_for_models", default=[])
        if provider or model:
            if executor_blocked(entries, f"{provider}/{model}", effort):
                return ""
        elif executor_blocked(entries, f"{executor_identity()[0]}/{executor_identity()[1]}", effort):
            return ""
        return GUIDANCE_SNIPPET

    return render_section


def make_handler(ctx: Any):
    """构造 ``advisor`` 工具处理函数：读快照、调用审阅模型、返回 JSON 结果。"""

    def handle(args: Dict[str, Any], **kwargs: Any) -> str:
        """工具入口；``args`` 为空对象（零参数），会话上下文从 dispatch 注入的 kwargs 取。"""
        key = (str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""))
        snapshot = _STORE.get(key)
        if snapshot is None:
            logger.info("advisor: 无上下文快照，拒绝调用 (session=%s task=%s)", key[0], key[1])
            return json.dumps({"text": ERR_NO_CONTEXT, "error": ERR_NO_CONTEXT_DETAIL}, ensure_ascii=False)
        payload = consult(ctx, snapshot, str(kwargs.get("user_task") or ""))
        return json.dumps(payload, ensure_ascii=False)

    return handle


def status_text(ctx: Any) -> str:
    """``/advisor`` 输出：审阅模型路由、执行器身份、当前是否生效、如何修改。"""
    provider, model = advisor_route()
    entries = ctx.get_config("disabled_for_models", default=[])
    state, reason = advisor_state(entries)
    executor_provider, executor_model, effort = executor_identity()
    lines = [
        "Advisor",
        f"  reviewer:  {provider or 'auto'} / {model or '(provider default)'}   (auxiliary.advisor)",
        f"  executor:  {executor_provider}/{executor_model}   effort={effort or 'unset'}",
        f"  state:     {state} — {reason}",
        f"  blocklist: {json.dumps(entries, ensure_ascii=False)}",
        "  change:    hermes model -> Configure auxiliary models -> Advisor "
        "(desktop: Settings -> Models -> Auxiliary)",
        "  note:      the tool set is built when an agent starts, so changes apply to new sessions",
    ]
    return "\n".join(lines)


def on_session_end(session_id: str = "", **_kwargs: Any) -> None:
    """``on_session_end`` 钩子：会话结束时清掉该会话的快照，避免长驻进程留着旧上下文。"""
    if session_id:
        _STORE.drop_session(str(session_id))


def register(ctx: Any) -> None:
    """插件入口：注册 auxiliary 槽位、上下文捕获中间件、工具与 ``/advisor`` 命令。"""
    from tools.registry import no_cache_check_fn

    ctx.register_auxiliary_task(
        TASK,
        display_name="Advisor",
        description="Stronger reviewer model consulted by the advisor tool.",
    )
    ctx.register_middleware("llm_request", capture_snapshot)
    ctx.register_tool(
        name=TOOL_NAME,
        toolset="advisor",
        schema=ADVISOR_SCHEMA,
        handler=make_handler(ctx),
        # 配置刚写入时也要立即生效，不能等 30s 的 check_fn TTL 过期。
        check_fn=no_cache_check_fn(make_check_fn(ctx)),
        description="Second opinion from a stronger reviewer model.",
        emoji="🧭",
    )
    ctx.register_command(
        "advisor",
        lambda raw_args: status_text(ctx),
        description="Show the advisor reviewer route and whether the tool is active.",
    )
    ctx.register_system_prompt_section("advisor-guidance", make_prompt_section(ctx))
    ctx.register_hook("on_session_end", on_session_end)
    ctx.on_unload(_STORE.clear)
    logger.info("advisor: 已注册工具、上下文捕获中间件与 auxiliary.%s 任务槽位", TASK)
