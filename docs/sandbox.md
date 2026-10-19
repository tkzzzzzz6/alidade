# 代码执行沙箱

分析 Agent 会生成 Python 代码并运行它来探索数据、计算指标、画图。这些代码由模型写成，不是人审过的，所以在沙箱里执行。本文说明沙箱防住了什么、怎么防的、以及它的边界在哪里。

对应的攻击测试在 `tests/backend/security/test_sandbox_attacks.py`，每一条都是「生成的代码不该做到的一件事」，照攻击者的写法写成。

## 两种沙箱

| | 本地沙箱（`SANDBOX=local`，默认） | Docker 沙箱（`SANDBOX=docker`） |
|---|---|---|
| 隔离方式 | 同机子进程 + Python 审计钩子 + 原生库护栏 | 独立容器 |
| 文件 | 只读工作区，禁止写、禁止读工作区以外 | 工作区只读挂载，输出单独目录 |
| 网络 | 禁止 | `--network none` |
| 资源上限 | 墙钟时间、内存（宿主监控） | `--memory 512m --cpus 1 --pids-limit 256` |
| 其他 | 擦除敏感环境变量 | `--cap-drop ALL --security-opt no-new-privileges` |
| 适合 | 内部 / 本机使用：快（进程常驻，省去 ~600ms 导入），防误用和随手的越界 | 面对不可信输入 / 多租户：内核级隔离 |

**本地沙箱是防误用的护栏，不是对抗性的安全边界。** 它拦住生成代码常见的越界和危险操作，但不假设代码在刻意逃逸——原生扩展、解释器内部仍有更隐蔽的路径碰到文件系统。需要真正的隔离时用 Docker 沙箱。下面主要讲本地沙箱。

## 威胁模型

- **信任的**：Alidade 自身的代码、运行它的机器、Python 标准库和已安装的第三方库。
- **不信任的**：模型为回答用户问题而生成的 Python 代码。它可能因为模型出错或提示词注入而尝试：读取工作区以外的文件、写文件、删文件、起进程、联网外传数据、或占满 CPU / 内存拖垮服务。
- **不在范围内**：真正的对抗性逃逸（针对 CPython 或原生库的漏洞利用）。那种场景用 Docker 沙箱。

## 本地沙箱怎么防

代码在一个常驻的工作子进程里跑（`py-src/alidade/sandbox/local_sandbox.py`），进程启动时就预导入了 pandas / numpy / duckdb / pyarrow。三层防护：

### 1. Python 审计钩子（`sys.addaudithook`）

钩子看得到解释器层面的每个敏感动作，命中就抛异常：

- **写文件**：任何非只读方式的 `open`、`os.open` 带写标志——拒绝。
- **读文件**：执行期间，读工作区以外的路径——拒绝（Python 标准库目录除外，否则 `import` 会被挡）。
- **改文件系统**：`os.remove` / `rmdir` / `rename` / `mkdir` / `chmod` / `symlink` / `truncate` 等——拒绝。
- **列目录**：`os.listdir` / `os.scandir` / `glob` 工作区以外——拒绝（否则能探到机器上有什么）。
- **起进程**：`os.system` / `exec*` / `spawn*` / `posix_spawn` / `fork` / `kill`，以及底层的 `_posixsubprocess.fork_exec`、`_winapi.CreateProcess`——拒绝。
- **网络**：`socket.connect` / `bind` / `sendto` / `sendmsg` / `getaddrinfo`——拒绝。创建 socket 对象本身允许（无害），但任何出站动作和 DNS 都被挡，数据出不去。
- **ctypes / 动态库**：`ctypes.dlopen` / `dlsym`——拒绝（否则可绕过钩子）。
- **导入危险模块**：`subprocess` / `shutil` / `socket` / `urllib` / `requests` / `ctypes` / `multiprocessing` / `signal` / `resource` 等——拒绝。进程启动时还会把可能已被缓存的 `_posixsubprocess`、`subprocess` 从 `sys.modules` 里移除，使其只能重新 `import`，而重新导入被挡。

进程一启动就擦除名字含 `KEY` / `SECRET` / `TOKEN` / `PASSWORD` / `CREDENTIAL` / `CONNECTION_STRING` 的环境变量，生成代码读不到 API Key。

### 2. 原生库护栏（`guards.py`）

审计钩子只看得到走解释器的动作。**DuckDB 和 PyArrow 的文件读写发生在 C++ 里，不触发任何审计事件**，钩子看不见。所以这两个库在工作子进程里被包了一层：

- **DuckDB**：连接以锁定方式打开——关闭外部访问（`enable_external_access=false`）、只允许读代码开始执行时工作区里就存在的文件（`allowed_paths`）、禁止加载扩展、锁死配置（`lock_configuration=true`，之后改不回来）；`COPY ... TO` 和 `EXPORT DATABASE` 被拒。锁定的连接不再自动识别按名字引用的 pandas DataFrame，所以查询里出现的 DataFrame 会先 `register` 到连接上，`duckdb.sql("SELECT ... FROM df")` 照常可用。只允许内存连接。
- **PyArrow**：带路径的读接口（`read_csv` / `read_table` / `dataset` / `ParquetFile` …）检查路径在工作区内；带路径的写接口（`write_table` / `write_csv` / `ParquetWriter` …）一律拒绝。写到内存缓冲（`BufferOutputStream`）仍然可以。

护栏只在模型代码执行期间生效（`guards.begin()` / `guards.end()` 包住 `exec`），库自己导入时不受影响。

### 3. 资源上限（宿主强制）

上限由宿主进程**从外部**盯着工作子进程，不依赖子进程内部自觉——所以子进程即便改掉自己的 RLIMIT 也逃不掉。

- **时间**：`ALIDADE_SANDBOX_TIMEOUT`（默认 120 秒）。死循环、长时间占住 GIL 的原生计算，超时就杀。
- **内存**：`ALIDADE_SANDBOX_MEMORY_MB`（默认 4096）。宿主每 0.1 秒查一次子进程的常驻内存（优先用 `psutil`，Linux 上退回 `/proc`），超限就杀。
- 被杀后：一次性调用直接返回错误；常驻会话（`SandboxSession`）会在一个新的工作进程里继续，并明确告诉模型「会话已重启，之前的变量没了，需要的请重新加载或重算」，于是它会重新读数据而不是在空命名空间上报错。

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `SANDBOX` | `local` | `local` 或 `docker` |
| `ALIDADE_SANDBOX_TIMEOUT` | `120` | 单次执行的墙钟上限（秒） |
| `ALIDADE_SANDBOX_MEMORY_MB` | `4096` | 单个工作进程的内存上限（MB，`0` = 不限） |

内存上限需要 `psutil`（已在依赖里）；装不上时，Linux 用 `/proc` 兜底，其他平台内存上限不生效，日志里会有一条告警，时间上限不受影响。

## 已知边界

- 本地沙箱不防对抗性逃逸：CPython 或某个原生库若有可利用的漏洞，仍可能绕过。不可信输入用 Docker 沙箱。
- 审计钩子对「导入已被缓存的模块」不总是触发（如 `socket`），所以防护的权威层在动作本身（`socket.connect` 等），不在导入。
- 内存上限是采样的（每 0.1 秒一次），极短时间内的尖峰分配可能在被查到之前就已发生。
- Docker 沙箱需要机器上有 Docker；没有时回退到本地沙箱。
