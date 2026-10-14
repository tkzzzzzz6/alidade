<#
.SYNOPSIS
    Alidade 评测一键脚本。

.DESCRIPTION
    子命令：
      smoke    冒烟测试：只跑 2 道题，确认模型和网络都通
      new      完整评测当前版本（结果：eval/results/new-<Tag>）
      old      完整评测改造前的版本（自动创建 git worktree；结果：eval/results/old-<Tag>）
      compare  对比 old 和 new，生成 eval/results/compare-<Tag>.md
      all      依次执行 new、old、compare
      report   汇总一次运行（默认 new-<Tag>，可用 -Run 指定目录）
      upload   把运行结果上传到 LangSmith 作为 experiment（-Run 指定目录，可多个，逗号分隔）
      help     显示帮助

    加 -LangSmith 时，new / old / smoke 跑完后也会自动上传（需要 LANGSMITH_API_KEY）。

    配置和 API Key 从 eval\.env 读取（模板见 eval\.env.example）；终端里已设置的同名
    环境变量优先。都没有 API Key 时会提示输入（只在本次运行内有效）。

.EXAMPLE
    .\eval\eval.ps1 smoke
.EXAMPLE
    .\eval\eval.ps1 all
.EXAMPLE
    .\eval\eval.ps1 new -Model qwen-plus -ApiBase https://dashscope.aliyuncs.com/compatible-mode/v1
.EXAMPLE
    .\eval\eval.ps1 new -Only trap -Repeat 1 -NoJudge
.EXAMPLE
    .\eval\eval.ps1 upload -Run eval\results\new-sub-off3,eval\results\new-sub-on4
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('smoke', 'new', 'old', 'compare', 'all', 'report', 'upload', 'help')]
    [string]$Command = 'help',

    # 模型配置；不传时依次取：终端环境变量 → eval\.env → 默认值（DeepSeek）
    [string]$Model,
    [string]$ApiBase,
    [string]$Endpoint,

    # 结果目录后缀，默认用模型名，例如 new-deepseek-chat
    [string]$Tag = '',

    [int]$Jobs = 0,      # 0 = EVAL_JOBS 或 3
    [int]$Repeat = 0,    # 0 = EVAL_REPEAT 或 2
    [switch]$NoJudge,
    # 模拟用户：带 intent 的题由一个 LLM 扮演用户回答反问，并按用户真实意图判分
    [switch]$UserSim,
    # 跑完后把结果上传到 LangSmith 作为 experiment（本地结果照常写）
    [switch]$LangSmith,
    # upload 时覆盖同名 experiment
    [switch]$Replace,
    # 模型分工：日常步骤用这个更便宜的模型（同一服务商），-Model 是强模型
    [string]$FastModel = '',
    # 按思考强度分工：关键步骤 / 常规步骤的 reasoning effort（如 high / low），可以和 -FastModel 一起用
    [string]$StrongEffort = '',
    [string]$FastEffort = '',
    # 只跑部分题：题目 id、类别（trap / ambiguous ...）或数据集（movies ...），逗号分隔；
    # -Only a,b,c 和 -Only "a,b,c" 都可以
    [string[]]$Only = @(),
    [int]$Timeout = 0,   # 0 = EVAL_TIMEOUT 或 600

    # 改造前的版本：不传时先找上游提交 5477f0e2，找不到就用仓库的第一个提交（git init 后的 "init"）
    [string]$LegacyCommit = '',
    [string]$LegacyDir = '',

    # report / upload 子命令的结果目录（upload 可以给多个，逗号分隔）
    [string[]]$Run = @(),

    # 配置文件（KEY=VALUE），默认 eval\.env
    [string]$EnvFile = ''
)

# PowerShell 把不加引号的 a,b,c 当成数组：合并回逗号分隔的字符串
$Only = (@($Only) | Where-Object { $_ }) -join ','
$RunDirs = @(@($Run) | ForEach-Object { $_ -split ',' } | Where-Object { $_ })

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

$EvalDir = $PSScriptRoot
$Root = Split-Path $EvalDir -Parent
$Results = Join-Path $EvalDir 'results'
if (-not $EnvFile) { $EnvFile = Join-Path $EvalDir '.env' }
$KeyPlaceholder = 'sk-your-key-here'

# ── 读取 .env：KEY=VALUE，# 开头为注释；终端里已设置的变量不覆盖 ──────────
$DotEnv = @{}
function Import-DotEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#') -or -not $t.Contains('=')) { continue }
        $i = $t.IndexOf('=')
        $key = $t.Substring(0, $i).Trim()
        if ($key.StartsWith('export ')) { $key = $key.Substring(7).Trim() }
        $value = $t.Substring($i + 1).Trim()
        if ($value.Length -ge 2 -and (($value[0] -eq '"' -and $value[-1] -eq '"') -or ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        } else {
            $hash = $value.IndexOf(' #')
            if ($hash -ge 0) { $value = $value.Substring(0, $hash).TrimEnd() }
        }
        $script:DotEnv[$key] = $value
        if (-not [Environment]::GetEnvironmentVariable($key, 'Process') -and $value) {
            [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        }
    }
}
Import-DotEnv $EnvFile

