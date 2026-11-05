# 参与贡献

感谢你愿意改进 Alidade。这份文档说明这个仓库**怎么协作**:从提 issue 到合并 PR 的流程、
提交和测试的要求、以及几条容易踩的规则。环境怎么装见 [DEVELOPMENT.md](DEVELOPMENT.md)
和 README 的[快速开始](README.md#快速开始)。

> 用 AI 编程助手(Claude Code、Codex、Cursor…)的话,它们读的是 [AGENTS.md](AGENTS.md),
> 内容和这里一致、更偏向命令和硬规则。

## 协作流程:Issue → 分支 → PR → 审阅 → 合并

1. **先有 issue。** 修 bug 写清复现步骤、期望和实际结果、环境(系统、Python / Node 版本);
   做功能写清要解决什么问题、怎样算做完(验收标准)。小到一行的拼写修正可以直接提 PR。
2. **从最新的 `master` 拉分支**,命名 `<类型>/<issue 号>-<简短描述>`,例如
   `fix/12-sandbox-fork-deadlock`、`docs/10-agent-guides`。不要直接往 `master` 推。
3. **一个 PR 只做一件事。** 不顺手重构、不改无关格式——审阅的人才看得清改了什么。
4. **开 PR**,描述里写:
   - `Closes #<issue 号>`(合并后自动关闭 issue);
   - 改了什么、为什么这么改;
   - **怎么验证的**:跑了哪些命令、结果如何,界面改动附截图;
   - 有没有留下的取舍或风险。
5. **CI 全绿后**由维护者审阅;有修改意见就在同一分支上追加提交。合并后本地 `git pull` 即可。

## 提交信息

用 [Conventional Commits](https://www.conventionalcommits.org/),英文、祈使句:

```
type(scope): summary

可选的正文:为什么改、怎么改、量到的结果。
```

- `type`:`feat` 新功能、`fix` 修 bug、`docs` 文档、`test` 测试、`ci` 流水线、
  `refactor` 重构、`perf` 性能、`chore` 杂项
- `scope` 跟着代码走:`agent`、`sandbox`、`ui`、`eval`、`ci`、`mcp`……
- 例:`fix(sandbox): start workers with spawn to avoid fork deadlocks on Linux`

## 测试

- **新功能和 bug 修复都要带测试。** 修 bug 时先写一个能复现问题(修之前会失败)的测试。
- **测试失败时不要改测试去迁就实现**,也不要 skip 掉。先判断是测试错了、实现错了,还是需求变了,
  在 PR 里说明。
- 本地运行:

  ```bash
  uv run python -m pytest tests/backend -q   # 后端
  yarn test                                  # 前端
  uv run ruff check --select E9,F63,F7,F82 py-src eval   # 和 CI 一样的 lint
  ```

- 测试约定(放哪个目录、怎么命名、怎么 mock)见 `.cursor/rules/backend-test-conventions.mdc`
  和 `frontend-test-conventions.mdc`。单元测试不连网、不调真实模型。

## 代码规范

详细规则在 `.cursor/rules/`(对所有人和所有 AI 工具都适用)和 `docs/dev-guides/`。最常用的几条:

| 场景 | 规则 |
|---|---|
| 前端可见文字 | 一律走 i18n,`en` 和 `zh` 两份语言文件都要加 key |
| API 报错 | 业务错误返回 HTTP 200 + `{"status": "error", ...}`,不把原始异常文本返回给前端 |
| 日志 | URL、参数、token 先脱敏再记录 |
| 文件路径 | 用户或模型能控制的路径一律经过 `ConfinedDir` |
| DataFrame 转 JSON | 用 `df_to_safe_records`(日期是 ISO 字符串) |
| 前端依赖 | Yarn 4(`corepack enable`),不要用 npm / pnpm,不要手改 `yarn.lock` |
| 跨平台 | 代码要在 Windows 和 Linux(CI、Docker)上都能跑;面向用户的命令给出 PowerShell 写法 |

## 评测和数字

`eval/` 用真实模型跑分析题,会花 API 费用,Key 放在 `eval/.env`(已 gitignore,**不要提交、不要贴进
issue 或 PR**)。用法见 [eval/README.md](eval/README.md)。

README、CHANGELOG、文档里的每个数字都要**来自一次真实的运行**,并能指出是哪次(结果目录或命令输出)。
变差或持平的结果也照实写——例如"耗时 −44%,token +7%"。

## 文档

行为、命令或约定变了,在同一个 PR 里更新 README / `docs/`,并在 [CHANGELOG.md](CHANGELOG.md)
的对应小节加一行。

## 安全问题

发现沙箱逃逸、路径穿越、密钥泄露这类问题,**请不要开公开 issue**,通过 GitHub 的
[私密漏洞报告](https://github.com/tkzzzzzz6/alidade/security/advisories/new)联系维护者。

## 许可

提交代码即表示你同意它以本项目的 [MIT 协议](LICENSE)发布。Alidade 基于
[Microsoft Data Formulator](https://github.com/microsoft/data-formulator)(MIT)开发。
