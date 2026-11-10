# 上游参考记录

本插件移植自 [juicesharp/rpiv-mono · packages/rpiv-advisor](https://github.com/juicesharp/rpiv-mono/tree/main/packages/rpiv-advisor)。
上游会演进，功能语义（提示词、guideline、清单格式、失败文案）都以固定提交为准，避免"跟着 main 漂移"。

## 固定引用

| 项 | 值 |
| --- | --- |
| 仓库 | `juicesharp/rpiv-mono` |
| 路径 | `packages/rpiv-advisor` |
| npm 包 | `@juicesharp/rpiv-advisor` |
| 版本 | **2.15.0** |
| 提交 | [`9bdbe85885b5f2f69c06e98e93fd182c44bed797`](https://github.com/juicesharp/rpiv-mono/commit/9bdbe85885b5f2f69c06e98e93fd182c44bed797) |
| 提交日期 | 2026-10-24 |
| 核对日期 | 2026-10-28 |
| 核对方式 | 该提交下 `advisor/*.ts`、`prompts/advisor-system.txt`、`LICENSE` 与 npm 包 2.15.0 逐文件 sha256 一致 |

机器可读的同一份记录在 `.upstream.json`，检查脚本读它。

## 版本核对记录

| 核对日期 | 从 → 到 | 结论 |
| --- | --- | --- |
| 2026-10-22 | 初次移植 → 2.13.9 | 提示词、guideline、清单格式、失败文案全部按该提交移植 |
| 2026-10-28 | 2.13.9 → 2.15.0 | 无可移植语义，`advisor.py` 不改（见下） |

### 2.13.9 → 2.15.0（2.14.0 / 2.14.1 / 2.14.2 / 2.15.0）

发布包内只有 4 个文件变化：`advisor/execute.ts`、`advisor/pi-compat.ts`、`advisor/register.ts`、
`package.json`（`rpiv-config` 依赖只升版本，CHANGELOG 之外无代码变化）。
`prompts/advisor-system.txt`、`advisor/messages.ts`、`advisor/inventory.ts`、`advisor/context.ts`、
`advisor/policy.ts` 逐字节未变——即「上游更新时怎么办」清单的第 1–5 项全部无需动作。

三项新行为都是 pi 宿主专属，且 Hermes 宿主已有对应能力：

1. **会话 id（2.14.1）**：pi 用 `advisor-<pi session id>` 派生 prompt-cache key、OpenCode 的
   session 头，并与执行器的缓存 key / Codex websocket 隔离，另在 `session_shutdown` 释放资源。
   Hermes 侧由宿主统一处理：`agent/turn_context.py` 每轮把 `session_id` / `cache_scope` 写进
   ContextVar，`agent/auxiliary_client.py` 据此派生 `prompt_cache_key`；`ctx.llm.complete()` 没有
   session 参数，插件既无处传、也不该传。
2. **瞬时故障重试（2.15.0）**：pi 用 `retryAssistantCall` + `retry` 设置重试 overloaded/限流/断连。
   宿主已有等价策略：`auxiliary_client._should_retry_same_provider()` 对 5xx/408/连接类错误在同
   provider 上重试（`auxiliary.transient_retries` 默认 2，指数退避上限 8s），之后走 fallback 阶梯，
   且显式关掉了 SDK 自带重试。`consult()` 只在重试与 fallback 都失败后才收到异常，失败文案不变。
3. **`session_shutdown` 清理**：pi 的进程退出问题；本插件 `on_session_end` 只清快照，客户端池归宿主。

`PLAN.md` 保留 2.13.9 的移植与验证记录，不回填版本号。

## 已读并用于移植的上游文件

```
index.ts
advisor/index.ts      advisor/register.ts   advisor/execute.ts     advisor/context.ts
advisor/messages.ts   advisor/inventory.ts  advisor/policy.ts      advisor/prompt.ts
advisor/config.ts     advisor/command.ts    advisor/handlers.ts    advisor/restore.ts
advisor/state.ts      advisor/pi-compat.ts
prompts/advisor-system.txt
docs/tool-reference.md  docs/configuration.md
LICENSE
```

`command.ts` / `handlers.ts` / `restore.ts` 是模型选择器、`advisor.json` 持久化与会话内剥离通知，
本插件用 Hermes 原生 `auxiliary.advisor` 槽位 + `check_fn` + 入口再判定替代，因此不移植；
`pi-compat.ts` 是 pi-ai 入口兼容层，无关。

## 上游更新时怎么办

```bash
./scripts/check-upstream.sh      # 对比固定提交 / 版本与上游最新，有漂移返回 1
```

有漂移时的核对清单：

1. **提示词与 guideline**：`prompts/advisor-system.txt`、`advisor/register.ts` 里的
   `ADVISOR_DESCRIPTION` / `DEFAULT_PROMPT_GUIDELINES` → 同步到 `advisor.py` 的
   `SYSTEM_PROMPT` / `ADVISOR_DESCRIPTION`。
2. **清单格式**：`advisor/inventory.ts`（`### name` + 描述 + 稳定序列化参数）→ `inventory_text()`。
3. **上下文整形**：`advisor/context.ts`（剔除尾部 advisor 调用、保证 user 收尾）→
   `strip_inflight_advisor_call()` / `ADVISOR_NUDGE`。
4. **失败文案与空响应重试**：`advisor/messages.ts` + `advisor/execute.ts`（R6.4 单次重试）→
   对应常量与 `consult()`。
5. **禁用规则**：`advisor/policy.ts`（`minEffort` 的 fail-soft 排序）→ `executor_blocked()`。
6. 跑 `python3 tests/test_advisor.py`，再用临时 `HERMES_HOME` 跑
   `hermes plugins validate` / `doctor`（见 `PLAN.md` 的验证记录）。
7. 更新 `.upstream.json` 的 `version` / `commit` / `commit_date` / `checked_at`，
   以及本文件的固定引用表。

宿主侧限制（工具定义在 agent 启动时生成、插件工具被 `tool_search` 折叠、系统提示段落收到
`MappingProxyType`）记录在 `PLAN.md`，上游变更不影响这些结论，但 Hermes 升级后需要复查。