# 取配置：参数 → 环境变量 → .env 里显式写的空值 → 默认值
function Get-Setting([string]$Current, [string]$Name, [string]$Default) {
    if ($Current) { return $Current }
    $fromEnv = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($fromEnv) { return $fromEnv }
    if ($script:DotEnv.ContainsKey($Name)) { return $script:DotEnv[$Name] }
    return $Default
}
$Model = Get-Setting $Model 'EVAL_MODEL' 'deepseek-chat'
$ApiBase = Get-Setting $ApiBase 'EVAL_API_BASE' 'https://api.deepseek.com'
$Endpoint = Get-Setting $Endpoint 'EVAL_ENDPOINT' 'openai'
if ($Jobs -le 0) { $Jobs = [int](Get-Setting '' 'EVAL_JOBS' '3') }
if ($Repeat -le 0) { $Repeat = [int](Get-Setting '' 'EVAL_REPEAT' '2') }
if ($Timeout -le 0) { $Timeout = [int](Get-Setting '' 'EVAL_TIMEOUT' '600') }
if (-not $NoJudge -and (Get-Setting '' 'EVAL_NO_JUDGE' '') -in @('1', 'true', 'yes')) { $NoJudge = [switch]$true }
if (-not $UserSim -and (Get-Setting '' 'EVAL_USER_SIM' '') -in @('1', 'true', 'yes')) { $UserSim = [switch]$true }
$FastModel = Get-Setting $FastModel 'EVAL_FAST_MODEL' ''
$StrongEffort = Get-Setting $StrongEffort 'EVAL_STRONG_EFFORT' ''
$FastEffort = Get-Setting $FastEffort 'EVAL_FAST_EFFORT' ''
if (-not $LegacyDir) { $LegacyDir = Join-Path (Split-Path $Root -Parent) 'alidade-legacy' }
if (-not $Tag) { $Tag = ($Model -replace '[^A-Za-z0-9._-]', '_') }

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { $Python = 'python' }

$NewDir = Join-Path $Results "new-$Tag"
$OldDir = Join-Path $Results "old-$Tag"
$CompareFile = Join-Path $Results "compare-$Tag.md"

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Show-Help {
    Get-Help $PSCommandPath -Detailed | Out-String | Write-Host
    $envState = if (Test-Path -LiteralPath $EnvFile) { "已读取 $EnvFile" } else { "未找到 $EnvFile（可从 .env.example 复制）" }
    $keyState = if ($env:EVAL_API_KEY -and $env:EVAL_API_KEY -ne $KeyPlaceholder) { '已设置' } else { '未设置' }
    Write-Host "配置文件：$envState"
    Write-Host "当前配置：endpoint=$Endpoint  model=$Model  api-base=$ApiBase  tag=$Tag  API Key：$keyState"
    Write-Host "结果目录：$Results"
}

function Ensure-ApiKey {
    if ($Endpoint -eq 'ollama') { return }
    if ($env:EVAL_API_KEY -and $env:EVAL_API_KEY -ne $KeyPlaceholder) { return }
    Write-Host "没有找到 API Key。可以在 $EnvFile 里写 EVAL_API_KEY=...（模板：eval\.env.example）。" -ForegroundColor Yellow
    $secure = Read-Host "现在输入 $Model 的 API Key（不会显示，也不会保存）" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $env:EVAL_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $env:EVAL_API_KEY) { throw '没有提供 API Key。' }
}

function Invoke-Python([string[]]$Arguments) {
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令失败（退出码 $LASTEXITCODE）：python $($Arguments -join ' ')" }
}

function Invoke-Eval {
    param([string]$Out, [string]$Package = 'alidade', [string]$Src = '', [string]$OnlyIds = $Only,
          [int]$RepeatCount = $Repeat, [switch]$Judge)
    Ensure-ApiKey
    $argsList = @((Join-Path $EvalDir 'run_eval.py'), '--env-file', $EnvFile,
        '--endpoint', $Endpoint, '--model', $Model,
        '--package', $Package, '--jobs', $Jobs, '--repeat', $RepeatCount,
        '--timeout', $Timeout, '--out', $Out)
    # 空 = 不用自定义地址（如 OpenAI 官方）。Windows PowerShell 5.1 会丢掉空字符串参数，所以空时不传。
    if ($ApiBase) { $argsList += @('--api-base', $ApiBase) }
    if ($Src) { $argsList += @('--src', $Src) }
    if ($OnlyIds) { $argsList += @('--only', $OnlyIds) }
    if ($Judge) { $argsList += '--judge' }
    if ($UserSim) { $argsList += '--user-sim' }
    if ($LangSmith) { $argsList += '--langsmith' }
    if ($FastModel) { $argsList += @('--fast-model', $FastModel) }
    if ($StrongEffort) { $argsList += @('--strong-effort', $StrongEffort) }
    if ($FastEffort) { $argsList += @('--fast-effort', $FastEffort) }
    Invoke-Python $argsList
}

