# hermes-advisor 设计计划

把 pi 扩展 [`@juicesharp/rpiv-advisor`](https://pi.dev/packages/@juicesharp/rpiv-advisor)（对照版本
**2.13.9**）移植为 Hermes 通用 Python 插件（`~/.hermes/plugins/advisor/`）：执行器模型可通过零参数
`advisor()` 工具，把当前**已解析上下文**交给一个更强的审阅模型，取回计划、纠偏或停止信号。

## 功能对照

| rpiv-advisor（pi） | 本插件（Hermes） | 说明 |
| --- | --- | --- |
| 零参数 `advisor` 工具 | `ctx.register_tool("advisor", …)`，`parameters` 为空对象 | 执行器无需传参 |
| 转发「已解析 LLM 上下文」（压缩后） | `llm_request` middleware 捕获 `request["messages"]` | 每次 provider 请求前捕获，压缩/裁剪后语义一致 |
| 含本轮已发生的工具调用与结果 | 同上：工具循环每轮请求前都会刷新快照 | 快照天然不含「正在发生的 advisor 调用本身」 |
| 工具清单前缀（`getAllTools()` 全量） | 读 `tools.registry` 的整个注册表（名字/描述/参数） | 不用请求里的 `tools`：默认配置下那里只剩 `tool_search` 桥工具 |
| 审阅者无工具、纯文本回答 | `ctx.llm.complete(messages=…)` 只做一次 chat 调用 | 不传工具、不写回 transcript |
| `/advisor` 选择审阅模型 + effort，持久化 | `ctx.register_auxiliary_task("advisor")` → 原生 `auxiliary.advisor` 槽位 | 选择器复用 `hermes model` → Configure auxiliary models / Desktop Settings；持久化、profile、密钥全部宿主负责 |
| `advisor.json` 中 `disabledForModels` | 插件设置 `plugins.entries.advisor.settings.disabled_for_models` | 支持字符串条目与 `{model, min_effort}` |
| 无模型时把工具从激活集移除（零提示成本） | `check_fn` 未配置即返回 `False`，schema/描述不进入提示词 | 用 `no_cache_check_fn` 绕过 30s TTL |
| 结果回传为工具结果 + 提示执行器复述要点 | 工具描述写入上游 7 条 guideline；系统提示另有同样 7 条的精简版（1426 字符） | 指引段落在未配置时整段不渲染（零 token） |
| envelope 带 effort / stopReason | `advisor.effort` + `advisor.stop_reason`（completed / empty response / aborted / error） | 取消类异常按类名识别，文案对齐上游 |
| `/advisor` 通知（restored/disabled/…） | `/advisor` 命令输出状态行 | Hermes 无等价通知通道，改为按需查询 |

## 架构

```
register(ctx)
├── ctx.register_auxiliary_task("advisor", …)          # 审阅模型槽位（provider/model/effort/timeout）
├── ctx.register_middleware("llm_request", capture)    # 只读：捕获有效上下文快照（返回 None）
├── ctx.register_tool("advisor", …)                    # 零参数工具 + check_fn 门控 + 入口再判定
├── ctx.register_system_prompt_section("advisor-guidance", …)   # 未配置时返回空串
├── ctx.register_command("advisor", status)            # 状态查询
└── ctx.register_hook("on_session_end", drop_session)  # 会话结束清快照
```

数据流：`provider 请求 → capture 快照 (session_id, task_id) → 模型调用 advisor() →
组装 system + 工具清单 + transcript → ctx.llm.complete(task="advisor") → JSON 工具结果`。

快照按 `(session_id, task_id)` 隔离，最多保留 16 个，写入加锁；子代理任务各自独立。
**fail-closed**：路由未明确选中（`model: auto`、provider 空/`auto`）、载荷无法重建完整上下文、
快照执行器命中 blocklist 时，工具与捕获一律停用，绝不退回主模型自我审阅。

## 已验证的宿主限制（决定实现边界）

1. **捕获点**：`pre_llm_call` 每轮只触发一次，拿不到本轮后续工具结果；`pre_api_request` 载荷经过
   脱敏与截断。`llm_request` middleware 是唯一「受支持的、每个 provider 请求都刷新、不截断」的
   只读入口（`hermes_cli/middleware.py`、`agent/turn_api_request.py:143`），故以此为准。
2. **工具定义在 agent 构造时生成**（`agent/agent_init.py:1119`），`check_fn` 每个 agent 实例评估
   一次。因此「未配置即隐藏」与 `disabled_for_models` 在**新会话**生效；同一会话内切换模型不会
   重建工具定义，所以 `consult()` 入口再做一次 fail-closed 判定。
3. **插件工具默认被渐进披露吞掉**：`tools/tool_search.py` 把非核心/非 GUI 会话面的工具（即所有
   插件工具）收进 `tool_search` 桥，工具描述不进提示词，请求里的 `tools` 也只剩桥工具。因此
   （a）工具清单改读完整注册表；（b）另注册 `register_system_prompt_section` 形式的精简指引，
   未配置时返回空串（宿主整段跳过，零 token）。需要工具直接可见时用户可关 `tool_search`。
4. **会话冻结配置**：网关会话把 `auxiliary` 冻结进 ContextVar，
   `agent.auxiliary_task_config._get_auxiliary_task_config(task)` 是宿主自己的解析入口；插件优先
   用它，磁盘 `config.yaml` 只作兜底，避免"读到旧配置"。
5. **系统提示段落收到的是 `MappingProxyType`**（`hermes_cli/plugins_dispatch.py`），必须按
   `Mapping` 判断；只认 `dict` 会让会话身份变空、blocklist 形同虚设。
6. **调用级模型覆盖走环境变量**：`hermes -m` / TUI 启动写 `HERMES_INFERENCE_MODEL` 与
   `HERMES_INFERENCE_PROVIDER` 而不改配置文件，执行器身份必须优先读它们。
7. **multimodal 降级**：图片等内容块在 transcript 中渲染为 `[image omitted]`，只做文本审阅。
8. **`/advisor` 不写宿主配置**：写 `auxiliary.advisor` 的唯一受支持入口是宿主的模型选择器。
9. **不做长度裁剪**：上游也不裁剪；超出审阅模型能力时让调用显式失败，而不是静默丢信息。

## 验证计划

- 自检脚本（`tests/test_advisor.py`，纯 assert，不依赖 pytest / 不导入宿主）：
  捕获隔离与同轮刷新、不可用载荷清旧快照、会话冻结路由优先、`auto` fail-closed、只读 Mapping 的
  会话身份、调用级环境覆盖、完整注册表清单与兜底、尾部 advisor 调用剔除保留助手文本、
  blocklist（含 `min_effort` 边界）、envelope 成功/空响应单次重试/异常、未配置与被禁用时零请求、
  会话结束清快照、注册装配。每个用例前复位替身，避免污染 pytest 进程。
- 临时 `HERMES_HOME` 下 `hermes plugins validate` + `hermes plugins doctor`，再用宿主运行时验证：
  中间件投递与捕获、`hermes tools`/`get_tool_definitions` 的门控、`render_system_prompt_sections`
  的真实渲染、`/advisor` 命令输出、`tool_search` 能否发现工具。不接触用户实况环境，不触发付费调用。

### 验证结果

（见本文件末尾"验证记录"，随每次改动更新。）

## 非目标

- 指南文本覆盖（上游 `guidance.*`）——改文本需改插件文件。
- 会话内模型切换时的即时剥离与通知。
- 图片/文件级审阅、流式输出。

## 验证记录（2026-10-22，Hermes 0.22.4，临时 `HERMES_HOME`）

| 场景 | 结果 |
| --- | --- |
| `hermes plugins validate` | 通过；1 工具 / 1 钩子 / 1 中间件与 manifest 一致；`security scan — safe`；⚠ `private core imports`（`agent.auxiliary_task_config._get_auxiliary_task_config`，见下） |
| `hermes plugins doctor` | 通过：`1 tool(s), 1 hook(s)` |
| 未配置 / `model: auto` | `check_fn` False；工具不进工具定义；无系统提示段落；**中间件不保留快照**；`tool_search` 看不到 advisor |
| 已配置 + 默认 `tool_search` | 工具被折叠进桥（默认 20 个定义里没有 advisor，`tool_search` 能看到 `advisor` 源） |
| 已配置 + `tools.tool_search.enabled: off` | `advisor` 直接出现在工具定义里（34 个工具） |
| 已配置 | `render_system_prompt_sections` 渲染 `advisor-guidance`（走真实 `MappingProxyType`） |
| blocklist `min_effort: high` + 执行器 `high` | `check_fn` False；无提示段落；`/advisor` 显示 `blocked` |
| `llm_request` 中间件投递 | 捕获成功且 `changed=False`（不改写宿主请求） |
| `/advisor` 命令 | 输出 reviewer / executor / state / blocklist / 怎么改 |
| 自检脚本 | 27/27 通过 |
| guidance 段内容 | 7 条上游 guideline 要点，1426 字符（宿主单段上限 4000，聚合上限 8000）；早期 306 字符摘要版已扩写 |

未覆盖（残留风险）：`plugins.isolation: host` 下中间件跨进程投递；真实 provider 付费调用；
Responses 增量形态（`instructions` + `previous_response_id`）。

### 已决策：有效 auxiliary 路由的读取方式

宿主没有公开 API 返回「某 auxiliary 任务当前解析出的 provider/model」。当前实现调用
`agent.auxiliary_task_config._get_auxiliary_task_config(task)`（宿主自己的读时解析入口，underscore
命名），因此 `hermes plugins validate` 给出 ⚠ private core imports 警告（不阻断）。备选：

**用户选择：保留私有读取。** 理由：网关冻结配置与磁盘配置都能正确判定，fail-closed 最严谨；
代价是 `validate` 的那条 ⚠（不阻断加载）以及核心升级时需要复查该入口。另外两个被否的备选：
只读磁盘 `config.yaml`（网关会话可能读到比冻结配置更新的值，极端情况下磁盘说"已配置"、实际路由
退回主模型，等于主模型审阅自己）；向核心提公开 seam（更干净，但超出本次插件范围）。

### 已读的上游源码（`@juicesharp/rpiv-advisor` 2.13.9）

`index.ts`、`advisor/{index,register,execute,context,messages,inventory,policy,prompt,config,command,handlers,restore,state,pi-compat}.ts`、
`prompts/advisor-system.txt`、`docs/{tool-reference,configuration}.md`。其中 `command.ts` /
`handlers.ts` / `restore.ts` 是选择器、持久化与生命周期通知，本插件用 Hermes 原生模型选择器与
`check_fn` + 入口再判定替代，不需要移植。
