# Alidade 评测

用真实模型批量跑一组分析问题，统计成功率、图表质量、反问行为、token 和耗时，并对比改造前后的版本。

| 文件 | 作用 |
|---|---|
| `questions.yaml` | 61 道题，13 类问题（见下表），用到 5 个自带数据集和 `data/` 里的 3 张表 |
| `data/` | 额外的评测数据：脏数据 `messy_sales.csv`、用于多表关联的 `stock_info.csv` 和 `country_continent.csv`；由 `data/make_data.py` 生成（固定随机种子，重跑结果一样） |
| `run_eval.py` | 运行器：每道题一个子进程、一个全新的工作区，直接调用 Agent（不经过浏览器和 Flask） |
| `report.py` | 汇总一次运行，或对比两次运行 |
| `number_control.py` | 数字溯源检查的对照实验：误报和查出率（见"数字溯源"） |
| `insights_benchmark.py` | 洞察扫描的基准：在埋了已知规律的合成表上测召回和误报（不调模型） |

## 题目分类

| 类别 | 题数 | 考什么 |
|---|---|---|
| `lookup` 查询 | 11 | 简单的查找、排名 |
| `trend` 趋势 | 3 | 随时间的变化 |
| `multistep` 多步 | 8 | 需要几步计算才能得出结论 |
| `trap` 陷阱 | 4 | 题目里有误导（比如样本很少的类别回报率最高） |
| `ambiguous` 模糊 | 5 | 宽泛的问题（"分析一下…"），允许直接分析，也允许先反问 |
| `cleaning` 数据清洗 | 5 | `messy_sales` 3 道：金额是 `"$1,234.50"` 字符串、有空值和 `N/A`；日期有三种写法；地区大小写和空格不统一；有重复订单。**不清洗就会得到另一个答案**（比如不去重时 West 最高，只解析 ISO 日期时峰值月份不对）。`movies` 2 道：上映日期是 `"Jun 12 1998"` 这样的字符串；导演有大量缺失，还要加"至少 5 部"的过滤 |
| `join` 多表关联 | 2 | 要把两张表关联起来才能回答（股票 + 行业表；能源 + 洲表，且两边列名不同） |
| `clarify` 必须先澄清 | 3 | 问题本身有歧义或涉及投资建议，**必须在出任何图之前先反问**，出了图再问也算失败 |
| `multiangle` 多角度分析 | 4 | 一个问题里有 2 个互不依赖的分析角度（如"收益率谁最高"和"成交量谁最大"），每个角度都要答对，且至少 2 张图。用来衡量并行（多工具调用、子 Agent）能省多少时间 |
| `planned` 三步分析 | 4 | 一个问题里有 3 个要分别出图的分析，至少 3 张图，每个都要答对。用来衡量"计划随出图自动推进"（`ALIDADE_PLAN_AUTO_ADVANCE`）省了多少 LLM 调用和时间 |
| `memory` 口径记忆 | 4 | 先问一个必须澄清的模糊问题，再在同一个用户目录里问一个用到同一个词的追问。追问时应该沿用第一次确定的口径、不再反问，并且答对。要配合 `-UserSim` 跑 |
| `insights` 自动发现洞察 | 3 | 用户明确要"自动扫描、看看有什么值得关注的"：不反问，至少 2 张图，结论要说到数据里真正最突出的规律 |
| `grounding` 数字溯源 | 5 | 诱导模型编数字：问数据里没有的年份、缺失值，问一个网上流传着另一个答案的数，或要求心算。结论里**每个数字都必须找得到出处**，并且答案正确 |

## 一键脚本（推荐）

`eval/` 里有三个脚本，功能相同，按系统选一个：

| 脚本 | 适用 | 用法 |
|---|---|---|
| `eval.ps1` | Windows PowerShell（主用） | `.\eval\eval.ps1 <子命令>` |
| `eval.bat` | Windows cmd 或双击 | `eval\eval.bat <子命令>`（自动绕过 PowerShell 执行策略） |
| `eval.sh` | macOS / Linux / Git Bash | `./eval/eval.sh <子命令>` |

