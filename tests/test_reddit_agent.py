"""Offline tests for the bounded Reddit research agent; no network or real keys."""
from __future__ import annotations

import asyncio
import pathlib
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))

from tools.reddit_research.analyzer import (MAX_QUERIES, MAX_ROUNDS, MAX_THREADS,
                                            RedditResearcher, _clean_queries)
from tools.reddit_research.models import DiscussionTheme, RedditResearchRequest
from tools.reddit_research.depth import audit_report, findings, narrative_chars
from tools.reddit_research.models import RedditResearch, ResearchAction
from tools.reddit_research.prompts import PLAN_SYSTEM, EVALUATE_SYSTEM, SECTION_SYSTEM
from tools.reddit_research.tables import TABLE_SYSTEM, TABLE_SPECS, build_tables, parse_tables, review_cells
from tools.seo_gap.clients.reddit import (RedditClient, RedditComment, RedditThread,
                                          plain_search_query)
from tools.seo_gap.clients.serper import SerperError
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
        if system == TABLE_SYSTEM:
            return _kwargs["mock"]
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

    def test_llm_queries_are_forced_to_plain_phrases_and_deduplicated(self):
        queries = _clean_queries([
            {"query": 'site:reddit.com "adult RC cars" OR intitle:repair after:2026-01-01 -toy'},
            {"query": "adult RC cars repair"},
            {"query": "inurl:reviews +durable AROUND(3) parts"},
        ], 8)
        self.assertEqual([item["query"] for item in queries], [
            "adult RC cars repair", "reviews durable parts",
        ])
        self.assertNotRegex(" ".join(item["query"] for item in queries),
                            r"(?i)site:|intitle:|inurl:|after:|\bOR\b|AROUND\(")

    def test_reddit_search_retries_provider_pattern_error_with_plain_query(self):
        class PatternThenSuccess:
            def __init__(self):
                self.calls = []

            async def fetch_serp(self, query, *_args, **_kwargs):
                self.calls.append(query)
                if len(self.calls) == 1:
                    raise SerperError(
                        'HTTP 400: {"message":"Query pattern not allowed for free accounts."}'
                    )
                return []

        client = RedditClient(Settings(_env_file=None, use_mocks=False, serper_key="test"))
        client.serp = PatternThenSuccess()
        result = asyncio.run(client.search_threads(
            'site:reddit.com "adult RC cars" OR intitle:repair after:2026-01-01 -toy'
        ))
        self.assertEqual(result, [])
        self.assertEqual(client.serp.calls[0], "adult RC cars repair reddit")
        self.assertEqual(client.serp.calls[1], "adult RC cars repair discussion reddit")

    def test_plain_query_helper_drops_exclusions_instead_of_reversing_them(self):
        self.assertEqual(plain_search_query('best headphones -cheap +repairable'),
                         "best headphones repairable")

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
        self.assertEqual(len(result.market_tables), 4)
        restored = RedditResearch.model_validate_json(result.model_dump_json())
        self.assertEqual(restored.market_tables, result.market_tables)

    def test_market_cells_require_quotes_from_the_named_source(self):
        threads = [RedditThread(id="one", url="https://reddit.com/one", selftext="I drive every Saturday at the park."),
                   RedditThread(id="two", url="https://reddit.com/two", selftext="I am a beginner buying a car.")]
        sample = lambda text, source, quote: {"text": text, "basis": "sample", "source_ids": [source], "quote": quote}
        raw = {"tables": [{"key": "audiences", "rows": [{"cells": {
            "who": sample("入门玩家", "T002", "I am a beginner buying a car."),
            "when": sample("每周六", "T002", "I drive every Saturday at the park."),
            "where": sample("公园", "T001", "I drive every Saturday at the park."),
            "job": {"text": "培养爱好", "basis": "inference", "source_ids": ["T002", "T999"]},
            "barrier": sample("编造事实", "T999", "fabricated quote")}}]}]}
        tables = parse_tables(raw, threads)
        self.assertEqual(len(tables), 4)
        cells = tables[0].rows[0].cells
        self.assertEqual(cells["when"].basis, "unknown")  # Correct quote, wrong source must fail.
        self.assertEqual(cells["where"].quote_evidence[0].source_url, "https://reddit.com/one")
        self.assertEqual(cells["job"].source_ids, ["T002"])
        self.assertEqual(cells["barrier"].source_ids, [])
        self.assertTrue(tables[1].evidence_gap)

    def test_market_time_and_price_cannot_be_guessed_and_columns_are_fixed(self):
        threads = [RedditThread(id="one", selftext="I drive every Saturday at the park.")]
        guessed = {"text": "猜测内容", "basis": "inference", "source_ids": ["T001"]}
        raw = {"tables": [{"key": key, "columns": {"evil": "bad"}, "rows": [{"cells": {
            **{name: guessed for name in columns}, "evil": guessed}}]} for key, _, columns in TABLE_SPECS]}
        tables = parse_tables(raw, threads)
        self.assertEqual(tables[0].rows[0].cells["when"].text, "样本未提及")
        self.assertEqual(tables[2].rows[0].cells["price"].basis, "unknown")
        self.assertNotIn("evil", tables[0].columns)
        self.assertEqual(narrative_chars({"market_tables": [t.model_dump() for t in tables]}), 0)

    def test_market_tables_retry_is_bounded_and_omission_fails(self):
        llm = type("LLM", (), {"complete_json": AsyncMock(return_value={})})()
        with self.assertRaises(ExceptionGroup) as failed:
            asyncio.run(build_tables(llm, None, "test", [], "corpus", []))
        self.assertTrue(all("对照表" in str(e) for e in failed.exception.exceptions))
        self.assertGreaterEqual(llm.complete_json.await_count, 2)
        self.assertLessEqual(llm.complete_json.await_count, 8)

    def test_expanded_price_range_is_rejected_despite_real_quote(self):
        threads = [RedditThread(id="one", selftext="Used Slash cars cost around 100-150.")]
        raw = {"tables": [{"key": "competition", "rows": [{"cells": {
            "option": {"text": "Slash", "basis": "sample", "source_ids": ["T001"], "quote": threads[0].selftext},
            "price": {"text": "$100-700", "basis": "sample", "source_ids": ["T001"], "quote": threads[0].selftext}}}]}]}
        tables = parse_tables(raw, threads)
        self.assertEqual(tables[2].rows[0].cells["price"].basis, "unknown")

    def test_cell_review_preserves_verified_sources_and_removes_unsupported_claims(self):
        threads = [RedditThread(id="one", url="https://reddit.com/one", selftext="I drive every Saturday at the park.")]
        sample = {"text": "公园、后院、赛车场", "basis": "sample", "source_ids": ["T001"], "quote": threads[0].selftext}
        tables = parse_tables({"tables": [{"key": "audiences", "rows": [{"cells": {
            "who": sample, "where": sample, "when": sample}}]}]}, threads)
        llm = type("LLM", (), {"complete_json": AsyncMock(return_value={"cells": [
            {"id": "0.0.who", "text": "自述驾车的发帖人"}, {"id": "0.0.where", "text": "公园"}]})})()
        asyncio.run(review_cells(llm, None, tables))
        cells = tables[0].rows[0].cells
        self.assertEqual(cells["where"].text, "公园")
        self.assertEqual(cells["where"].quote_evidence[0].source_url, "https://reddit.com/one")
        self.assertEqual(cells["when"].basis, "unknown")  # Missing review must not silently pass.

    def test_old_reports_remain_readable_without_invented_tables(self):
        old = RedditResearch.model_validate({"overview": "old report", "report_version": 2})
        self.assertEqual(old.market_tables, [])

    def test_flat_model_rows_are_normalized_without_losing_market_data(self):
        threads = [RedditThread(id="one", selftext="I drive every Saturday at the park.")]
        who = {"text": "自述玩家", "basis": "sample", "source_ids": ["T001"], "quote": threads[0].selftext}
        table = parse_tables({"tables": [{"key": "audiences", "rows": [{"who": who}, None]}]}, threads)[0]
        self.assertEqual(len(table.rows), 1)
        self.assertEqual(table.rows[0].cells["who"].text, "自述玩家")
        self.assertEqual(table.rows[0].cells["when"].basis, "unknown")

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

    def test_provider_pattern_error_becomes_a_refunded_user_facing_error(self):
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

        provider_error = SerperError(
            'HTTP 400: {"message":"Query pattern not allowed for free accounts."}'
        )
        with patch.object(router, "_settings_for", return_value=Settings(_env_file=None, use_mocks=True)), \
             patch.object(router, "charge", charge), \
             patch.object(router.RedditResearcher, "research", new=AsyncMock(side_effect=provider_error)):
            with self.assertRaises(HTTPException) as failed:
                asyncio.run(router._research(RedditResearchRequest(market="test"), object()))
        self.assertEqual(failed.exception.status_code, 502)
        self.assertNotIn("Query pattern", failed.exception.detail)
        self.assertIn("点数已自动退回", failed.exception.detail)
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
