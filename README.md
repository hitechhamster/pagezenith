# PageZenith — AI 跨境营销工具

**卡密制**的 AI 跨境营销工具集（2026-08 改造：从 BYO-key 切成服务端统一出 key、按点数计费）。主推 **SEO 文章生成**：填关键词 → 搜同类内容避开同质化 →
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

## 卡密与计费（2026-08 起）

**用户只输卡密，不再自带任何 API Key。** 卡密即身份：余额、消费流水、生成结果都挂在卡上，
换设备输入同一张卡即可取回（`/history`）。

- **服务端三个 key**（`.env`）：`OPENROUTER_API_KEY`（全站 LLM + embeddings + 配图）、
  `SERPER_KEY`（SERP）、`EXA_KEY`（搜索与竞品正文）。Tavily / SerpApi / DeepSeek 已下线。
- **点数制**罩住全部 AI 端点，价目见 `api/billing/pricing.py`（前端从 `/api/billing/pricing` 实时读）。
- **模型选择权在服务端**：用户只选 `basic` / `pro` 档，映射在 `TIERS`。放开自选 = 用户人均 Opus。
- **护栏**：单卡日限 / 全局日成本熔断（读 usage 表真实成本）/ 无效卡密限流 / 生成失败自动退点。
- **造卡**：`python scripts/mint_cards.py --count 20 --credits 60 --label 标准卡`
  → 输出 CSV 传发卡平台；库里只存 sha256，明文卡号只在造卡那一刻出现。

### 断线不丢
干活的是后台 Job（`api/billing/jobs.py`），SSE 只是订阅者。用户关标签页 / 断网，
任务照跑照落库 —— 不会出现"钱扣了、文章没了"。重连用 `/api/billing/job/{id}`，
或直接去「我的记录」取。

### 润色是独立环节，要手动点

写完文章后页面上会出现「✨ 润色一遍（12 年级可读）」。它是**另一次完整长文调用**，成本和写一篇差不多，
所以不自动跑。润色只改表达不动结构：H 标题的疑问句、黄金答案句的粗体、`[IMAGE:]` 占位符、Markdown
链接全部原样保留。英文文章会用 `textstat` 算 Flesch-Kincaid 阅读年级，在结果里显示润色前后的对比
（目标 9–12；低于 9 会提示"偏浅"，高于 12 提示"偏难"）。不满意可以一键「还原润色前」。

**加新工具** = `api/tools/<新工具>/router.py` 写个 `APIRouter` → `main.py` `include_router` →
`web/tools/<新工具>.html` + 首页加张卡片。互不影响。

## 内部 BYOK（不对外展示）

给我们自己写文章用的通道：**填自己的 OpenRouter + Serper key 跑同一条文章流水线，
不扣站内点数**。线上那套点数、熔断、单卡日限保护的是我们的账单，对自带 key 的请求没有意义，
所以这条路把它们整个绕开了 —— 也正因为绕开了，它必须自带门禁。

- **开关**：`.env` 里的 `INTERNAL_PATH` = 一串随便生成的乱码。**留空 = 这条通道不存在**
  （路由压根不注册，API 也一律 404）。
- **入口**：`https://站点/<那串乱码>`。首页和导航都不挂，猜不到就进不去。
- **凭证 = 地址本身**：页面把 URL 里那串乱码原样放进请求头，后端拿它比对。
  所以只藏页面是不够的这件事已经处理了 —— 能白嫖的是 `/api/seo-writer/*`，凭证该带还得带，
  只是不用人手输。收藏一条 URL 就行。
- **用法**：进页面 → 左上角面板选模型供应商、填它的 Key + Serper Key → 保存到本机。
  两把 key 只存这台浏览器的 localStorage，按请求走 header 传，服务端用完即弃、不落库不打日志。