| 子命令 | 作用 | 结果 |
|---|---|---|
| `smoke` | 冒烟测试：只跑 2 道题，确认模型和网络都通 | `eval/results/smoke-<模型>` |
| `new` | 完整评测当前版本（默认每题 2 次，带 LLM 评分） | `eval/results/new-<模型>` |
| `old` | 完整评测改造前的版本（第一次会自动创建 git worktree） | `eval/results/old-<模型>` |
| `compare` | 对比 old 和 new | `eval/results/compare-<模型>.md` |
| `all` | 依次执行 new → old → compare | 以上全部 |
| `report` | 汇总一次运行（默认 new，`-Run <目录>` 指定其他） | 打印到终端 |
| `help` | 显示帮助和当前配置 | |

第一次使用先建配置文件，填上 API Key（`eval/.env` 已被 git 忽略，不会被提交）：

```powershell
Copy-Item eval\.env.example eval\.env      # 然后用编辑器打开 eval\.env，填 EVAL_API_KEY
```

```powershell
.\eval\eval.ps1 help                      # 查看当前读到的配置（不会显示 Key 本身）
.\eval\eval.ps1 smoke
.\eval\eval.ps1 all                       # 完整跑一遍新旧对比

# 换模型：结果会放到以模型名命名的目录，互不覆盖
.\eval\eval.ps1 all -Model qwen-plus -ApiBase https://dashscope.aliyuncs.com/compatible-mode/v1

# 只跑部分题、跑得快一点
.\eval\eval.ps1 new -Only trap -Repeat 1 -NoJudge
```

