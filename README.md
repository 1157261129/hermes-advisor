# hermes-advisor

Hermes Agent 插件，移植 pi 扩展 [`@juicesharp/rpiv-advisor`](https://pi.dev/packages/@juicesharp/rpiv-advisor)
（对照版本 **2.13.9**，源码 `rpiv-mono/packages/rpiv-advisor`）：给执行器模型配一个零参数的
`advisor` 工具，随时把**整段已解析上下文**交给一个更强的审阅模型，拿回计划、纠偏或停止信号，
然后继续干活。

审阅模型走 Hermes 原生的 `auxiliary.advisor` 槽位——模型选择、认证、profile、推理强度、超时与
持久化全部由宿主负责，插件不接触密钥、不新建配置文件、不写 `config.yaml`。

## 安装

```bash
hermes plugins install 1157261129/hermes-advisor
hermes plugins enable advisor
```

新会话生效。想先在本地改代码再装，也可以直接放目录（热重载更顺手）：

```bash
git clone https://github.com/1157261129/hermes-advisor ~/workspace/hermes-advisor
cp -r ~/workspace/hermes-advisor ~/.hermes/plugins/advisor
hermes plugins enable advisor
```

验证：

```bash
hermes plugins doctor ~/.hermes/plugins/advisor      # 注册路径
hermes plugins validate ~/.hermes/plugins/advisor    # catalog 同款校验
```

## 配置审阅模型

`advisor` 是 `hermes model` 辅助模型选择器里的一等槽位：

```bash
hermes model          # → Configure auxiliary models → Advisor
```

桌面端在 Settings → Models → Auxiliary。等价的 `config.yaml`：

```yaml
auxiliary:
  advisor:
    provider: anthropic           # 留空或 auto 表示没有偏好
    model: claude-opus-4-5
    reasoning_effort: high        # 可选：审阅者的思考强度
    timeout: 180                  # 可选：审阅调用超时（秒）
```

**fail-closed**：`model: auto` 或 provider 为空/`auto` 都算未配置——此时工具、系统提示指引、
快照捕获全部停用，工具也不会退化成「用主模型审阅自己」。网关会话会把 `auxiliary` 冻结进
上下文，插件优先读这份冻结配置，其次才是磁盘 `config.yaml`。

在会话里输入 `/advisor` 看当前路由与生效状态。

### 跳过已经足够强的主模型

`plugins.entries.advisor.settings.disabled_for_models` 按执行器模型屏蔽：

```yaml
plugins:
  entries:
    advisor:
      settings:
        disabled_for_models:
          - anthropic/claude-opus-4-5                 # 任何强度都屏蔽
          - model: openai/gpt-5.2                     # 仅当执行器强度 ≥ high 时屏蔽
            min_effort: high
```

未设置或 `off` 的执行器强度永远不满足 `min_effort`；`provider:model` 旧写法会自动归一。
`hermes -m` 这类只设环境变量的调用级覆盖也会参与判定。

## 它做什么

* **零参数**：`advisor()` 不接受任何参数，整段对话自动转发——任务、每次工具调用、每个结果。
* **上下文是真的"当前"**：取自宿主 `llm_request` 中间件看到的**装配完成的请求**，压缩/裁剪之后
  的语义，包含本轮已发生的工具调用与结果；正在进行的 `advisor()` 调用本身不转发。
* **不做裁剪**：transcript 与工具清单按原样外发。超出审阅模型能力时显式报错，而不是悄悄扔掉
  旧消息（`_MAX_BLOCK_CHARS` 那类"顺手截断"是功能缩水，不是宿主限制）。
* **审阅者没有工具**：只做一次 chat 调用，回答只作为工具结果回到执行器，不写进 transcript。
* **工具清单是完整注册表**：按上游 `getAllTools()` 语义取整个工具注册表（名字、描述、参数），
  而不是 `tool_search` 折叠后的三个桥工具。
* **回答只出现一次**：工具描述要求执行器把要点复述到可见回复里，不要只留在折叠的工具卡片里。
* **空回答只重试一次**：连续两次为空则明确报错，不进入循环。

## 与上游的差异

| 上游（pi） | 这里（Hermes） | 原因 |
| --- | --- | --- |
| `/advisor` 选择器写 `advisor.json` | `hermes model` → 辅助模型槽位 | Hermes 已有 profile/密钥/持久化的统一入口，插件不重复实现 |
| 原样转发 provider 消息数组 | 渲染成文本 transcript | 审阅模型可能是另一家 provider，`tool_call_id`、`reasoning_content`、缓存标记原样转发会被拒收；渲染不丢信息，也不裁剪 |
| `promptSnippet` + `promptGuidelines` | 工具描述全文 + 一段可开关的系统提示指引 | Hermes 插件工具默认被 `tool_search` 渐进披露，描述常常不进提示词 |
| 会话内切换模型即时剥离工具 | 新会话生效 | 工具定义在 `AIAgent.__init__` 生成（`agent/agent_init.py:1119`）；调用入口另有一次 fail-closed 判定 |

## 已知限制

* **默认被 tool_search 折进桥**：插件工具都属于可延迟面，模型可通过 `tool_search("advisor")`
  找到并调用；要让 `advisor` 直接出现在工具列表里，设 `tools.tool_search.enabled: off`。
* **只做文本审阅**：transcript 里的图片渲染为 `[image omitted]`。
* **Responses 增量形态**：只认 `messages` / 列表形态的 `input`；`instructions` +
  `previous_response_id` 这类无法重建完整上下文的请求会清掉旧快照并让工具报「无上下文」。
* **改配置后需要新会话**：工具集在 agent 启动时生成，`/advisor` 会提示这一点。
* `plugins.isolation: host` 隔离模式下的跨进程中间件投递未做端到端验证（默认 `in_process`）。
* **有效路由读取用了核心私有入口**：`agent.auxiliary_task_config._get_auxiliary_task_config`，
  `hermes plugins validate` 会给出 ⚠ private core imports 警告（不阻断）。宿主暂无公开 API 能区分
  「网关会话冻结的 auxiliary 配置」与磁盘 `config.yaml`；只读磁盘会在两者不一致时误判，故保留私有
  读取。若你只跑 CLI/Desktop 且更在意零警告，可改为只读 `config.yaml`。

## 自检

```bash
python3 tests/test_advisor.py     # 24 项，纯 assert，不依赖 pytest / 不导入 Hermes / 不发模型调用
```

覆盖：会话/任务隔离、同轮工具结果、载荷不可用时清旧快照、会话冻结路由优先、`auto` fail-closed、
只读 Mapping 的会话身份、blocklist（含 `min_effort` 边界）、完整注册表清单、尾部 advisor 调用
剔除保留助手文本、空回答单次重试、未配置/被禁用时零请求。

## 结构

```
plugin.yaml               # manifest：1 个工具、1 个中间件、1 个钩子
__init__.py               # 导出 register
advisor.py                # 捕获有效上下文 → 组装审阅请求 → 调用 auxiliary.advisor → 门控与状态
tests/test_advisor.py
PLAN.md                   # 设计计划、功能对照、已验证的宿主限制与验证记录
UPSTREAM.md               # 固定的上游提交/版本，以及上游更新时的核对清单
.upstream.json            # 上表机器可读版，供 scripts/check-upstream.sh 使用
scripts/check-upstream.sh # 检查上游是否有新提交/新版本（有漂移退出码 1）
```

上游固定引用见 `UPSTREAM.md`（当前 `@juicesharp/rpiv-advisor` 2.13.9 @
`c287cddb3eba50b9ecf670dbb04fecb9ef192d7e`）；定期跑 `./scripts/check-upstream.sh`。

## License

MIT，见 `LICENSE`。上游 `@juicesharp/rpiv-advisor`（Copyright (c) 2026 juicesharp）同为 MIT，
本仓库保留其版权声明；系统提示、工具描述与 guideline 文本来自上游。
