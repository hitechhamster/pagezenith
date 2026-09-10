"""Offline tests for the bounded Reddit research agent; no network or real keys."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))

from tools.reddit_research.analyzer import MAX_QUERIES, MAX_ROUNDS, MAX_THREADS, RedditResearcher
from tools.reddit_research.models import RedditResearchRequest
from tools.seo_gap.clients.reddit import RedditComment, RedditThread
from tools.seo_gap.config import Settings


class FakeReddit:
    async def collect(self, query, *_args, **_kwargs):
        slug = query.replace(" ", "-")
        return [RedditThread(id=slug, title=query, url=f"https://reddit.com/r/test/comments/{slug}",
                             subreddit="test", score=9, num_comments=2,
                             top_comments=[RedditComment(body="real discussion", score=3)])]


class FakeLLM:
    def __init__(self, supplement=False):
        self.supplement = supplement
        self.calls = 0

    async def complete_json(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"research_type": "购买决策", "queries": [
                {"query": f"initial query {i}", "purpose": "initial"} for i in range(6)
            ]}
        if self.calls == 2 and self.supplement:
            return {"sufficient": False, "evidence_gaps": ["反方样本不足"],
                    "add_queries": [{"query": f"extra query {i}", "purpose": "extra"} for i in range(6)]}
        if self.calls == 2:
            return {"sufficient": True, "evidence_gaps": [], "add_queries": []}
        return {"overview": "Only sample evidence.", "audience": "buyers",
                "themes": [{"name": "Price", "weight": 75, "summary": "discussed"}],
                "questions": ["What costs more?"], "article_ideas": []}


class RedditAgentTests(unittest.TestCase):
    def make_agent(self, supplement=False):
        agent = RedditResearcher(Settings(_env_file=None, use_mocks=True, reddit_enabled=True))
        agent.llm = FakeLLM(supplement)
        agent.reddit = FakeReddit()
        return agent

    def test_question_accepts_natural_language_and_stops_when_evidence_is_sufficient(self):
        result = asyncio.run(self.make_agent().research(RedditResearchRequest(question="Why do buyers hesitate?")))
        self.assertEqual(result.question, "Why do buyers hesitate?")
        self.assertEqual(result.rounds, 1)
        self.assertEqual(result.query_count, 4)
        self.assertEqual(result.thread_count, 4)
        self.assertEqual(len(result.steps), 5)
        self.assertTrue(result.chart.items)

    def test_legacy_keyword_is_supported(self):
        result = asyncio.run(self.make_agent().research(RedditResearchRequest(keyword="legacy keyword")))
        self.assertEqual(result.question, "legacy keyword")

    def test_supplement_is_bounded(self):
        result = asyncio.run(self.make_agent(True).research(RedditResearchRequest(question="test")))
        self.assertEqual(result.rounds, MAX_ROUNDS)
        self.assertLessEqual(result.query_count, MAX_QUERIES)
        self.assertEqual(result.query_count, MAX_QUERIES)
        self.assertLessEqual(result.thread_count, MAX_THREADS)
        self.assertEqual(result.evidence_gaps, ["反方样本不足"])
        self.assertEqual(result.steps[3].status, "limited")

    def test_empty_question_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "问题"):
            asyncio.run(self.make_agent().research(RedditResearchRequest()))

    def test_real_stage_events_are_emitted(self):
        events = []

        async def collect(event):
            events.append(event)

        asyncio.run(self.make_agent().research(RedditResearchRequest(question="test"), on_step=collect))
        done = {(event["key"], event["status"]) for event in events}
        self.assertIn(("understand", "done"), done)
        self.assertIn(("plan", "done"), done)
        self.assertIn(("evidence", "done"), done)
        self.assertIn(("verify", "done"), done)
        self.assertIn(("report", "done"), done)


if __name__ == "__main__":
    unittest.main()