![1791592689554.png](https://tk-pichost-1325224430.cos.ap-chengdu.myqcloud.com/blog/1791592689554.png)

配置的优先级：**命令行参数 > 终端里设置的环境变量 > `eval/.env` > 默认值（DeepSeek）**。所以平时把常用模型写在 `.env` 里，临时换模型直接加参数就行，不用改文件。`eval/.env` 里可写的项见 `eval/.env.example`：

| 变量 | 说明 |
|---|---|
| `EVAL_API_KEY` | 必填；没有时脚本会提示输入（只在本次运行内有效） |
| `EVAL_MODEL` / `EVAL_API_BASE` / `EVAL_ENDPOINT` | 模型；`EVAL_API_BASE=` 写空表示不用自定义地址（如 OpenAI 官方） |
| `EVAL_JOBS` / `EVAL_REPEAT` / `EVAL_TIMEOUT` / `EVAL_NO_JUDGE` | 运行参数，默认 3 / 2 / 600 / 0 |

PowerShell 参数：`-Model`、`-ApiBase`、`-Endpoint`、`-Jobs`、`-Repeat`、`-NoJudge`、`-Only`、`-Timeout`、`-Tag`（结果目录后缀）、`-Run`（report 用）、`-EnvFile`（换一个配置文件）、`-UserSim`（模拟用户回答反问，见下文）。`eval.sh` 只读环境变量和 `.env`（另有 `EVAL_ONLY`、`EVAL_TAG`、`EVAL_ENV_FILE`、`EVAL_USER_SIM=1`）。直接运行 `run_eval.py` 时也会自动读取 `eval/.env`。

## 1. 准备

在项目根目录、已激活的虚拟环境里运行（PowerShell）。API Key 写在 `eval/.env`（见上一节），`run_eval.py` 会自动读取，不会写进结果文件：

```powershell
.\.venv\Scripts\activate
Copy-Item eval\.env.example eval\.env    # 只需一次，然后填 EVAL_API_KEY
```

任何兼容 OpenAI 协议的服务都用 `--endpoint openai`，再配上 `--api-base` 和 `--model`。常见写法（模型名以各家文档为准）：

| 服务 | `--api-base` | `--model` |
|---|---|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` |
| 通义千问（DashScope 兼容模式） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | （留空） | `gpt-4.1-mini` 等 |
| 本地 Ollama | `--endpoint ollama --model qwen3:32b` | |

## 2. 先跑两道题试试

```powershell
python eval/run_eval.py --endpoint openai --api-base https://api.deepseek.com --model deepseek-chat `
    --only gas-grade-compare,gas-ambiguous --out eval/results/smoke
```

每道题打印一行（✓/✗、耗时、token、图表数，失败时附原因）。跑完会生成：
- `runs.jsonl`：每题一条完整记录
- `report.md`：汇总报告
- `logs/`：每题的完整日志，排查失败时看这里

## 3. 跑完整评测（改造后）

```powershell
python eval/run_eval.py --endpoint openai --api-base https://api.deepseek.com --model deepseek-chat `
    --jobs 3 --repeat 2 --judge --out eval/results/new-deepseek
```

- `--jobs 3`：同时跑 3 道题。遇到限流（429）就调小。
- `--repeat 2`：每题跑 2 次，减少模型随机性带来的波动。写进简历的数字建议至少跑 2–3 次。
- `--judge`：再让同一个模型对照参考答案给每题打 1–5 分（会额外消耗一些 token）。
- `--only`：只跑部分题，可以写题目 id、类别（`trap`、`cleaning`、`clarify` 等）或数据集（`movies`、`messy_sales` 等），逗号分隔。

一道题大约 3–5 万 token（系统提示词每次调用都要带上），61 道题一轮大约 220–360 万 token。

## 4. 跑改造前的版本做对比

改造前的版本用 git worktree 检出到旁边的文件夹，不影响当前代码。用哪个提交：

- 从 GitHub 克隆的仓库：上游 `5477f0e2`（Data Formulator 0.8.0b1，LangGraph 改造之前的最后一个提交）；
- 从压缩包或复制的代码 `git init` 的仓库：没有上游历史，用仓库的**第一个提交**（例如 `init`），它就是改造前的代码。

一键脚本会按这个顺序自动选择，并打印用了哪个提交；也可以用 `-LegacyCommit <提交号>`（bash：`LEGACY_COMMIT=...`）手动指定。手动做的话：

```powershell
git log --oneline --reverse | Select-Object -First 1     # 找到第一个提交，例如 22bc963 init
git worktree add --detach ..\alidade-legacy 22bc963

python eval/run_eval.py --package data_formulator --src ..\alidade-legacy\py-src `
    --endpoint openai --api-base https://api.deepseek.com --model deepseek-chat `
    --jobs 3 --repeat 2 --judge --out eval/results/old-deepseek
```

旧版本用同一个虚拟环境就能跑，不需要额外安装。

## 5. 生成对比报告

```powershell
python eval/report.py eval/results/old-deepseek eval/results/new-deepseek --labels 改造前 改造后 `
    --out eval/results/compare-deepseek.md
```

报告是一张"指标 | 改造前 | 改造后 | 变化"的表，变好标 ✅、变差标 ⚠️。后面附有分类成功率，以及两个版本结果不同的题目。

## 按任务分配模型（`-FastModel`）

`-Model` 是强模型（网页里用户选的那个），`-FastModel` 是同一服务商更便宜的模型（设置 `ALIDADE_FAST_MODEL`）。规则见 `py-src/alidade/analyst/engine/routing.py`：

- 读问题、读用户的回答、上一步失败后：用强模型；
- 其余步骤（看表、写下一张图的代码、子 Agent）：用小模型；
- 小模型写的最终结论如果为空，或有在本次运行里找不到出处的数字，交给强模型重写。

```powershell
.\eval\eval.ps1 new -Model "强模型名" -Only multistep,planned,ambiguous -Tag route-off
.\eval\eval.ps1 new -Model "强模型名" -FastModel "小模型名" -Only multistep,planned,ambiguous -Tag route-on
python eval/report.py eval/results/new-route-off eval/results/new-route-on --labels 只用强模型 按任务分配 --out eval/results/compare-routing.md
```

报告新增"每题平均费用""强模型调用占比""小模型的结论被强模型重写的题数"三行，以及按模型拆开的 token（记录在 `runs.jsonl` 的 `by_model`）。费用按 `EVAL_PRICES` 估算，写在 `eval/.env` 或环境变量里，单位是每百万 token、任意货币（输入 / 命中缓存的输入 / 输出）：

```
EVAL_PRICES=强模型名=4/1/16;小模型名=1/0.25/2
```

"强模型名" / "小模型名" 要换成服务商实际的模型名（引号保留或去掉都可以）（和 `-Model` / `-FastModel` 一致），价格以服务商当前的价目表为准，这里的数字只是格式示例。注意：两个模型交替调用时，各自的前缀缓存是分开的，缓存命中率可能下降，所以要看"未命中缓存的输入 token"和估算费用，不能只看总 token。

### 同一个模型、不同思考强度（`-StrongEffort` / `-FastEffort`）

只用一个模型时，也可以按思考强度分工：关键步骤用高思考，常规步骤用低思考（设置 `ALIDADE_STRONG_EFFORT` / `ALIDADE_FAST_EFFORT`）。例如只用 DeepSeek：

```powershell
# 对照：现在的默认做法
.\eval\eval.ps1 new -Model deepseek-flash -Only multistep,planned,ambiguous -Tag effort-default
# 按思考强度分工：关键步骤 high，常规步骤 low
.\eval\eval.ps1 new -Model deepseek-flash -StrongEffort high -FastEffort low -Only multistep,planned,ambiguous -Tag effort-route
# 全部 low：看分工比"一律低思考"好多少
.\eval\eval.ps1 new -Model deepseek-flash -StrongEffort low -FastEffort low -Only multistep,planned,ambiguous -Tag effort-low
python eval/report.py eval/results/new-effort-default eval/results/new-effort-route --labels 默认 按思考强度分工
python eval/report.py eval/results/new-effort-low eval/results/new-effort-route --labels 全部low 按思考强度分工
```

注意：Agent 平时配置的思考强度（`low`）在 DeepSeek 这类 LiteLLM 不认识的模型上会被 LiteLLM 丢掉，请求里根本没有这个参数，DeepSeek 就用它自己的默认值（思考开、`high`）。所以之前所有用 deepseek-flash 跑的评测，实际都是 `high`，"默认"那组也就是"全部 high"。只有设置了 `-StrongEffort` / `-FastEffort` 时，代码才会明确把思考强度发出去。思考内容算在输出 token 里，所以报告里要看"每次调用的平均输出 token"和估算费用。

## 口径记忆（`memory` 题）

用户回答了"最成功指什么"之后，Agent 用 `save_definition` 把这个词义存到用户目录；之后在同样的表上用到同一个词，会直接沿用并说明。`memory` 题在第一问结束后，用同一个工作区、同一个用户目录再问一个追问，记录追问时是否又反问、花了多少 token、是否答对。

```powershell
# 关 vs 开（ALIDADE_DEFINITIONS=0 时既不保存也不使用）
$env:ALIDADE_DEFINITIONS = '0'; .\eval\eval.ps1 new -Only memory -UserSim -Tag memory-off; Remove-Item Env:ALIDADE_DEFINITIONS
.\eval\eval.ps1 new -Only memory -UserSim -Tag memory-on
python eval/report.py eval/results/new-memory-off eval/results/new-memory-on --labels 不记口径 记住口径 --out eval/results/compare-memory.md
```

看"第二问又反问的比例""第二问的平均 token""第二问答对的比例"。每道题都在自己的临时 `ALIDADE_HOME` 里跑，你本机已保存的口径和规则不会影响评测。

## 自动发现洞察（`insights` 题与基准）

`find_insights` 不调模型，用统计检验找趋势、突变、组间差异、相关、异常值和集中度，所有检验一起做 Benjamini–Hochberg 校正（错误发现率 5%），而且只报告"大到值得一提"的效应。对"每个实体一条时间序列"的表（股票、国家）会改为比较各实体自己的首尾变化，并降低"中国比哈萨克斯坦排放多"这类显而易见结论的排名。

扫描本身的质量用合成数据测（`python eval/insights_benchmark.py --tables 50`）：每张表埋 6 种已知规律，另有 3 个和什么都无关的噪声列；强度 0 表示一张什么规律都没有的表，此时报出的每一条都是误报。

| strength | trend | shift | group_difference | correlation | outliers | concentration | recall | findings / table | false findings | tables with a false finding |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 6.0 | 0 (0%) | 0/50 |
| 0.5 | 100% | 100% | 48% | 100% | 100% | 0% | 75% | 4.5 | 0 (0%) | 0/50 |
| 0.25 | 96% | 70% | 0% | 0% | 100% | 0% | 44% | 2.7 | 0 (0%) | 0/50 |
| 0 | — | — | — | — | — | — | — | 0.0 | 1 (100%) | 1/50 |

强度 1 时 6 种规律全部找到、噪声列零误报；没有任何规律的表里，50 张中有 1 张报了 1 条（2%）。规律变弱时召回下降，这是"效应足够大才报告"的门槛有意为之：宁可漏掉很小的差异，也不把噪声当发现。

端到端（Agent 是否会用它、结论是否说到点子上）跑 `insights` 题：

```powershell
.\eval\eval.ps1 new -Only insights -Tag insights
.\eval\eval.ps1 old -Only insights -Tag insights
python eval/report.py eval/results/old-insights eval/results/new-insights --labels 改造前 改造后 --out eval/results/compare-insights.md
```

## 并行相关的开关和指标

| 环境变量 | 作用 |
|---|---|
| `ALIDADE_PARALLEL_TOOLS` | 默认 `1`：一次回复里可以同时调多个查看类工具（看表结构、跑查询）。设为 `0` 退回每次回复只调一个 |
| `ALIDADE_SUBAGENTS` | 默认 `0`。设为 `1` 时，计划的每一步要标出依赖（`depends_on`）和独立的任务说明（`goal`），互相独立的步骤交给子 Agent 并行完成（见 `docs/plans/parallel-subagents.md`） |
| `ALIDADE_MAX_SUBAGENTS` / `ALIDADE_SUBAGENT_TIMEOUT` | 同时运行的子 Agent 数（默认 3）/ 单个子 Agent 的超时秒数（默认 180） |
| `ALIDADE_PLAN_AUTO_ADVANCE` | 默认 `1`：画出一张图，计划里对应的步骤自动标为完成、下一步标为进行中，模型不用再调 `update_plan` 改状态。设为 `0` 退回旧行为（提示词也换回"自己更新状态"），只用于对比评测 |

开关在 PowerShell 里这样设置（只对当前终端有效），再用 `report.py` 直接对比两个结果目录：

```powershell
# 并行工具调用：关 vs 开
$env:ALIDADE_PARALLEL_TOOLS = '0'; .\eval\eval.ps1 new -Only multiangle,multistep -Tag serial
Remove-Item Env:ALIDADE_PARALLEL_TOOLS; .\eval\eval.ps1 new -Only multiangle,multistep -Tag parallel
python eval/report.py eval/results/new-serial eval/results/new-parallel --labels 串行 并行 --out eval/results/compare-parallel.md

# 计划里能不能识别出可以同时做的步骤
$env:ALIDADE_SUBAGENTS = '1'; .\eval\eval.ps1 new -Only multiangle -Tag plan-deps; Remove-Item Env:ALIDADE_SUBAGENTS

# 子 Agent 关 vs 开（-Jobs 1：一次只跑一道题，耗时才可比）
.\eval\eval.ps1 new -Only multiangle,multistep -Jobs 1 -Tag sub-off
$env:ALIDADE_SUBAGENTS = '1'; .\eval\eval.ps1 new -Only multiangle,multistep -Jobs 1 -Tag sub-on; Remove-Item Env:ALIDADE_SUBAGENTS
python eval/report.py eval/results/new-sub-off eval/results/new-sub-on --labels 单Agent 子Agent --out eval/results/compare-subagents.md

# 计划自动推进：关 vs 开（三步分析题）
$env:ALIDADE_PLAN_AUTO_ADVANCE = '0'; .\eval\eval.ps1 new -Only planned -Jobs 1 -Tag advance-off; Remove-Item Env:ALIDADE_PLAN_AUTO_ADVANCE
.\eval\eval.ps1 new -Only planned -Jobs 1 -Tag advance-on
python eval/report.py eval/results/new-advance-off eval/results/new-advance-on --labels 手动更新 自动推进 --out eval/results/compare-advance.md
```

| 指标 | 计算方式 |
|---|---|
| 多步/多角度题：平均耗时 | `multistep` + `multiangle` 两类题的平均耗时，并行优化主要看这一行 |
| 多角度题：平均 LLM 调用数 | 一次回复调多个工具后应该下降 |
| 平均 update_plan 调用次数 | 计划状态现在由代码随出图自动推进，这个数应该接近"每题最多 1 次"（列计划那一次） |
| 和动作一起被丢弃的工具调用 | 模型把查看类调用和画图放在同一次回复里、被丢掉的次数；计划更新不再被丢弃 |
| 多角度题：计划里有可并行步骤的比例 | 计划中最多有几步可以同时开始（依赖都已完成）；≥2 记为"可并行"。只有开了 `ALIDADE_SUBAGENTS` 才可能大于 0 |
| 使用子 Agent 的题目比例 / 子 Agent 总数 / 失败率 | 开了 `ALIDADE_SUBAGENTS` 才有数据；失败包括出错和超时 |

## 数字溯源（结论里的数字是不是算出来的）

每道题跑完后，评测会用 `py-src/alidade/analyst/engine/number_check.py` 检查最终结论里的每个数字，看它能不能在这次运行里找到出处：

| 出处 | 说明 |
|---|---|
| 计算结果 | Python 打印的输出、`inspect_source_data` 的输出、每张图背后的数据 |
| 简单推算 | 两个打印出来的数之间的差、比值、占比、涨跌幅。只对读起来像比较的数字才算（数字前后有"高/低/差/涨/跌/占/倍/剩/合计"等字样，或单位是 %、个百分点、倍） |
| 原始数据 | 输入表格的单元格，**只认写到 5 位有效数字以上的数**（大表格里短数字很容易碰巧撞上某个单元格）；以及各数值列的最小、最大、均值、中位数、合计和行数 |
| Agent 看到的说明 | 用户问题、反问的回答、数据体检提示里的数字（如"87 条重复"） |

比对只按数字**写出来的精度**（6.4 ≈ 6.432，6.5 不算；"8,660 万"末尾的 0 视为取整），带"突破、超过、不足、以上"等界限词的数字多放宽一位（"突破 0.27" ≈ 0.276），允许百分数和小数互换（73.9% ≈ 0.739）、中文和英文数量单位（2.41 亿 ≈ 241,171,386；"5080 万股" ≈ 打印的 50.84 百万），正负号写在文字里（"下降 41%"）。年份、≤ 10 的整数、日期和月份、贴着字母的编号（PG-13、Q3、s1）不检查。

### 这个检查靠不靠得住：对照实验

`number_control.py` 用保存下来的真实结论做两件事，不调用模型：

1. 原样检查，统计**误报**（真实结论里被判为"找不到出处"的数字）；
2. 把结论里每个数字故意改错（×1.02、×1.05、×1.1、×1.5、×0.7，保持原来的写法精度）再检查，统计**查出率**。

```powershell
python eval/number_control.py eval/results/new-grounding eval/results/old-grounding
```

2026-10-10 的 56 次运行（新旧版本各 28 次，538 个数字）：误报 0 个；改错的数字查出 66%–72%。漏掉的主要原因是改错后的数恰好等于另一个计算结果，或两个打印数之差。所以"找不到出处的比例"是一个**下限**：报出来的基本可信，没报出来的不代表一定没问题。检查规则就是用这批数据调出来的，换一批题目后需要重新跑一次对照。

单题报告末尾会列出所有找不到出处的数字和上下文，供人工抽查。每题的证据（打印输出、图表数据、结论原文）保存在 `logs/<题目>.evidence.json`。改了检查规则后，不用重新调用模型，可以直接重算：

```powershell
python eval/report.py eval/results/new-grounding --recheck-numbers
```

| 指标 | 计算方式 |
|---|---|
| 结论中检查的数字个数 | 所有结论里被检查的数字总数 |
| 结论中找不到出处的数字比例 | 找不到出处的数字 ÷ 检查的数字 |
| 含找不到出处数字的回答比例 | 至少有一个这种数字的回答 ÷ 有检查结果的回答 |

## 指标怎么算

| 指标 | 计算方式 |
|---|---|
| 任务成功率 | 正常结束，且该出图的题出了图，且结论通过答案检查（见"增加题目"），且 `expect_ask` 的题在出图前先反问了 |
| 结论正确率 | 只看带答案检查的题：结论或图表标题是否通过 `mentions_any` / `mentions_all` / `answer_number` |
| 首次出图的错误率 | 改造后：自检第一次拦下的图 ÷ 最终展示的图；改造前：没有自检，直接按同一套规则检查它展示的图 |
| 最终展示图表的错误率 | 用户最终看到的图里，仍有阻断性问题（未聚合、系列过多等）的比例；两个版本用**同一套规则**事后检查 |
| 自检后修正成功率 | 被自检拦下后，模型修好并展示的比例（只有改造后有） |
| token / LLM 调用数 | 包装 `litellm.completion` 统计；服务商不返回用量时会估算，报告里会注明 |
| 输入 token 的缓存命中率 | 服务商返回的"命中前缀缓存"的输入 token ÷ 全部输入 token（DeepSeek 的 `prompt_cache_hit_tokens`、OpenAI 的 `cached_tokens` 等）。服务商不返回时显示"—" |
| 平均未命中缓存的输入 token | 输入 token 减去缓存命中的部分。命中缓存的输入通常便宜得多，所以这一行比"平均输入 token"更接近实际花费 |
| 模糊/需澄清题：用户确认方向前的平均 token | 第一次反问之前花掉的 token；从不反问的题按整次运行算（全程都在猜方向）。这是"方向猜错时白花的成本" |
| 模糊/需澄清题：反问的比例 | `ambiguous` + `clarify` 两类题目里 Agent 反问的比例；"明确问题中反问的比例"越低越好 |
| 模糊/需澄清题：出图前先反问的比例 | 第一次反问之前没有出过任何图的比例（旧版结果里没有这个字段时显示"—"） |
| 必须先澄清的题：做到的比例 | `clarify` 类题目的成功率 |
| 模糊/需澄清题：平均总 token | 这类题平均花了多少 token。先问清楚再做，通常会明显少于"先把能画的都画一遍" |
| 最终展示图表带警告的比例 | 有警告（不阻断）的图，比如折线图超过 10 条线 |
| 反问带 ≥2 个选项的比例 | 反问时是否给了可点击的选项 |
| 上下文压缩 | 触发压缩的题数，以及压缩前后的 token |

Agent 反问时，运行器会自动选第一个（推荐的）选项来回答，每题最多回答 2 次（`--max-answers`）。

## 模拟用户（`--user-sim` / `-UserSim`）

"总选推荐选项"测不出一件事：用户心里想的其实是另一个口径时，先反问到底有没有用。为此 8 道模糊/需澄清题带了一个隐藏的 `intent`（用户真正想问什么，以及对应的标准答案），例如"最成功的电影"其实是想看**回报率**（预算 ≥ 5000 万），答案是 Jurassic Park。

打开 `--user-sim` 后：

- Agent 反问时，由同一个模型扮演用户，只看到 `intent.text` 和 Agent 的问题与选项，按真实意图作答（选最接近的选项，必要时补一句说明）。这次调用**不计入** token 统计；调用失败时退回选第一个选项；
- 判分时额外检查结论是否答到 `intent` 的答案（`intent.mentions_any`）；这些题的"任务成功"改为"正常结束且答到了用户真正想问的"；
- 报告新增三行："模拟用户：按用户真实意图答对的比例"，以及按"反问过 / 没反问"拆开的两行。没反问就只能靠猜，命中意图的概率应该明显更低——两行的差就是"先问清楚"的价值。

```powershell
.\eval\eval.ps1 new -Only ambiguous,clarify -UserSim -Tag usersim
.\eval\eval.ps1 old -Only ambiguous,clarify -UserSim -Tag usersim
python eval/report.py eval/results/old-usersim eval/results/new-usersim --labels 改造前 改造后 --out eval/results/compare-usersim.md
```

注意：模拟用户和被测 Agent 用的是同一个模型，结果有一定"自己配合自己"的偏差；`intent` 的口径是出题时定的，不代表唯一合理的理解。

## 解读时注意

- **图表规则是改造后才加的**：所以"最终展示图表的错误率"天然对新版本有利。写结论时要说明：这是"用同一套规则检查两个版本的输出"，并附上几张实际的图作为例子。
- **结论正确率只是关键词匹配**：可能漏判（比如答案写成了别的说法）。可以结合 `--judge` 的分数和 `logs/` 里的原文一起看。
- **样本量不大**：61 道题、每题 2–3 次，差距在几个百分点以内的指标不要下结论。

## 图表检查规则（折线图线数）

两个版本的最终图表都用同一套规则检查（`py-src/alidade/analyst/engine/chart_check.py`）。折线类图表（折线、面积、Bump、Slope）按颜色 / 线型 / detail 字段数线：

| 线数 | 结果 |
|---|---|
| ≤ 10 | 通过 |
| 11–20 | 警告：建议只保留前几名、合并成"其他"或分面 |
| > 20 | 阻断：自检会要求模型改图（最多重试 2 次） |

## 简历数字怎么得出来

| 想写的结论 | 看报告里哪一行 | 跑哪些题 |
|---|---|---|
| 模糊问题先澄清 | 出图前先反问的比例、必须先澄清的题：做到的比例 | `ambiguous,clarify` |
| 澄清减少了白花的 token | 模糊/需澄清题：**用户确认方向前的平均 token** | `ambiguous,clarify` |
| 数据体检提高了脏数据题的正确率 | 分类成功率里的"数据清洗" | `cleaning` |
| 实际成本 | 输入 token 的缓存命中率、平均未命中缓存的输入 token | 全部 |

注意："模糊/需澄清题：平均总 token"**不会**因为先澄清而下降：评测脚本总是选推荐的选项，Agent 随后照样把分析做完，再加上多了一轮问答。能体现澄清价值的是"确认方向前的 token"——如果不问就猜，猜错时这些 token 就白花了。

```powershell
.\eval\eval.ps1 new                 # 当前版本（全部题目）
.\eval\eval.ps1 old                 # 改造前的版本
.\eval\eval.ps1 compare
# 只看某几类：
.\eval\eval.ps1 new -Only ambiguous,clarify,cleaning -Tag focus
.\eval\eval.ps1 old -Only ambiguous,clarify,cleaning -Tag focus
.\eval\eval.ps1 compare -Tag focus
```

旧的结果文件里没有"确认方向前的 token"和缓存字段，这几行会显示"—"，需要重跑。

## 增加题目

在 `questions.yaml` 里加一项即可，`note` 写参考答案，供 `--judge` 使用。参考值可以先用 pandas 在数据上算出来（`data/make_data.py` 运行时会打印脏数据各题的正确答案和"不清洗时的错误答案"）。答案检查可以组合使用，全部通过才算对：

| 字段 | 含义 |
|---|---|
| `mentions_any` | 列表，出现其中任意一个就算对（中英文都写上；忽略大小写和空格） |
| `mentions_all` | 列表的列表，**每一组**都要命中一个。例如投资回报率题：既要说出类别，又要提到样本量 |
| `answer_number` | `{value: 511.54, tolerance: 0.001}`：结论里有一个数字和它的相对误差在 tolerance 以内（默认 1%） |
| `expect_ask` | `true` 表示必须在出任何图之前先反问；一般配合 `chart: false` |
| `min_charts` | 配合 `chart: true`，至少要出几张图（默认 1） |
| `max_unsupported` | 结论里最多允许几个找不到出处的数字（数字溯源题都是 0） |
| `dataset` | 一个数据集，或一个列表（多表关联题） |

`datasets` 里除了自带的 `zip` + `member`，也可以写 `file: data/xxx.csv`；加 `raw: true` 时所有列按原样读成文本（空字符串和 `N/A` 不会被自动转成缺失值），模拟一份没处理过的导出文件。
