<h1 align="center">
  <img src="./public/alidade-icon.svg" alt="" width="40"><br>
  Alidade
</h1>

<p align="center">
  瞄准问题，读懂数据。 · Aim at the question, read the data.
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>&ensp;
  <img src="https://img.shields.io/badge/agent-LangGraph-1F3A5F" alt="LangGraph">&ensp;
  <img src="https://img.shields.io/badge/models-LiteLLM-E8743B" alt="LiteLLM">
</p>

**Alidade（照准仪）**是测量仪器上用来瞄准目标、读取角度的部件。这个项目是一个在本机运行的 AI 数据分析 Agent：你提出问题，它先列出分析计划，再写代码探索数据、生成图表，必要时停下来问你，并在同一次运行里继续。

> Alidade 基于 [Microsoft Data Formulator](https://github.com/microsoft/data-formulator)（MIT 协议）改造。数据连接、数据线程（Data Threads）、图表渲染等基础能力来自上游项目；Agent 框架和工作流程是重写的，具体见下文的[与上游的区别](#与上游的区别)。

## 主要特性

- **先规划再执行**：Agent 先用 `update_plan` 列出分析步骤，界面上实时显示进度清单。
- **图表自检**：图表在展示前要通过一组规则检查（未聚合的折线、类别过多、空数据等）。不通过就退回给模型修正，最多重试 2 次。
- **持久化 Python 会话**：探索代码在同一个会话里执行，变量在步骤之间保留。可视化代码每次重新运行，保证图表可以复现。
- **上下文压缩**：长分析超过 token 预算时，先裁剪旧的输出，再把更早的步骤压缩成摘要。
- **暂停与恢复**：Agent 用 `ask_user` 反问时会附上它的判断和 2–3 个选项，第一个是推荐选项。回答后通过 `run_id` 从 SQLite 检查点恢复，在同一次运行里继续。
- **不打断的输入框**：Agent 工作时，输入框上方一行显示它正在做什么。这时发的消息会进入队列，可以修改或删除，运行结束后按顺序发送。
- **任意模型**：通过 LiteLLM 接入 OpenAI、Anthropic、Azure、Ollama 或任何兼容 OpenAI 协议的服务。

## 快速开始

需要 Python 3.11+、Node.js 18+、[uv](https://docs.astral.sh/uv/)（推荐）和 Yarn 1.x。

**Windows (PowerShell)**

```powershell
# 后端
uv venv
.\.venv\Scripts\activate
uv pip install -e .

# 前端（构建到 py-src/alidade/dist，由后端直接提供）
yarn install
yarn build

# 配置模型：复制模板后填入 API Key
copy .env.template .env

# 启动，然后打开 http://localhost:5567
alidade
```

**macOS / Linux**

```bash
uv venv && source .venv/bin/activate && uv pip install -e .
yarn install && yarn build
cp .env.template .env
alidade
```

开发时可以用 `yarn start` 启动带热更新的前端，再用 `alidade --dev` 启动后端。更多选项见 [DEVELOPMENT.md](DEVELOPMENT.md)。

### 配置

| 配置 | 说明 |
|---|---|
| `OPENAI_API_KEY` / `OPENAI_MODELS` 等 | 服务端预置的模型，见 `.env.template`；也可以在界面里添加自定义模型 |
| `ALIDADE_HOME` | 数据目录，默认 `~/.alidade`（会话、工作区、日志、检查点） |
| `ALIDADE_AGENT_CONTEXT_BUDGET` | 上下文压缩的 token 预算，默认 60000 |
| `SANDBOX` | 代码执行方式：`local`（默认）或 `docker` |

## 架构

```
用户提问 ──► prepare ──► agent ⇄ tools（只读工具：查看数据、执行探索代码、更新计划）
                          │
                          ▼
                         act（visualize / ask_user / write_report …）
                          │
              ┌───────────┼──────────────┐
              ▼           ▼              ▼
            agent     await_user       finish
         （继续下一步） （interrupt 暂停） （输出结论）
```

- **后端**：Flask + LangGraph（`py-src/alidade/analyst/engine/`）。模型调用用 LiteLLM，状态检查点存在 SQLite，流式事件用 NDJSON 推给前端。
- **前端**：React + Redux + MUI，图表基于 Vega-Lite / [Flint](https://microsoft.github.io/flint-chart/)。
- **技能**：Agent 的能力按技能组织（core / report / data-loading），每个技能由 `SKILL.md` 和 `tools.json` 描述。

| 模块 | 作用 |
|---|---|
| `analyst/engine/graph.py` | 状态图节点与路由 |
| `analyst/engine/planning.py` | `update_plan` 工具与计划事件 |
| `analyst/engine/chart_check.py` | 图表自检规则 |
| `analyst/engine/execution.py` | 持久化探索会话与可视化执行 |
| `analyst/engine/compression.py` | 上下文压缩 |
| `analyst/engine/checkpoints.py` | 暂停运行的 SQLite 检查点 |

## 评测

`eval/` 里有一套用真实模型跑的评测：31 道题、5 个数据集，统计成功率、图表错误率、反问行为、token 和耗时，并能和改造前的版本对比。用法见 [eval/README.md](eval/README.md)。

## 测试

```bash
python -m pytest tests/backend      # 后端
yarn test                           # 前端（vitest）
```

## 与上游的区别

相对于 Data Formulator 0.8.0b1（完整列表见 [CHANGELOG.md](CHANGELOG.md)）：

- Agent 循环用 LangGraph 重写，新增规划、图表自检、持久化会话、上下文压缩，暂停与恢复改为按 `run_id` 从检查点恢复。
- 反问附带上下文和推荐选项；Agent 运行时输入框可用，消息可以排队。
- 修复了空工作区中运行 Agent 的问题。
- 删除了桌面打包、Azure Blob 工作区、Kusto 的 Microsoft 登录、Superset 连接器和上游的 CI 与宣传素材。
- 项目更名：包名 `alidade`、命令 `alidade`、环境变量 `ALIDADE_*`、数据目录 `~/.alidade`。

### 从旧版本迁移

- 旧的环境变量名 `DATA_FORMULATOR_*` 和 `DF_*` 仍然有效，新名字优先。
- 第一次启动时，如果只有 `~/.data_formulator`，会自动复制到 `~/.alidade`，旧目录保留作为备份。
- 包名变了，需要重新安装一次：`uv pip install -e .`。

## 致谢与许可

Alidade 基于 [Microsoft Data Formulator](https://github.com/microsoft/data-formulator) 开发，原项目以 MIT 协议发布，版权归 Microsoft Corporation 所有。本项目同样以 MIT 协议发布，见 [LICENSE](LICENSE)。上游的 README 和更新记录保存在 [docs/upstream/](docs/upstream/)。
