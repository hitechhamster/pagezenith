"""One real research run and a same-corpus old-format baseline; saves no credentials."""
import argparse
import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
from tools.reddit_research.analyzer import RedditResearcher
from tools.reddit_research.depth import TARGET_REPORT_CHARS, audit_report, narrative_chars
from tools.reddit_research.models import RedditResearch, RedditResearchRequest
from tools.reddit_research.prompts import SYNTHESIS_SYSTEM, synthesis_user
from tools.seo_gap.config import Settings


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-root", type=pathlib.Path, required=True)
    parser.add_argument("--market", default="成人遥控车 RC 的市场")
    parser.add_argument("--concern", default="中国卖家往美国卖还有机会吗")
    parser.add_argument("--reuse-corpus", action="store_true")
    parser.add_argument("--audit-only", action="store_true")
    args = parser.parse_args()
    settings = Settings(_env_file=[args.env_root / ".env", args.env_root / "api/.env"], use_mocks=False)
    agent = RedditResearcher(settings)
    collected = {}
    collector = agent.reddit.collect
    async def collect(*a, **kw):
        batch = await collector(*a, **kw)
        for t in batch:
            collected[t.id or t.url] = t
        return batch
    agent.reddit.collect = collect
    if args.reuse_corpus or args.audit_only:
        from tools.seo_gap.clients.reddit import RedditThread
        corpus_path = ROOT / "work/reddit-depth/corpus.json"
        cached = [RedditThread.model_validate(x) for x in json.loads(corpus_path.read_text(encoding="utf-8"))]
        for t in cached:
            collected[t.id or t.url] = t
        async def cached_collect(*_args, **_kwargs):
            return cached
        agent.reddit.collect = cached_collect
    async def progress(event):
        print(json.dumps(event, ensure_ascii=False), flush=True)
    req = RedditResearchRequest(market=args.market, additional_questions=[args.concern])
    if args.audit_only:
        result = RedditResearch.model_validate_json((ROOT / "work/reddit-depth/report.json").read_text(encoding="utf-8"))
        audit = await audit_report(agent.llm, settings.writer_model or None, result, agent._corpus(cached))
        (ROOT / "work/reddit-depth/audit.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
        result.narrative_chars = narrative_chars(result.model_dump())
    else:
        result = await agent.research(req, on_step=progress)
    output = ROOT / "work/reddit-depth"
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(result.model_dump_json(indent=2), encoding="utf-8")
    threads = [collected[t.id or t.url] for t in collected.values() if t.url in {s.url for s in result.threads}]
    (output / "corpus.json").write_text(json.dumps([t.model_dump() for t in threads], ensure_ascii=False), encoding="utf-8")
    baseline_path = output / "baseline.json"
    if (args.reuse_corpus or args.audit_only) and baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    else:
        baseline = await agent.llm.complete_json(SYNTHESIS_SYSTEM,
            synthesis_user(req.market, req.concerns(), result.research_type, [s.model_dump() for s in result.searches],
                           [], agent._corpus(threads)), model=settings.writer_model or None)
    (output / "baseline.json").write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
    old_chars = narrative_chars(baseline)
    metrics = {"new_narrative_chars": result.narrative_chars, "baseline_narrative_chars": old_chars,
               "ratio": round(result.narrative_chars / max(old_chars, 1), 2),
               "sections": len(result.sections), "findings": sum(len(s.findings) for s in result.sections),
               "cross_checks": len(result.cross_checks), "actions": len(result.action_plan),
               "threads": result.thread_count, "queries": result.query_count, "depth_note": result.depth_note}
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False), flush=True)
    assert result.narrative_chars >= TARGET_REPORT_CHARS, "New report below depth target"
    assert result.narrative_chars >= old_chars * 3, "Less than three times baseline prose"
    assert len(result.sections) == 6 and len(result.cross_checks) >= 3 and len(result.action_plan) >= 5


if __name__ == "__main__":
    asyncio.run(main())
