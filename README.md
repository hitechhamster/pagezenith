# AI SEO 工具站

自带 API Key 的 AI SEO 工具集。当前工具：**内容差距分析**（对比前 10 给出页面类型匹配、
语义意图、逐页差距、LSI 覆盖、GEO 分析、AI 总结与可粘贴的增补段落，流式逐块出结果）。

## 结构

```
api/
  main.py                 # FastAPI 入口：挂载各工具 router + 服务 web/
  requirements.txt
  tools/
    seo_gap/              # 工具①：内容差距分析
      router.py           # /api/seo-gap/*（key 按请求传 + 并发上限）
      report_v2.py        # 四部分报告编排（流式）
      security.py         # SSRF 防护
      config.py models.py clients/ extraction/ scoring/ ...
web/
  index.html              # 首页：工具列表 + API Key 设置
  tools/seo-gap.html      # 工具① 前端（流式渲染）
  shared/app.css keys.js  # 全站样式 + 自带 key 管理（localStorage）
Dockerfile                # Playwright 官方镜像（自带 Chromium）
render.yaml               # Render Blueprint
```

**加新工具** = `api/tools/<新工具>/router.py` 写个 `APIRouter` → `main.py` `include_router` →
`web/tools/<新工具>.html` + 首页加张卡片。互不影响。

## 本地运行

```bash
# 1) 装依赖（含 Chromium）
pip install -r api/requirements.txt
python -m playwright install chromium

# 2) 配 .env（复制 .env.example），本地可填兜底 key 方便自测
#    BROWSER_CHANNEL=chrome 用系统 Chrome，免下载

# 3) 启动（仓库根目录）
python -m uvicorn main:app --app-dir api --port 8000
# 打开 http://localhost:8000 → 右上角填 API Key → 用工具
```

测试：`PYTHONPATH=api python tests/test_semantic_dedup.py`

## 部署到 Render

1. 推到 GitHub（确认 `.env` **没被提交**，`.gitignore` 已排除）。
2. Render → New → **Blueprint**，选这个仓库（读 `render.yaml`）。或手动建 Web Service：
   - Runtime: **Docker**，Plan: **Standard（≥2GB，Chromium 吃内存，free/starter 会 OOM）**
   - Health check: `/api/seo-gap/health`
3. 环境变量（render.yaml 已含）：`USE_MOCKS=false`、`SERP_PROVIDER=serpapi`、
   `BROWSER_CHANNEL=`（空，用自带 Chromium）、`FETCH_MODE=browser`、`MAX_CONCURRENT_RUNS=2`。
4. **不需要在服务器配 OpenRouter/SerpApi key**——用户在前端自带，按请求传，后端用完即弃。
   （如要服务器兜底 key 做演示，在 Render 控制台设为 Secret，别写进仓库。）

## 安全要点（已实现）
- **Key 按请求传**：用户 key 存浏览器 localStorage，请求时带上，服务器不存储、不打日志。
- **SSRF 防护**：禁止抓取私有/内网/云元数据地址（`security.py`）。
- **并发上限**：`MAX_CONCURRENT_RUNS` 限制同时分析数，超出返回 429，防资源/账单失控。

## 数据库
当前无需数据库（工具无状态、key 在浏览器）。要做账号/历史/用量/计费时再接 Supabase。
