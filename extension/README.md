# SEO 质量分析 — 浏览器插件（Chrome / Edge）

薄客户端：取当前标签页 URL + 你填的关键词 → 调本地后端 `/analyze` → 渲染报告。
所有 API key 和重活都在后端，插件不持有任何密钥。

## 用法

### 1. 启动后端（持有 key，必须先跑起来）
在项目根目录（`D:\WIKIFX项目\SEO质量插件`）：
```powershell
.\.venv\Scripts\python.exe -m uvicorn seo_analysis.api:app --port 8000
```
看到 `Uvicorn running on http://127.0.0.1:8000` 即就绪。保持这个窗口开着。

### 2. 加载插件（只需一次）
1. Chrome/Edge 打开 `chrome://extensions`（Edge 为 `edge://extensions`）
2. 右上角打开「开发者模式」
3. 点「加载已解压的扩展程序」，选择本 `extension` 文件夹
4. 工具栏出现「SEO 质量分析」图标（可固定到工具栏）

### 3. 分析
1. 打开任意想分析的网页
2. 点插件图标 → 当前页 URL 已自动填入
3. 填关键词（如 `how to avoid forex scams`）、选地区/语言
4. 点「分析此页」→ 等 30–90 秒（真机抓取 + LLM）→ 看报告

## 两种抓取 + 两种模式
- **关键词**：填了=对比前 10 竞品；留空=只评本页（不发现竞品）。
- **目标 URL**：默认当前页，可改成任意网址。仍是当前页时读「已打开的正文」（有登录态、过反爬）；改成别的网址则由后端抓取。
- **本地浏览器抓取**（勾选框）：竞品/非当前页目标用本地 Chrome 打开抓取，能过多数反爬 403（如 Wikipedia、WikiFX）。较慢。需已装 Chrome（用 `BROWSER_CHANNEL=chrome`，免下载额外浏览器）。
  - 极强反爬（Cloudflare「请稍候」挑战页，如 investopedia）可在后端 `.env` 设 `BROWSER_HEADLESS=false` 弹真实窗口，通过率更高。

## 报告看什么
- **顶部评分条**：排名 / 信息增益 / 可读性 / 经验·专业·可信·权威
- **优先行动建议**：按 high/medium/low 配色，这是最该看的可执行清单
- **缺失内容 / 独特内容**：可折叠明细

## 说明
- 权威性（外链）需 DataForSEO 账号验证后才有数据；未验证时显示「—」，其余维度均真实。
- 后端地址默认 `http://localhost:8000`，如改了端口在插件里改「后端」一栏。
- 后端没启动时插件会提示「连不上后端」。
