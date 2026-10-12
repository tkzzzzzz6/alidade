# 工作方案：计划驱动的子 Agent 并行分析

- 状态：阶段 0–5 已完成（阶段 5：2026-10-11），待用真模型评测；阶段 6（评测调参）待开始，开关默认关闭
- 预计工作量：约 7 个工作日，分 6 个阶段，每个阶段结束都能单独验收和提交
- 相关代码：`py-src/alidade/analyst/`（后端）、`src/views/SimpleChartRecBox.tsx`（前端）、`eval/`（评测）

## 1. 目标

宽泛或多角度的问题，在用户确认方向后，由主 Agent 把计划里**互相独立的步骤**交给几个子 Agent 同时完成，再汇总成一个结论。

**要达到的效果（用评测验收）：**

| 指标 | 目标 |
|---|---|
| 多角度题的平均耗时 | 比单 Agent 降低 30% 以上（现在约 45–65 秒） |
| 多角度题的成功率 | 不低于单 Agent |
| 简单题（查询、单图） | 耗时和 token 不变：不拆分，不受影响 |
| 未命中缓存的输入 token | 增加不超过 30% |

**不做的事：**

- 子 Agent 不向用户反问，也不再拆出下一级子 Agent（只有一层）。
- 不做跨对话、跨用户的并行，那一层现在已经支持了。
- 不改澄清流程：拆分只发生在用户确认方向之后。

## 2. 现状

| 方面 | 现在的做法 | 位置 |
|---|---|---|
| 主循环 | LangGraph：`prepare → agent ⇄ tools`，`agent → act → agent / await_user / finish` | `engine/graph.py` |
| 工具调用 | `parallel_tool_calls=False`，每次回复只能调一个工具 | `engine/llm.py` 的 `open_stream` |
| 动作 | 每轮只执行一个，多余的丢掉并告诉模型 | `graph.py` 的 `_commit_action` |
| 计划 | `update_plan` 只有 `title` 和 `status`，只用来给用户看进度；所有步骤由同一个 Agent 顺序完成 | `engine/planning.py` |
| 状态 | `AnalystState` 没有 reducer，节点返回整个新值 | `engine/state.py` |
| Python 会话 | 每次运行一个 `CodeExecutor`（一个沙箱进程），按 `run_id` 存取变量 | `engine/execution.py` |
| 运行时对象 | `executor`、`_reasoning_log`、`_begin_payload/_end_payload` 都挂在同一个 Agent 实例上 | `analyst/agent.py` |
| 前端 | 一个 `thinkingSteps` 数组、一个 `agentPlan`，假设事件按顺序一条条到达 | `SimpleChartRecBox.tsx`（约第 1000–1540 行） |
| 提示词 | 提到了 `delegate` 动作，但实际没有这个工具 | `engine/prompts.py` 第 41 行 |

这些决定了下面几个必须解决的问题：
- 状态要加 reducer，多个分支才能同时写结果；
- 每个子 Agent 要有自己的运行时对象，不能共用主 Agent 的实例；
- 前端事件要带上"属于哪一步"。

## 3. 总体设计

```
prepare → agent ⇄ tools
            │
            ├─ act(update_plan 带 depends_on) ──→ dispatch
            │                                      │ 找出依赖已满足、未开始的步骤
            │                                      │ ≥2 个 → Send("subagent", 子任务) × N（最多 3 个并发）
            │                                      │ 0–1 个 → 回到 agent，照旧顺序做
            │                                      ▼
            │                     subagent ×N（各自：LLM ⇄ 只读工具 → visualize，预算 2 个动作）
            │                                      │ 结果通过 reducer 追加到 subagent_results
            │                                      ▼
            └──────────────────────────────── collect：把各步发现作为一条消息交给主 Agent
                                                   │ 还有待做的步骤？→ dispatch
                                                   ▼
                                                 agent：读发现、写最终结论 → finish
```

