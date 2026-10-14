# LangSmith 追踪（可选，默认关闭）

打开后，每次分析在 LangSmith 里是一棵树：

```
alidade analyst                      ← 根：输入是问题和用到的表，输出是回答（或反问的问题）
└─ graph                             ← LangGraph 这次运行
   ├─ prepare / agent / tools …      ← 节点
   │  └─ litellm openai/deepseek-flash  ← 节点里的模型调用：消息、工具、回复（正文、思考、工具调用）、token
   ├─ dispatch
   ├─ subagent                       ← 每个并行步骤一个 Send 分支
   │  └─ subagent s1                 ← 子 Agent 自己的图
   │     └─ agent → litellm …
   ├─ subagent
   │  └─ subagent s2 …
   └─ merge → agent → finish
```

- **Threads**：根和图都带 `thread_id = run_id`。一次分析中途反问用户、用户回答后续跑，是两条 trace（第二条的输入里多一个 `reply`，带 `resumed` 标签），在 Threads 标签页里归在同一个线程下。
- **辅助 Agent**：图外面的模型调用（数据加载、推荐问题、图表改样式等）各自是一条单独的 trace，名字是调用它的 Agent，例如 `StarterQuestionsAgent · anthropic/MiniMax-M3`，元数据里有 `agent`。

## 打开

在 `.env` 里：

```
ALIDADE_LANGSMITH=1
LANGSMITH_API_KEY=lsv2_...
# LANGSMITH_PROJECT=alidade        # 默认 alidade
# LANGSMITH_ENDPOINT=...           # 自托管或欧洲区时填写
```

重启后端即可。只设了 `ALIDADE_LANGSMITH=1` 而没有 API Key 时，日志里会有一条警告，追踪保持关闭。

## 它做了什么

- LangGraph 的节点由 LangGraph 自己上报（`ALIDADE_LANGSMITH=1` 时自动设置 `LANGSMITH_TRACING=true`）。
- 模型调用走的是 LiteLLM，不是 LangChain 的模型类，LangGraph 看不到它们。`alidade/langsmith_tracing.py` 在 `Client._dispatch` 里把每次调用记成一个 `llm` 运行，挂在当前节点下面；流式回复在读完（或中途停止读取）时记录，内容从各个 chunk 拼回来，用量取最后一个 chunk 的 `usage`（含 DeepSeek 的缓存命中 token）。图外面的调用（例如数据加载时的辅助 Agent）各自是一棵单独的树。
- 追踪本身出错（比如连不上 LangSmith）只写调试日志，不影响模型调用。

## 用户反馈（👍 / 👎）

打开追踪后，每个回答在说明面板下面有 👍 / 👎：

- 评价作为 feedback 记到这次运行的根 trace 上，key 是 `user_rating`，👍 = 1，👎 = 0；点 👎 后可以写一句"哪里不对"，作为 comment 一起记下。
- 每个用户对每个回答只有一条评价：再点另一个会改掉原来的，再点同一个会撤回。
- 接口 `POST /api/agent/feedback`：`{trace_id, feedback_token, score: 1 | 0 | null, comment?}`。`feedback_token` 是回答里带的，用 LangSmith API Key 做 HMAC，用户只能评价发给自己的回答。
- 没打开追踪时回答里没有 `trace_id`，按钮不显示。

**把差评自动收集成评测集（在 LangSmith 网页上设置一次）：**

1. Tracing 项目 `alidade` → 右上角 **Automations** → 新建规则；
2. 过滤条件：Feedback `user_rating` = 0（或者再加上"有 comment"）；
3. 动作选 **Add to dataset**（例如新建数据集 `alidade-bad-answers`），或者 **Add to annotation queue** 先人工看一遍；
4. 之后被点 👎 的运行会自动进这个数据集，它的输入（问题和表）可以挑出来加进 `eval/` 的回归题。

## 评测结果对比

`eval/` 的评测结果可以作为实验上传到 LangSmith，在网页上并排对比几次运行（例如子 Agent 关 / 开）：`.\eval\eval.ps1 upload -Run eval\results\new-sub-off3,eval\results\new-sub-on4`。说明见 [eval/README.md](../eval/README.md#上传到-langsmith-对比可选)。评测的判分和报告仍以本地为准。

## 注意

- **数据会发到 LangSmith**：trace 里有完整的提示词，包括表的摘要和样例行、用户的问题。数据敏感时只在开发 / 评测环境打开，或者用 LangSmith 自带的 `LANGSMITH_HIDE_INPUTS=true` / `LANGSMITH_HIDE_OUTPUTS=true` 隐藏输入输出。
- 单独设置 `LANGSMITH_TRACING=true`（不设 `ALIDADE_LANGSMITH`）时，只有 LangGraph 节点会上报，没有模型调用。
- Alidade 自己的运行轨迹（`analyst/engine/trace.py`）和评测（`eval/`）不依赖 LangSmith，照常工作。
