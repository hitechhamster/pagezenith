# Reddit discovery query experiment log

All runs below were executed locally. The raw Serper payloads stay in ignored
`data/reddit-discovery/`; this file records the reproducible query decisions.
No run is a market-opportunity conclusion.

| Round | Candidate queries | Window / pages | Completed | What it established | Decision record |
| --- | ---: | --- | ---: | --- | --- |
| v2 | 37 | one trailing 90-day window / 1 page | 37/37 | Generic gap and DIY phrases are mostly noise; replacement-part phrases are promising. | `reddit_discovery_query_review_v2.csv` |
| v3 | 26 | one trailing 90-day window / 1 page | 26/26 | More precise availability and failure wording improves physical-product recall; several causal-DIY variants still fail. | `reddit_discovery_query_review_v3.csv` |
| v4 | 10 | one trailing 90-day window / 1 page | 10/10 | Non-repair paths exist, especially buyer wording around function and fit; broad absence phrases are noise. | `reddit_discovery_query_review_v4.csv` |
| core v1 | 20 retained candidates | recent 30 days + preceding 60 days / 2 pages each | 80/80 | Collects the first verification-sized sample without expanding the query set. | `reddit_discovery_core_query_library_v1.csv` |
| v5 | 24 | one trailing 90-day window / 1 page | 24/24 | Full commercial-intent sentences are too brittle; only 35 result rows returned. | `reddit_discovery_query_review_v5.csv` |
| v6 | 24 | one trailing 90-day window / 1 page | 24/24 | Shorter phrases recover useful forced-bundle, landed-cost, custom-demand and professional-use signals. | `reddit_discovery_query_review_v6.csv` |

## Core-sample run

- Run directory: `data/reddit-discovery/20260914T011404Z/`
- Requests: 20 query candidates × 2 windows × 2 pages = 80
- Raw result rows: 521
- Unique canonical Reddit URLs: 476
- Unique subreddits: 360

The next stage is to normalize the posts, exclude prohibited or unsuitable
categories, then test whether a need repeats in an active subreddit. It must
not turn raw Google/Serper result counts into demand or market-size claims.

The v6 promotion list is deliberately separate from core v1. Four candidates
are ready for a later core-library revision and four remain secondary. This
prevents one promising sample from immediately changing the full-run query set.