**什么时候拆：** 计划里同时有 2 个以上"依赖已满足、还没开始"的步骤。拆不拆由主模型列计划时决定（它给每一步标 `depends_on`），代码只按依赖调度。没有计划、或计划是一条直线的，行为和现在完全一样。

**功能开关：** `ALIDADE_SUBAGENTS=1` 时启用，默认关闭，评测通过后再改成默认开启。

### 3.1 数据约定

计划步骤（`update_plan` 扩展）：

```json
{"id": "s2", "title": "各国 CO₂ 排放趋势", "status": "pending",
 "depends_on": [], "goal": "按国家画 1990–2020 年 CO₂ 排放折线，找出降幅最大的 3 个国家"}
```

- `id`：模型自己起，例如 `s1`、`s2`；`depends_on` 引用这些 id；
- `goal`：给子 Agent 的完整任务说明，因为它看不到对话；
- 两个字段都可选，旧格式的计划照常工作。

子任务（`dispatch → subagent`）：

```python
class SubTask(TypedDict):
    step_id: str
    goal: str
    user_question: str        # 用户原始问题和澄清时的回答，用来统一口径
    input_tables: list[dict]  # 表结构和数据体检，与主 Agent 相同
    max_actions: int          # 默认 2
```

子结果（`subagent → collect`）：

```python
class SubResult(TypedDict):
    step_id: str
    status: str               # success | failed | timeout | budget_exhausted
    findings: str             # 3–5 行，带具体数字
    open_questions: list[str] # 拿不准、需要主 Agent 或用户决定的事
    charts: list[dict]        # 本步产出的图表（与现在的 completed_steps 格式相同）
    tokens: int
    duration_s: float
```

### 3.2 必须遵守的规则

1. 子 Agent 只有 `execute_python_script`、`inspect_source_data`、`visualize` 三个工具，没有 `ask_user`、`update_plan`、`load_skill`。
2. 每个子 Agent 使用**独立的 `CodeExecutor`**（`run_id` 加上 `-<step_id>` 后缀）和独立的日志、payload；不共用主 Agent 实例上的可变状态。
3. 并发上限 `ALIDADE_MAX_SUBAGENTS`，默认 3，用信号量控制；遇到 429 走现有的指数退避重试。
4. 单个子 Agent 超时（默认 120 秒）或失败，只把这一步记为失败，其他步骤照常完成，主 Agent 在结论里说明哪一步没做成。
5. 用户点"停止"时，所有子 Agent 在下一次调用 LLM 之前检查取消标志并退出。
6. 子 Agent 产出的每个事件都带 `step_id`；主 Agent 的事件不带。

## 4. 分阶段计划

每个阶段都要求：后端测试全部通过、对应的新测试通过、能单独提交。

### 阶段 0：评测准备（0.5 天）

先有尺子再动手，否则无法证明并行是否有用。

- `eval/questions.yaml` 新增类别 `multiangle`，4–5 道题。题目明确要求 2–3 个独立角度，例如"对比各国可再生能源占比的变化，以及 CO₂ 排放的变化"。每题用 `mentions_all` 检查每个角度都有结论，并要求至少 2 张图。
- `eval/report.py` 新增：多步/多角度题的平均耗时、使用子 Agent 的比例、子 Agent 失败率。
- `eval/run_eval.py` 记录 `subagent` 事件：拆了几个、各自耗时、状态。
- 用现在的单 Agent 版本跑一遍新题，作为基线。

**验收：** 新题在单 Agent 版本上能跑通，报告里出现耗时基线。

### 阶段 1：一次回复调多个只读工具（0.5–1 天）

- `engine/llm.py`：`parallel_tool_calls` 改为可配置，默认 `True`（`ALIDADE_PARALLEL_TOOLS=0` 时退回旧行为）。
- `graph.py` 的 `tools_step` 已经支持按顺序执行多个只读调用，基本不用改。`_commit_action` 的"一轮一个动作"规则保持不变。
- 提示词：删掉 `delegate`；说明"互相独立的查看可以在一次回复里同时发出"。
- 测试：一次回复包含 2 个只读调用加 1 个动作时，只读调用全部执行、只保留第一个动作。