function Resolve-LegacyCommit {
    if ($LegacyCommit) { return $LegacyCommit }
    # 上游 Data Formulator 0.8.0b1（克隆自 GitHub 的仓库里才有）
    $null = git -C $Root rev-parse --verify --quiet '5477f0e2^{commit}'
    if ($LASTEXITCODE -eq 0) { return '5477f0e2' }
    # 否则用仓库最早的提交：从压缩包/复制的代码 git init 时，它就是改造前的版本
    $roots = @(git -C $Root rev-list --max-parents=0 HEAD)
    if ($LASTEXITCODE -ne 0 -or $roots.Count -eq 0) { throw '找不到改造前的提交，请用 -LegacyCommit <提交号> 指定。' }
    return $roots[-1]
}

function Ensure-Legacy {
    if (Test-Path (Join-Path $LegacyDir 'py-src\data_formulator')) { return }
    $commit = Resolve-LegacyCommit
    $desc = (git -C $Root log -1 --format='%h %s (%ad)' --date=short $commit) -join ''
    Write-Step "创建改造前版本的 worktree：$LegacyDir ← $desc"
    git -C $Root worktree add --detach $LegacyDir $commit
    if ($LASTEXITCODE -ne 0) { throw 'git worktree add 失败。' }
    if (-not (Test-Path (Join-Path $LegacyDir 'py-src\data_formulator'))) {
        throw "提交 $commit 里没有 py-src\data_formulator，不是改造前的版本。请删除 $LegacyDir（git worktree remove $LegacyDir）后用 -LegacyCommit 指定正确的提交。"
    }
}

function Run-Smoke {
    Write-Step "冒烟测试（2 道题）：$Model"
    Invoke-Eval -Out (Join-Path $Results "smoke-$Tag") -OnlyIds 'gas-grade-compare,gas-ambiguous' -RepeatCount 1
}

function Run-New {
    Write-Step "评测当前版本 → $NewDir"
    Invoke-Eval -Out $NewDir -Judge:(-not $NoJudge)
}

function Run-Old {
    Ensure-Legacy
    Write-Step "评测改造前的版本 → $OldDir"
    Invoke-Eval -Out $OldDir -Package 'data_formulator' -Src (Join-Path $LegacyDir 'py-src') -Judge:(-not $NoJudge)
}

function Run-Compare {
    foreach ($dir in @($OldDir, $NewDir)) {
        if (-not (Test-Path (Join-Path $dir 'runs.jsonl'))) { throw "找不到 $dir\runs.jsonl，请先运行 old 和 new。" }
    }
    Write-Step "生成对比报告 → $CompareFile"
    Invoke-Python @((Join-Path $EvalDir 'report.py'), $OldDir, $NewDir, '--labels', '改造前', '改造后', '--out', $CompareFile)
}

function Run-Report {
    $dir = if ($RunDirs.Count -gt 0) { $RunDirs[0] } else { $NewDir }
    if (-not (Test-Path $dir)) { throw "找不到结果目录：$dir" }
    Invoke-Python @((Join-Path $EvalDir 'report.py'), $dir)
}

function Run-Upload {
    $dirs = if ($RunDirs.Count -gt 0) { $RunDirs } else { @($NewDir) }
    foreach ($dir in $dirs) {
        if (-not (Test-Path (Join-Path $dir 'runs.jsonl'))) { throw "找不到 $dir\runs.jsonl" }
    }
    Write-Step "上传到 LangSmith：$($dirs -join ', ')"
    $argsList = @((Join-Path $EvalDir 'langsmith_upload.py')) + $dirs
    if ($Replace) { $argsList += '--replace' }
    Invoke-Python $argsList
}

Push-Location $Root
try {
    switch ($Command) {
        'smoke'   { Run-Smoke }
        'new'     { Run-New }
        'old'     { Run-Old }
        'compare' { Run-Compare }
        'all'     { Run-New; Run-Old; Run-Compare }
        'report'  { Run-Report }
        'upload'  { Run-Upload }
        default   { Show-Help }
    }
} finally {
    Pop-Location
}
