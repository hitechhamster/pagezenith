# PageZenith — AI 跨境营销工具

自带 API Key 的 AI 跨境营销工具集。主推 **SEO 文章生成**：填关键词 → 搜同类内容避开同质化 →
出大纲给你审批（可反复改）→ 按大纲写整篇长文，出 SEO 标题描述、可选 AI 配图，导出 Word；
写完还可以单独跑一次**润色**，把全文改写到「美国 12 年级学生能读懂」（FK 阅读年级 9–12）。
另有内容差距分析、文章质量检测、站点情报侦察、Reddit 选题研究、外链拓客。

## 结构

```
api/
  main.py                 # FastAPI 入口：挂载各工具 router + 服务 web/
  requirements.txt
  tools/
    seo_writer/           # 工具⑥：SEO 文章生成（主推）
      router.py           # /api/seo-writer/*（三步向导 + 润色，全部 SSE 流式）
      providers.py        # LLM: OpenRouter | DeepSeek；搜索: Tavily | Exa；配图: OpenRouter
      workflow.py         # 判字数 → 搜索 → 分类 → 大纲 → 改大纲 → 写文 → SEO 元数据 → 配图 → 润色
      prompts.py          # 全部 prompt（改文风只动这里）
      docx_export.py      # Markdown → Word（标题层级/表格/超链接/嵌图）
      session.py          # 三步之间的进程内会话（带 TTL，不落库）
    seo_gap/              # 工具①：内容差距分析
      router.py           # /api/seo-gap/*（key 按请求传 + 并发上限）
      report_v2.py        # 四部分报告编排（流式）
      security.py         # SSRF 防护
      config.py models.py clients/ extraction/ scoring/ ...   # config.py 是全站共用的 Settings
web/
  index.html              # 首页：工具列表 + API Key 设置
  tools/seo-writer.html   # 工具⑥ 前端（三步向导 + 流式渲染 + 下载 Word）
  tools/seo-gap.html      # 工具① 前端（流式渲染）
  shared/app.css keys.js  # 全站样式 + 自带 key 管理（localStorage）
  shared/md.js            # 极简 Markdown 渲染（全站零外部依赖，不引 CDN）
Dockerfile                # Playwright 官方镜像（自带 Chromium）
render.yaml               # Render Blueprint
```

## API Key 与供应商

key 全部由用户在浏览器里填，按请求传给后端，用完即弃。各工具需要的 key：

| Key | 用途 |
|---|---|
| OpenRouter | 全站 LLM；SEO 文章生成的默认写作模型；**AI 配图只有它能做** |
| DeepSeek | SEO 文章生成里可替代 OpenRouter 的写作模型（便宜，但不能出图） |
| SerpApi | 内容差距分析 / Reddit 选题 / 外链拓客的 SERP 数据 |
| Tavily | 竞品正文解析；SEO 文章生成的搜索源之一 |
| Exa | SEO 文章生成的另一个搜索源 |

SEO 文章生成页面顶部可以手动切「写作模型供应商 + 模型」和「搜索源」，没填 key 的选项会自动置灰。

### 润色是独立环节，要手动点

写完文章后页面上会出现「✨ 润色一遍（12 年级可读）」。它是**另一次完整长文调用**，成本和写一篇差不多，
所以不自动跑。润色只改表达不动结构：H 标题的疑问句、黄金答案句的粗体、`[IMAGE:]` 占位符、Markdown
链接全部原样保留。英文文章会用 `textstat` 算 Flesch-Kincaid 阅读年级，在结果里显示润色前后的对比
（目标 9–12；低于 9 会提示"偏浅"，高于 12 提示"偏难"）。不满意可以一键「还原润色前」。

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
4. **不需要在服务器配任何服务商 key**——用户在前端自带，按请求传，后端用完即弃。
   （如要服务器兜底 key 做演示，在 Render 控制台设为 Secret，别写进仓库。）

## 安全要点（已实现）
- **Key 按请求传**：用户 key 存浏览器 localStorage，请求时带上，服务器不存储、不打日志。
- **SSRF 防护**：禁止抓取私有/内网/云元数据地址（`security.py`）。
- **并发上限**：`MAX_CONCURRENT_RUNS` 限制同时分析数，超出返回 429，防资源/账单失控。

## 数据库
当前无需数据库（工具无状态、key 在浏览器）。要做账号/历史/用量/计费时再接 Supabase。

唯一的例外是 SEO 文章生成的三步向导：搜索结果上百 KB，来回传太重，所以第一步的参数/搜索上下文/大纲
存在**进程内**的带 TTL 字典里（`seo_writer/session.py`，默认 2 小时、200 条上限），不落盘、不落库。
进程重启或会话过期时，前端会把大纲和参数回传，降级为"没有搜索上下文"继续出文。