**验收：** 全量评测成功率不下降，平均 LLM 调用数和耗时下降。这一阶段可以单独发布。

### 阶段 2：计划支持依赖（0.5 天）

- `planning.py`：`PLAN_TOOL` 增加可选的 `id`、`depends_on`、`goal`；`normalize_steps` 负责校验：未知 id、自己依赖自己、有环的依赖，都退化为顺序执行。
- `PLANNING_PROMPT`：说明什么样的步骤算互相独立（不用到别的步骤的结果），并给出一个带 `depends_on` 的例子。
- 新增纯函数 `ready_steps(plan) -> list[step]`：返回依赖都已完成、自己还是 `pending` 的步骤。
- 测试：直线依赖、两步并列、有环、引用不存在的 id。

**验收：** 开关关闭时行为不变；在多角度题上，模型给出的计划里大多数有可并列的步骤。

### 阶段 3：子 Agent 运行器（1.5 天）

- 新文件 `engine/subagent.py`：`run_subagent(task: SubTask, runtime) -> SubResult`。
  - 内部是一个精简的循环：LLM ⇄ 只读工具 → `visualize`，最多 `max_actions` 个动作、6 轮查看；
  - 复用 `llm.stream_completion`、`execution.CodeExecutor`、`chart_check`；
  - 系统提示复用主 Agent 的大部分内容（命中缓存），末尾加一段"你是子任务执行者：只做这一步、不要反问、最后用 3–5 行写出带数字的发现，把拿不准的事写进 open_questions"。
- 把挂在 `AnalystAgent` 实例上的运行时状态（executor、reasoning log、payload）抽成一个 `RunContext`，主 Agent 和每个子 Agent 各持一份。这是改动面最大的一步，要先补足现有行为的测试再动手。
- 测试（用假的 LLM 客户端）：正常完成、动作预算用完、代码报错后修复、超时、两个子 Agent 同时运行时变量互不可见。

**验收：** 单独调用 `run_subagent` 能产出图表和发现；并发运行两个时，沙箱、日志、图表互不串。

### 阶段 4：调度与汇总（1 天）

- `state.py`：新增 `subagent_results: Annotated[list[SubResult], operator.add]`，这是第一个需要 reducer 的字段；`plan` 在 collect 节点统一更新，避免并发写。
- `graph.py`：
  - 新增 `dispatch` 节点：用 `ready_steps` 选出步骤，不少于 2 个时返回 `[Send("subagent", task) ...]`，否则回到 `agent`；
  - 新增 `subagent` 节点：调用 `run_subagent`，加超时和信号量；
  - 新增 `collect` 节点：把各结果写成一条 `[SUBTASK RESULTS]` 消息（每步一段：状态、发现、待定问题、图表 id），更新计划状态，图表按步骤顺序并入 `charts`；
  - 路由：`act` 处理带依赖的 `update_plan` 后进入 `dispatch`；`collect` 之后有新的可执行步骤就回到 `dispatch`，否则回到 `agent` 写结论。
- 汇总提示：要求主 Agent 检查各步口径是否一致（时间范围、单位、筛选条件），有矛盾就在结论里指出，不要把冲突的数字并列给出。
- 断点续跑：子 Agent 里没有 `interrupt`，暂停只发生在主 Agent；确认 `durability="exit"` 下，分发到一半被取消时能正常结束。
- 测试：两步并行加一步依赖（先 2 并行、再 1 顺序）、一个子 Agent 失败、并发上限生效、开关关闭时图结构不变。

**验收：** 假模型下端到端跑通；真模型下多角度题能拆分，结论覆盖所有角度。

### 阶段 5：事件协议与前端（1–1.5 天）