- **两家供应商可选**（搜索永远是 Serper，只有 LLM/出图这头在切）：
  - `openrouter` —— 一把 key 通吃，但要预付费充值
  - `gemini` —— 线上那条线本来就是 Gemini 直连，模型是照着它调优的；香港机器直连 Google
    会被拒（`User location is not supported`），走的是服务端 `OUTBOUND_PROXY` 那条隧道，
    所以 `settings_for()` 里**绝不能**把 `outbound_proxy` 清掉
  两家的 key 和模型表在浏览器里各存各的，来回切不会互相覆盖。
- **换地址**：改 `.env` 重启，旧 URL 当场失效（旧书签点进去会看到「这条地址已失效」）。
  ⚠️ URL 里带秘密的代价：会落进浏览器历史、书签和沿途访问日志。这一页不引任何外部资源
  （字体都在本地），所以不会顺着 Referer 漏给第三方。
- **模型可改**：面板里「模型设置」有五个槽位（outline / article / polish / utility / image），
  默认值从 `/api/seo-writer/byok/defaults` 取（即 `api/byok.py` 的 `DEFAULT_MODELS`，按供应商分两组）。
  gemini 那组就是线上 `billing.pricing.TIERS` 的原值；openrouter 那组是同一批模型在 OpenRouter 上的
  写法（2026-09-08 对着 OpenRouter 公开模型表核对过，五个当时都在）。两家都会下架 / 改名模型，
  哪天某个失效就在页面上改，不用改代码重启。
- **不落库**：不扣点、不进 usage 流水、不进「我的记录」。产物直接在页面上下载 Word。

实现全部收在 `api/byok.py` 一个文件里（门禁 + 配置装配），工具流水线一行没动：
`require_card` 认出 BYOK 身份 → `charge()` 短路不扣点 → `_build()` 换成用户的 key 和模型表。
两个容易踩的坑已经堵上：Serper 要覆盖 `serper_keys`（key 池读的是它，只覆盖 `serper_key` 会被
服务端那几把盖掉）；服务端的 Gemini / DeepSeek / SerpApi key 必须**清空**，否则某个分支没命中
就会静默花公司的钱。`tests/test_byok_flow.py` 盯着这两条。

## 本地运行

```bash
# 1) 装依赖（不再需要 Chromium）
pip install -r api/requirements.txt

# 2) 配 .env（复制 .env.example），本地可填兜底 key 方便自测
#    BROWSER_CHANNEL=chrome 用系统 Chrome，免下载

# 3) 启动（仓库根目录）
python -m uvicorn main:app --app-dir api --port 8000
# 打开 http://localhost:8000 → 右上角填 API Key → 用工具
```

测试（都在 mock 下跑，不花钱）：

```bash
PYTHONPATH=api python tests/test_semantic_dedup.py
cd api && python ../tests/test_billing_flow.py   # 卡密/扣点/退点/断线不丢
cd api && python ../tests/test_byok_flow.py      # 内部 BYOK：门禁 + 配置装配 + 不计费
cd api && python ../tests/test_postfix_language.py  # 补写块跟着文章语言走
```

`test_account_flow.py` / `test_pay_flow.py` 要**先起服务器**再跑
（`python tests/test_account_flow.py http://127.0.0.1:8012`）。

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
SQLite 单文件（`BILLING_DB`，默认 `data/billing.db`），三张表：`cards` / `usage` / `results`。
单机单进程 + 个位数并发，WAL 模式绰绰有余，一个文件就能 cron 备份走。
**部署时务必放持久盘并每日备份** —— 丢了等于所有卡密余额归零。

唯一的例外是 SEO 文章生成的三步向导：搜索结果上百 KB，来回传太重，所以第一步的参数/搜索上下文/大纲
存在**进程内**的带 TTL 字典里（`seo_writer/session.py`，默认 2 小时、200 条上限），不落盘、不落库。
进程重启或会话过期时，前端会把大纲和参数回传，降级为"没有搜索上下文"继续出文。
