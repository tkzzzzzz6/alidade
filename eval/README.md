# Alidade 评测

用真实模型批量跑一组分析问题，统计成功率、图表质量、反问行为、token 和耗时，并对比改造前后的版本。

| 文件 | 作用 |
|---|---|
| `questions.yaml` | 41 道题，8 类问题（见下表），用到 5 个自带数据集和 `data/` 里的 3 张表 |
| `data/` | 额外的评测数据：脏数据 `messy_sales.csv`、用于多表关联的 `stock_info.csv` 和 `country_continent.csv`；由 `data/make_data.py` 生成（固定随机种子，重跑结果一样） |
| `run_eval.py` | 运行器：每道题一个子进程、一个全新的工作区，直接调用 Agent（不经过浏览器和 Flask） |
| `report.py` | 汇总一次运行，或对比两次运行 |

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

PowerShell 参数：`-Model`、`-ApiBase`、`-Endpoint`、`-Jobs`、`-Repeat`、`-NoJudge`、`-Only`、`-Timeout`、`-Tag`（结果目录后缀）、`-Run`（report 用）、`-EnvFile`（换一个配置文件）。`eval.sh` 只读环境变量和 `.env`（另有 `EVAL_ONLY`、`EVAL_TAG`、`EVAL_ENV_FILE`）。直接运行 `run_eval.py` 时也会自动读取 `eval/.env`。

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

一道题大约 3–5 万 token（系统提示词每次调用都要带上），41 道题一轮大约 150–250 万 token。

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

## 解读时注意

- **图表规则是改造后才加的**：所以"最终展示图表的错误率"天然对新版本有利。写结论时要说明：这是"用同一套规则检查两个版本的输出"，并附上几张实际的图作为例子。
- **结论正确率只是关键词匹配**：可能漏判（比如答案写成了别的说法）。可以结合 `--judge` 的分数和 `logs/` 里的原文一起看。
- **样本量不大**：41 道题、每题 2–3 次，差距在几个百分点以内的指标不要下结论。

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
.\eval\eval.ps1 new                 # 当前版本（41 道题）
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
| `dataset` | 一个数据集，或一个列表（多表关联题） |

`datasets` 里除了自带的 `zip` + `member`，也可以写 `file: data/xxx.csv`；加 `raw: true` 时所有列按原样读成文本（空字符串和 `N/A` 不会被自动转成缺失值），模拟一份没处理过的导出文件。