- 后端：子 Agent 的所有事件（`tool_start`、`tool_result`、`chart_check`、`result` 等）带 `step_id`；新增 `subagent` 事件，`status` 为 `started` / `finished` / `failed`。
- 前端 `SimpleChartRecBox.tsx`：
  - 计划清单可以同时有多个"进行中"的步骤，旁边显示"并行"标记；
  - 状态栏显示"第 2、3 步同时进行中"，不再逐条刷工具调用；
  - `thinkingSteps` 按 `step_id` 分组，避免不同子 Agent 的步骤交错成一团；
  - 图表到达顺序不固定，按步骤顺序插入，并标上来自哪一步。
- 中英文文案加到 `src/i18n/locales/{en,zh}/messages.json`。
- 测试：vitest 覆盖事件交错到达时的分组和排序；`tsc` 通过。

**验收：** 浏览器里跑一道多角度题，能看到并行进度，图表顺序正确，停止按钮能中断所有子 Agent。

### 阶段 6：评测与调参（1 天）

- 同一份代码，开关关闭和开启各跑一遍：`multiangle`、`multistep`、`ambiguous` 加若干简单题，每题 2–3 次。
- 对比：耗时、成功率、总 token、未命中缓存 token、子 Agent 失败率、结论里口径冲突的次数（LLM 评分时单独打分）。
- 调整：并发上限、子 Agent 动作预算、超时、什么情况下值得拆（例如两步都只是查一个数时不拆）。
- 更新 `eval/README.md` 和 CHANGELOG，在 `eval/` 下写一份分析报告。

**验收：** 达到第 1 节的目标；达不到的，在报告里如实说明原因，开关保持默认关闭。

## 4.5 实际实现（阶段 3–5）与原设计的差异

**更正（2026-10-11）：**
- 阶段 2 提示词里的依赖示例（"renewable share by country" / "CO₂ trend by country"）和评测题 `multi-energy` 几乎一样，当时测得的"计划里有可并行步骤的比例 62%"可能偏高。示例已换成无关领域，需要重测。
- 第一次真实评测（`new-sub-on`）里子 Agent 一次都没触发：计划门槛"至少 3 步"写在提示词主体里，开关打开后追加的"2 个独立分析就写计划"和它冲突，模型听了前者。现在开关打开时直接改写门槛那句话（`planning._THRESHOLD`），并有测试保证两种文字不会同时出现。

**阶段 5（前端）的实际做法：**

- 后端的 `subagent` 事件状态是 `started` 和最终状态（`success` / `budget_exhausted` / `failed` / `timeout` / `stopped`），不是设计里的 `finished`；
- 运行中的步骤列表仍是一个字符串数组，没有改成按步骤分组的结构：子 Agent 的每一行前面加 "[第 2 步] " 标签，"标记工具完成"和"出图时取走本步的行"都只作用于同一标签的行（`src/views/subagentEvents.ts`），所以交错到达也不会把别的步骤的行标成完成或带进别的图表历史；
- 每步最后一次的图表说明和输入表分开记（`PerStep`），图表标题前加"第 N 步 · "；
- 图表按到达顺序插入，没有按步骤顺序重排：重排要改线程里已插入节点的位置，收益不大；标签已经能看出来自哪一步；
- 状态栏在计划里有 ≥2 个进行中的步骤时显示"第 2、3 步同时进行中"；
- 停止：之前用户点停止时，事件流关闭会等整个分发节点跑完，子 Agent 会继续调模型直到结束。现在 `AnalystAgent.run` 在关闭事件流之前先置位 `run_ended`，分发节点看到后让所有子 Agent 在下一次调模型之前停下（有测试）。

**子 Agent 的运行时隔离：** 没有把 `AnalystAgent` 上的运行状态抽成 `RunContext`。每个子 Agent 直接是一个新的 `AnalystAgent` 实例（`engine/subagent.py` 的 `SubtaskAgent`），天然拥有自己的 Python 会话、日志、图表缓冲和自检状态，主 Agent 的代码不用重构。技能注册表每个子 Agent 各建一份（约 9 ms），不在线程间共享。

**调度方式：** 没有用 LangGraph 的 `Send` 加 reducer，而是一个 `subagents` 节点里用有上限的线程池跑子 Agent：

