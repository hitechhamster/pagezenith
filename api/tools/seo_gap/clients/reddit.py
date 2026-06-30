"""Reddit 内容研究底座（被内容差距 / 独立 Reddit 工具复用）。

数据路径（均实测可用、免费、Render 数据中心 IP 可跑）：
  - 发现：SerpApi 搜 `<keyword> site:reddit.com` → 全站前 N 帖的 URL（含子版+帖子ID）。
  - 帖子元数据：Arctic-Shift `/api/posts/ids?ids=...`（一次拿多帖标题/正文/分数）。
  - 全部评论：Arctic-Shift `/api/comments/search?link_id=...`（扁平全评论，按点赞排序）。

Reddit 官方免费 .json 已对数据中心 IP/headless 全面 403，故改用 Arctic-Shift
（Pushshift 维护中继任归档，新鲜到当天，限流约 1000/分钟，global scope）。
限流时优雅降级：拿不到就少给评论，绝不让整个分析失败。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from .serpapi import SerpApiClient

logger = logging.getLogger(__name__)

# https://www.reddit.com/r/<sub>/comments/<id>/<slug>/
_THREAD_RE = re.compile(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", re.I)

# 关键词 → list[RedditThread] 的进程内 TTL 缓存（内容研究不需实时，削减共享限流压力）。
_CACHE: dict[str, tuple[float, list["RedditThread"]]] = {}


class RedditComment(BaseModel):
    body: str
    score: int = 0


class RedditThread(BaseModel):
    id: str = ""
    title: str = ""
    url: str = ""
    subreddit: str = ""
    score: int = 0
    num_comments: int = 0
    selftext: str = ""
    top_comments: list[RedditComment] = Field(default_factory=list)

    def as_text(self, max_chars: int) -> str:
        """拼成喂给 LLM 的一段文本（标题 + 正文 + 高赞评论），限长。"""
        parts = [f"[r/{self.subreddit}] {self.title}（{self.score}赞/{self.num_comments}评）"]
        if self.selftext.strip():
            parts.append(self.selftext.strip())
        for c in self.top_comments:
            parts.append(f"- ({c.score}赞) {c.body.strip()}")
        return "\n".join(parts)[:max_chars]


class RedditClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.serp = SerpApiClient(self.s)

    # ------------------------------------------------------------------ #
    # 发现：SerpApi site:reddit.com
    # ------------------------------------------------------------------ #
    async def search_threads(self, keyword: str, location_code: int = 2840,
                             language_code: str = "en", limit: int | None = None) -> list[dict]:
        """返回 [{id, subreddit, url, title}]（按 Google 排名，帖子去重）。"""
        limit = limit or self.s.reddit_max_threads
        if self.s.use_mocks:
            slug = keyword.replace(" ", "-").lower()
            return [{"id": f"id{i}", "subreddit": "test",
                     "url": f"https://www.reddit.com/r/test/comments/id{i}/{slug}/",
                     "title": f"{keyword} discussion {i}"} for i in range(1, limit + 1)]
        items = await self.serp.fetch_serp(
            f"{keyword} site:reddit.com", location_code, language_code, depth=20
        )
        seen, out = set(), []
        for it in items:
            m = _THREAD_RE.search(it.url or "")
            if not m:
                continue
            pid = m.group(2).lower()
            if pid in seen:
                continue
            seen.add(pid)
            out.append({"id": pid, "subreddit": m.group(1), "url": it.url, "title": it.title or ""})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ #
    # Arctic-Shift：帖子元数据 + 全部评论
    # ------------------------------------------------------------------ #
    async def _arctic_get(self, client: httpx.AsyncClient, path: str, params: dict) -> list | None:
        """GET Arctic-Shift，返回 data 列表；429/异常返回 None（上层降级处理）。"""
        try:
            r = await client.get(self.s.arctic_base_url + path, params=params)
            if r.status_code == 429:
                logger.warning("Arctic-Shift 限流 429（%s）", path)
                return None
            if r.status_code != 200:
                logger.warning("Arctic-Shift %s → HTTP %s", path, r.status_code)
                return None
            data = r.json().get("data")
            return data if isinstance(data, list) else None
        except Exception as exc:
            logger.warning("Arctic-Shift 请求失败 %s: %s", path, exc)
            return None

    async def _fetch_meta(self, client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict]:
        """一次批量取多帖元数据：id → {title, selftext, score, num_comments, subreddit}。"""
        if not ids:
            return {}
        data = await self._arctic_get(client, "/api/posts/ids", {"ids": ",".join(ids)})
        out = {}
        for p in data or []:
            pid = p.get("id")
            if pid:
                out[pid] = p
        return out

    async def _fetch_comments(self, client: httpx.AsyncClient, pid: str) -> list[RedditComment]:
        """取一帖的全部评论（扁平），过滤删除/低赞/AutoModerator，按点赞取前 N。"""
        data = await self._arctic_get(client, "/api/comments/search",
                                      {"link_id": pid, "limit": 100,
                                       "fields": "body,score,author"})
        out = []
        for c in data or []:
            body = (c.get("body") or "").strip()
            score = int(c.get("score") or 0)
            if (body and body not in ("[deleted]", "[removed]")
                    and score >= self.s.reddit_comment_min_score
                    and c.get("author") != "AutoModerator"):
                out.append(RedditComment(body=body[:1200], score=score))
        out.sort(key=lambda c: c.score, reverse=True)
        return out[: self.s.reddit_max_comments]

    # ------------------------------------------------------------------ #
    # 编排
    # ------------------------------------------------------------------ #
    async def collect(self, keyword: str, location_code: int = 2840,
                      language_code: str = "en", limit: int | None = None) -> list[RedditThread]:
        """搜帖 + 批量元数据 + 全评论，返回 RedditThread 列表（带 TTL 缓存）。"""
        if not self.s.reddit_enabled:
            return []
        ck = f"{keyword.lower()}|{location_code}|{language_code}|{limit or self.s.reddit_max_threads}"
        hit = _CACHE.get(ck)
        if hit and (time.time() - hit[0]) < self.s.reddit_cache_ttl:
            return hit[1]

        refs = await self.search_threads(keyword, location_code, language_code, limit)
        if not refs:
            return []

        if self.s.use_mocks:
            from .reddit import _mock_thread  # 自引用避免循环；mock 路径
            threads = [_mock_thread(r) for r in refs]
            _CACHE[ck] = (time.time(), threads)
            return threads

        headers = {"User-Agent": self.s.reddit_user_agent, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=self.s.reddit_timeout, follow_redirects=True,
                                     headers=headers) as client:
            meta = await self._fetch_meta(client, [r["id"] for r in refs])
            sem = asyncio.Semaphore(self.s.reddit_concurrency)

            async def build(ref) -> RedditThread | None:
                async with sem:
                    comments = await self._fetch_comments(client, ref["id"])
                m = meta.get(ref["id"], {})
                title = m.get("title") or ref["title"]
                if not title and not comments:
                    return None
                return RedditThread(
                    id=ref["id"], title=title, url=ref["url"],
                    subreddit=m.get("subreddit") or ref["subreddit"],
                    score=int(m.get("score") or 0),
                    num_comments=int(m.get("num_comments") or 0),
                    selftext=(m.get("selftext") or "")[:3000],
                    top_comments=comments,
                )

            results = await asyncio.gather(*[build(r) for r in refs])
        threads = [t for t in results if t and (t.selftext or t.top_comments)]
        _CACHE[ck] = (time.time(), threads)
        return threads


def _mock_thread(ref: dict) -> RedditThread:
    return RedditThread(
        id=ref["id"], title=ref.get("title") or "Is this broker a scam?",
        url=ref["url"], subreddit=ref.get("subreddit", "Forex"), score=128, num_comments=44,
        selftext="I deposited and now they delay my withdrawal for 9 days. Anyone else?",
        top_comments=[
            RedditComment(body="Always check the regulator's register before depositing.", score=51),
            RedditComment(body="Unregulated brokers do this all the time, file a chargeback.", score=33),
            RedditComment(body="Which regulator? FCA/ASIC brokers segregate client funds.", score=20),
        ],
    )
