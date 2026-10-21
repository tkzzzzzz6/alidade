# 桌面应用

Alidade 可以用一个原生窗口运行,而不是开浏览器标签页。窗口用的是**系统自带的 WebView**
(Windows 的 WebView2、macOS 的 WKWebKit、Linux 的 WebKitGTK),通过
[pywebview](https://pywebview.flowrl.com/) 驱动——不额外打包一份 Chromium。

后端仍然是同一个 Flask + LangGraph 服务:桌面模式只是把它跑在后台线程里,再开一个指向
`http://localhost:5567` 的窗口。窗口关掉,进程退出。

## 为什么是 pywebview,不是 Electron / Tauri

Alidade 的逻辑在 **Python 后端**。Electron 和 Tauri 都是为"逻辑在 JS 里"的应用设计的,
用它们也得单独打包 Python,等于多背一套工具链(Electron 还要自带 Chromium,Tauri 要 Rust)。
pywebview 是纯 Python,用的和 Tauri 一样的系统 WebView,跟后端的 PyInstaller 打包是**同一条工具链**,
对这个项目最省事。而且产物体积主要来自 `pandas / duckdb / pyarrow / vl-convert`,壳子大小无关紧要。

## 本地运行(不打包)

```bash
# 装上桌面依赖
uv pip install -e ".[desktop]"     # 或: pip install "alidade[desktop]"

# 先构建一次前端(后端从 py-src/alidade/dist 提供页面)
yarn install && yarn build

# 开原生窗口
alidade --desktop
```

没装 pywebview 时 `--desktop` 会给出提示并退出;去掉 `--desktop` 就回到浏览器模式。
`ALIDADE_DESKTOP=1` 等价于加 `--desktop`。

## 打包成可分发的桌面程序

用 [PyInstaller](https://pyinstaller.org/)。打包前**必须先构建前端**,它会被一起打进产物。

```bash
yarn install && yarn build                      # 1) 前端 → py-src/alidade/dist
uv pip install -e ".[desktop]" pyinstaller      # 2) 构建依赖
pyinstaller packaging/alidade.spec --noconfirm  # 3) 打包
```

产物在 `dist/Alidade/`(onedir 形式,启动快、好排查),里面的可执行文件:

- Windows:`dist/Alidade/Alidade.exe`
- macOS / Linux:`dist/Alidade/Alidade`

### spec 里的两个取舍

- **排除了重型云连接器**(Azure / BigQuery / Databricks / Mongo / 各类 SQL 驱动等)。
  它们在数据加载器里是 `try/except` 可选导入,去掉不影响启动,但能把产物从 1GB+ 压到可接受。
  需要某个连接器时,把它从 `packaging/alidade.spec` 的 `excludes` 里删掉再打包。
- **窗口模式(无控制台)**。排查打包问题时,把 spec 里的 `console=False` 改成 `True`,
  就能看到后端 stdout/stderr。

### 常见问题

- **第一次打包报 `ModuleNotFoundError` 或缺数据文件**:重型依赖树的常态。把缺的模块加到 spec 顶部的
  `hiddenimports`,缺的数据文件加到 `datas`,再打一次。不要改 spec 主体。
- **Linux 起不来窗口**:装上 WebKitGTK 运行库——
  `sudo apt-get install libgtk-3-0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0`,
  并 `pip install pygobject`。Linux 的 WebView 后端是三个平台里最挑环境的。
- **Windows 空白窗口**:需要 Edge WebView2 运行时(Win10/11 一般自带;企业精简版可能要
  [单独安装](https://developer.microsoft.com/microsoft-edge/webview2/))。
- **没有代码签名**:产物未签名,首次打开会有"未知开发者"提示,属正常。要消除需各平台的签名证书。

## CI / 跨平台发布

- **`.github/workflows/ci.yml`**:push / PR 触发,跑后端测试、前端 build + vitest、ruff。
- **`.github/workflows/release.yml`**:打 `v*` tag(或手动触发)时,在 Windows / macOS / Linux 三平台
  各打一次,产物作为**草稿 Release**上传。确认无误后在 GitHub 上点发布即可。

  ```bash
  git tag v0.1.0
  git push origin v0.1.0
  ```