- 并发上限、单步超时、失败隔离都直接由线程池和计时器实现；
- 子 Agent 的事件经队列回到该节点的线程，再统一写入用户的事件流（带 `step_id`），避免多线程直接写流；
- 子 Agent 用内存检查点，不写主运行的 SQLite。子 Agent 不能反问，所以不存在"分发到一半需要暂停恢复"的情况；
- 代价：整个分发是一个图节点，进程中途崩溃时这一轮要重做。

**触发条件：** 模型单独调用 `update_plan`（不和动作一起）之后，如果计划里有 ≥2 个"依赖已完成、还没交出去"的步骤，就进入分发；一轮结束后，新解锁的步骤够 2 个就再分发一轮，最多 3 轮。只剩 1 步时由主 Agent 自己做。

**结果交回主 Agent：** 一条 `[SUBTASK RESULTS]` 消息，每步写明状态、已展示的图、3–5 行发现，并要求主 Agent 检查各步口径是否一致。成功的步骤在计划里标为完成，没做成的保持"待做"，由主 Agent 决定是否自己补。

**子 Agent 的预算：** 提示词里允许 2 张图，实际动作上限是 3，多出的 1 次留给收尾的文字总结；如果没写总结，用已展示图表的标题代替。

**并行实测（模拟模型，每次调用 1 秒，2 核容器）：** 3 个各画 1 张图的步骤，子 Agent 阶段从顺序约 6.3 秒降到并行约 4.1 秒，整次运行 8.4 → 6.4 秒。并行时每步变慢（2.1 → 4.0 秒），原因是几个子 Agent 同时启动 Python 进程、渲染图表，抢 2 个 CPU。真实模型每次调用要等几秒、几乎不占 CPU，收益应更大；评测时用 `-Jobs 1`，避免多道题同时跑互相抢 CPU。

## 4.6 用户控制（2026-10-12）

之前子 Agent 只能用后端环境变量开关，改了要重启；用户也看不到每个子 Agent 在做什么，只能整个停掉。现在：

**每次运行的设置**（`engine/run_settings.py`）

| 设置 | 环境变量（服务器默认） | 网页上可选范围 |
|---|---|---|
| 并行子 Agent | `ALIDADE_SUBAGENTS`（默认关） | 开 / 关 |
| 同时运行几个 | `ALIDADE_MAX_SUBAGENTS`（3） | 1–4 |
| 每个最多几张图 | `ALIDADE_SUBAGENT_MAX_CHARTS`（2） | 1–3 |
| 单步时间上限 | `ALIDADE_SUBAGENT_TIMEOUT`（180 秒） | 30–600 秒 |
| 计划自动推进 | `ALIDADE_PLAN_AUTO_ADVANCE`（默认开） | 开 / 关 |

- 前端"设置 → Agent"里每项可以"跟随服务器默认"或手动指定，选择存在浏览器里（redux-persist），每次提问随请求发 `agent_settings`；没设的项不发，后端用环境变量。评测脚本和 MCP 服务器不发这个字段，行为不变。
- 后端按上表范围截断请求里的值；环境变量只限制最小值（测试里会用 1 秒超时）。
- 设置写进运行的检查点（`run_settings`）。一次运行在 `ask_user` 暂停后恢复，用的是开始时的设置，不会因为中途改了设置而换掉计划工具的格式。
- 运行记录的每段里记下当时的设置，方便对比开 / 关。
- `GET /api/agent/run-settings` 返回服务器默认值和范围，设置页用它显示"服务器默认（关）"。

**单独停止一个子 Agent**（`engine/subagent_control.py`）

