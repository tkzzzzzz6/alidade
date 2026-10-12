# 把 Alidade 接到 Claude Desktop / Cursor（MCP）

Alidade 可以作为一个 MCP 服务器运行。接上后，在 Claude Desktop、Cursor 等支持 MCP 的工具里，可以直接让 Alidade 分析你电脑上的数据文件：它写代码、跑分析、出图（以图片形式返回），图背后的数据和数字核对机制都和网页版一样。

## 提供的工具

| 工具 | 作用 |
|---|---|
| `load_file` | 读入本地 CSV / TSV / Excel / Parquet / JSON 文件，成为一张表 |
| `list_tables` / `describe_table` | 已加载的表；列、类型、样例行和数据体检提示（重复行、数字存成文本等） |
| `ask` | 向分析 Agent 提问，返回结论、图表（PNG）和图背后的数据。需要澄清时，返回问题和 `run_id` |
| `answer` | 回答 Agent 的澄清问题，让它继续 |
| `find_insights` | 不调模型的统计扫描：趋势、突变、组间差异、相关、异常值、集中度，带错误发现率控制 |
| `list_definitions` | 之前确定过的词义（比如“最好”按什么算） |
| `last_run` | 最近一次分析调用了几次模型、用了多少 token 和时间 |

## 配置

服务器从环境变量（或项目根目录的 `.env`）读取模型：

| 变量 | 说明 |
|---|---|
| `ALIDADE_MCP_MODEL` | 模型名，例如 `deepseek-chat`；也可以是网页版里配置的全局模型 id |
| `ALIDADE_MCP_API_KEY` / `ALIDADE_MCP_API_BASE` | 模型的 Key 和地址（OpenAI 兼容接口） |
| `ALIDADE_MCP_ENDPOINT` | 默认 `openai`（OpenAI 兼容），也可以是 `anthropic`、`azure`、`gemini`、`ollama` |
| `ALIDADE_FAST_MODEL` | 可选：日常步骤用的更便宜的模型（见"按任务分配模型"） |
| `ALIDADE_MCP_ROOTS` | 可选：只允许读这些文件夹（用 `;` 分隔，Windows；macOS / Linux 用 `:`） |

## Claude Desktop

编辑 `claude_desktop_config.json`（Windows：`%APPDATA%\Claude\claude_desktop_config.json`；macOS：`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "alidade": {
      "command": "D:\\code\\alidade\\.venv\\Scripts\\python.exe",
      "args": ["-m", "alidade.mcp_server"],
      "env": {
        "ALIDADE_MCP_MODEL": "deepseek-chat",
        "ALIDADE_MCP_API_BASE": "https://api.deepseek.com",
        "ALIDADE_MCP_API_KEY": "sk-...",
        "ALIDADE_MCP_ROOTS": "D:\\data"
      }
    }
  }
}
```

重启 Claude Desktop 后，对话里就能用 Alidade 的工具。例如："用 alidade 加载 D:\data\sales.csv，看看哪个地区销售额最高，画个图"。

## Cursor

在项目的 `.cursor/mcp.json`（或全局设置）里加同样的一段 `mcpServers` 配置。

## 实现说明

- 传输方式是标准的 MCP stdio（JSON-RPC 2.0，每行一条消息），在 `py-src/alidade/mcp_server.py` 里直接实现，不需要额外依赖。运行时 stdout 只输出协议消息，日志和分析代码的打印都转到 stderr。
- 数据放在单独的工作区 `ALIDADE_HOME/mcp-workspaces/`，不和网页版的会话混在一起。保存的口径、运行记录也都按这个身份单独存放。
- 测试：`tests/backend/agents/test_mcp_server.py` 覆盖握手、工具列表、stdio 传输、文件加载和目录限制，以及一次完整的"提问 → 澄清 → 回答 → 出图"。
