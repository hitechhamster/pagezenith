"""Offline tests for the bounded Reddit research agent; no network or real keys."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))

from tools.reddit_research.analyzer import MAX_QUERIES, MAX_ROUNDS, MAX_THREADS, RedditResearcher
from tools.reddit_research.models import DiscussionTheme, RedditResearchRequest
from tools.reddit_research.depth import audit_report, findings, narrative_chars
from tools.reddit_research.models import RedditResearch, ResearchAction
from tools.reddit_research.prompts import PLAN_SYSTEM, EVALUATE_SYSTEM, SECTION_SYSTEM
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
        self.evaluations = 0
        self.section_calls = 0

    async def complete_json(self, system, user, **_kwargs):
        self.calls += 1
        if system == PLAN_SYSTEM:
            return {"research_type": "购买决策", "queries": [
                {"query": f"initial query {i}", "purpose": "initial"} for i in range(8)
            ]}
        if system == EVALUATE_SYSTEM:
            self.evaluations += 1
            return {"sufficient": not self.supplement,
                    "evidence_gaps": ["反方样本不足"] if self.supplement else [],
                    "add_queries": [{"query": f"extra query {self.evaluations} {i}", "purpose": "extra"} for i in range(8)]}
        if system == SECTION_SYSTEM:
            self.section_calls += 1
            return {"introduction": "Sparse offline corpus.", "findings": [
                {"title": "A retained specialist finding", "observation": "real discussion",
                 "source_ids": ["T001", "T999"], "quotes": ["real discussion", "fabricated discussion"]}],
                "evidence_gaps": ["More evidence needed."]}
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
        self.assertEqual(result.query_count, 8)
        self.assertEqual(result.thread_count, 8)
        self.assertEqual(len(result.steps), 6)
        self.assertTrue(result.chart.items)

    def test_legacy_keyword_is_supported(self):
        result = asyncio.run(self.make_agent().research(RedditResearchRequest(keyword="legacy keyword")))
        self.assertEqual(result.question, "legacy keyword")

    def test_market_and_extra_questions_are_structured(self):
        result = asyncio.run(self.make_agent().research(RedditResearchRequest(
            market="US return policies", additional_questions=["What creates distrust?", "what creates distrust?", ""]
        )))
        self.assertEqual(result.market, "US return policies")
        self.assertEqual(result.additional_questions, ["What creates distrust?"])
        self.assertEqual(result.concern_answers[0].question, "What creates distrust?")

    def test_quotes_must_appear_in_collected_corpus(self):
        thread = RedditThread(id="one", title="title", url="https://reddit.com/one", subreddit="test",
                              selftext="I waited nine days for my refund.")
        themes = [DiscussionTheme(name="Refund", quotes=["I waited nine days for my refund.", "A fabricated quote."])]
        kept, dropped = RedditResearcher._verify_quotes(themes, [thread])
        self.assertEqual((kept, dropped), (1, 1))
        self.assertEqual(themes[0].quotes, ["I waited nine days for my refund."])
        self.assertEqual(themes[0].quote_evidence[0].source_url, "https://reddit.com/one")

    def test_supplement_is_bounded(self):
        result = asyncio.run(self.make_agent(True).research(RedditResearchRequest(question="test")))
        self.assertLessEqual(result.rounds, MAX_ROUNDS)
        self.assertLessEqual(result.query_count, MAX_QUERIES)
        self.assertEqual(result.query_count, MAX_QUERIES)
        self.assertLessEqual(result.thread_count, MAX_THREADS)
        self.assertIn("反方样本不足", result.evidence_gaps)
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
        self.assertIn(("report", "limited"), done)  # Sparse fixtures must not be sold as a complete report.

    def test_specialist_content_survives_short_final_summary_and_quotes_are_verified(self):
        agent = self.make_agent()
        result = asyncio.run(agent.research(RedditResearchRequest(market="test")))
        self.assertEqual(len(result.sections), 6)
        self.assertEqual(agent.llm.section_calls, 12)  # one bounded repair for each sparse draft
        self.assertEqual(result.sections[0].findings[0].source_ids, ["T001"])
        self.assertEqual(result.sections[0].findings[0].quotes, ["real discussion"])
        self.assertEqual(result.quotes_dropped, 6)
        self.assertEqual(result.chart.items[0]["value"], 1)
        self.assertTrue(result.depth_note)

    def test_unknown_sources_cannot_be_presented_as_observations(self):
        entries = findings([{"title": "unsupported", "observation": "made up fact", "source_ids": ["T999"],
                             "quotes": ["fake quote"]}], {"T001"})
        self.assertEqual(entries[0].observation, "")
        self.assertEqual(entries[0].quotes, [])
        self.assertIn("待验证假设", entries[0].caveat)

    def test_length_excludes_duplicate_prose_links_titles_and_quotes(self):
        self.assertEqual(narrative_chars({"overview": "实际发现", "audience": "实际发现", "title": "ignored",
            "quotes": ["ignored"], "source_ids": ["ignored"], "sections": [{"introduction": "另一发现"}]}), 8)

    def test_editorial_audit_can_fix_claims_but_cannot_change_sources(self):
        class Editor:
            async def complete_json(self, *_args, **_kwargs):
                return {"corrections": [
                    {"path": "action_plan.0.success_signal", "replacement": "Measure margin after actual return and shipping costs."},
                    {"path": "action_plan.0.source_ids", "replacement": "https://evil.example/invented"},
                    {"path": "action_plan.999.task", "replacement": "An invalid task path must not crash the audit."}]}
        result = RedditResearch(action_plan=[ResearchAction(task="test", success_signal="10%", source_ids=["T001"])])
        asyncio.run(audit_report(Editor(), None, result, "test"))
        self.assertIn("actual return", result.action_plan[0].success_signal)
        self.assertEqual(result.action_plan[0].source_ids, ["T001"])

    def test_incomplete_report_raises_inside_charge_for_refund(self):
        from fastapi import HTTPException
        from tools.reddit_research import router
        outcomes = []
        @asynccontextmanager
        async def charge(*_args):
            try:
                yield object()
            except HTTPException:
                outcomes.append("refund path")
                raise
        with patch.object(router, "_settings_for", return_value=Settings(_env_file=None, use_mocks=True)), \
             patch.object(router, "charge", charge), \
             patch.object(router.RedditResearcher, "research", new=AsyncMock(return_value=RedditResearch(depth_note="too short"))):
            with self.assertRaises(HTTPException):
                asyncio.run(router._research(RedditResearchRequest(market="test"), object()))
        self.assertEqual(outcomes, ["refund path"])

    def test_last_search_round_is_evaluated(self):
        agent = self.make_agent(True)
        asyncio.run(agent.research(RedditResearchRequest(market="test")))
        self.assertEqual(agent.llm.evaluations, 3)

    def test_duplicate_results_and_overlarge_provider_response_respect_thread_cap(self):
        class OverlargeReddit:
            async def collect(self, *_args, **_kwargs):
                return [RedditThread(id=str(i), url=f"https://reddit.com/{i}") for i in range(200)] * 2
        agent = self.make_agent()
        agent.reddit = OverlargeReddit()
        searches = []
        threads = asyncio.run(agent._collect([{"query": "test"}], RedditResearchRequest(), 1, [], searches))
        self.assertEqual(len(threads), MAX_THREADS)
        self.assertEqual(len({t.id for t in threads}), MAX_THREADS)


if __name__ == "__main__":
    unittest.main()