- 分发节点给每个交出去的步骤登记一个可停止的句柄，键是（用户身份、运行 id、步骤 id）；步骤结束就注销。
- `POST /api/agent/subagent-stop {run_id, step_id}`：只能停自己的运行；步骤已经结束时返回 `stopped: false`。
- 停止在该子 Agent 下一次调模型之前生效（正在跑的 Python 代码会跑完）。
- 被用户停止的步骤状态是 `cancelled`，计划里标为"跳过"；交给主 Agent 的结果里写明"用户停止了这一步，不要重做，在最终回答里说明"。超时是 `timeout`，整个运行被停止是 `stopped`，三者分开记录（句柄只记第一个原因）。
- 登记表在进程内。后端是单进程、每个运行一个线程，够用；多进程部署时停止请求可能落到别的进程，会返回 `stopped: false`。

**每个子 Agent 的状态记在它的计划步骤上**

计划步骤多了一个 `subagent` 字段：`status`（`queued` → `running` → 结束状态）、`run_id`、`started_at`、`timeout_s`、`duration_s`、`charts`、`chart_titles`、`findings`（子 Agent 的 3–5 行结论）、`error`。

- 后端在分发、开始、结束时更新它，并随 `plan` 事件发给前端；`subagent` 事件结束时也带上 `findings`。
- 模型之后用 `update_plan` 重写计划时，按步骤 id 把 `subagent` 记录带过去（`planning.keep_subagent_info`），不会丢。
- 因为记录在计划里，运行结束后线程里保留的计划也带着它，不用另外存。

**前端**

- 计划清单（`AgentPlanChecklist`）里，交给子 Agent 的步骤下面多一行：
  - 运行中：状态、已用时间（每秒刷新）、它当前在做什么（从运行步骤列表里取这一步最新的一行），以及一个停止按钮，只停这一步，其他步骤继续。点了以后显示"正在停止…"；如果它已经结束，按钮恢复。
  - 结束后：状态（完成 / 超时 / 已被你停止 / 失败）、用时、图表数，"查看结论"展开它的结论原文。
- 标题旁显示"· N 步并行"。
- 运行步骤列表里，被停止和超时的步骤有各自的提示文字。

**测试：** `tests/backend/agents/test_subagent_controls.py`（设置合并与截断、请求覆盖环境变量、设置在暂停后保留、计划记录、单步停止、身份隔离、三个路由）；前端 `agentSettings.test.ts`、`AgentSettingsSection.test.tsx`、`AgentPlanChecklist.test.tsx`、`subagentEvents.test.ts`。

## 4.7 实测结果（2026-10-12，DeepSeek deepseek-flash）

`planned` + `multiangle` 共 8 题，每题 3 次，`-Jobs 1`（不同时跑多道题，耗时可比）。结果目录：`eval/results/new-sub-off3`、`new-sub-on3`。

| 指标 | 关 | 开 | 变化 |
|---|---|---|---|
| 任务成功率 / 结论正确率 | 100% / 100% | 100% / 100% | 持平 |
| 平均耗时 | 25.4 秒 | 24.3 秒 | −4% |
| 耗时中位数 | 25.2 秒 | 22.7 秒 | −10% |
| 平均总 token | 77k | 135k | +76% |
| 平均 LLM 调用数 | 5.3 | 10.9 | ×2.1 |
| 未命中缓存的输入 token | 2.9k | 7.3k | ×2.5 |
| 每题估算费用（低峰价） | $0.0027 | $0.0043 | +59% |
| 用了子 Agent 的题 | — | 22 / 24 | 共 55 个子 Agent，0 失败 |

按题看（每题 3 次的平均）：8 题里 4 题变快、4 题变慢，中位数 −0.9%。变快最多的是两道多角度题（multi-energy −17%、multi-gas −18%），变慢最多的是 planned-gas（+11%）。

**为什么几乎没变快：**
- 并行本身是有效的：每次平均 2.5 个子 Agent，顺序跑要 30.2 秒，并行只要 12.5 秒，省下 17.7 秒；
- 但每个子 Agent 是冷启动：自己的系统提示（缓存未命中）、重新查看数据、最后再写一段总结，一步平均 12.1 秒，比主 Agent 顺手做同一步慢得多；
- 主 Agent 还多了两次调用：先写带依赖的计划，最后汇总各步结果。开的时候 96% 的题用了计划，关的时候是 50%。

结论：这组题每一步只画 1 张图、几秒就能做完，并行省下的时间被启动和汇总的开销抵消，token 却接近翻倍。所以**默认保持关闭**，作为设置项提供。要让它真正划算，可以：
1. 只在步骤够重时才分发（例如 ≥3 个独立步骤，或每步需要多次查询）；
2. 把主 Agent 已经看过的表结构直接交给子 Agent，省掉重复的数据查看，并给子 Agent 一份更短的系统提示；
3. 子 Agent 走小模型 / 低思考档（模型路由已支持）。

## 5. 时间表

| 天 | 内容 | 可交付 |
|---|---|---|
| 第 1 天 | 阶段 0 + 阶段 1 | 多角度评测题、耗时基线；并行只读工具调用（可单独发布） |
| 第 2 天 | 阶段 2 + 阶段 3 开头（抽出 `RunContext`） | 带依赖的计划 |
| 第 3 天 | 阶段 3 | 子 Agent 运行器和测试 |
| 第 4 天 | 阶段 4 | 后端端到端跑通 |
| 第 5–6 天 | 阶段 5 | 前端展示 |
| 第 7 天 | 阶段 6 | 评测报告、决定是否默认开启 |

## 6. 风险和应对

| 风险 | 影响 | 应对 |
|---|---|---|
| 模型标错依赖（把有依赖的步骤标成独立） | 子 Agent 缺少前一步的结果，结论不完整 | 提示词给出反例；子 Agent 发现缺信息时写进 `open_questions`，由主 Agent 补做 |
| 子 Agent 口径不一致（时间范围、单位不同） | 汇总后的结论自相矛盾 | 子任务里带上用户的原始问题和澄清答案；汇总提示要求检查口径；评测单独统计冲突 |
| 共用 Agent 实例导致并发串数据 | 图表或变量串到别的步骤 | 阶段 3 先抽出 `RunContext`，再加"两个子 Agent 互不可见"的测试 |
| 限流（429） | 并行反而更慢 | 信号量限制并发；沿用现有重试；评测时观察 429 次数 |
| 多开沙箱进程 | 内存上涨 | 并发上限 3；子 Agent 结束立即关闭会话 |
| 前端改动面大 | 显示错乱 | 事件都带 `step_id`；没有 `step_id` 的事件按旧逻辑处理，保证向后兼容 |
| 收益不明显（多数题本来就短） | 白做 | 阶段 0 先量基线；阶段 1 独立就有收益；最终由评测决定是否默认开启 |

## 7. 简历可用的表述（按 4.7 的实测数字）

> 设计计划驱动的多 Agent 并行分析：主 Agent 生成带依赖关系的分析计划，把互不依赖的步骤交给子 Agent 在有上限的线程池中并行执行（独立 Python 会话、动作预算、超时与失败隔离、可单独停止），再由主 Agent 统一口径汇总。在 8 道多步 / 多角度题 × 3 次的对比中，正确率保持 100%，子 Agent 阶段的并行把这部分耗时从 30 秒压到 12.5 秒；但由于子 Agent 冷启动和汇总开销，端到端耗时只降低 4%（中位数 10%）而 token 增加 76%，因此默认关闭、作为可配置能力提供，并据此定位了下一步优化方向（按步骤重量决定是否分发、复用上下文）。

面试时可以讲的点：先量基线再做优化；发现"并行有效但端到端不划算"后拆解耗时（并行省 17.7 秒 vs 冷启动和汇总多花的时间），据此决定默认关闭。比只报一个"提速 X%"更有说服力。

## 8. 待定问题

1. 子 Agent 用和主 Agent 相同的模型，还是用更便宜的模型？先用相同模型，阶段 6 视成本再试。
2. 子 Agent 的图表是边产出边展示，还是等汇总后一起展示？建议边产出边展示，用户等待时能看到进展。
3. 是否允许"依赖步骤"读取前一步的 Python 变量？第一版不允许，只通过文字发现传递；需要时再考虑把前一步的结果表存成文件共享。
