# Selected-query Reddit discovery run — 2026-09-14

This was a local Serper/Google discovery run. It exhausts retrievable pages for
the selected query catalog and date windows; it does not represent all Reddit.
No demand clustering or market conclusions are included here.

## Scope

- Selected queries: 32
- Consumer queries: 31
- Separate B2B queries: 1
- Date windows: current 0–30 days, active 31–90 days, history 91–365 days
- Pagination: continue until an empty page or a page with no unseen URL
- Result: all 96 query/window series stopped on an empty page
- API errors: 0

## Collection totals

| Window | Pages requested | Raw result rows | Unique URLs within window | Subreddits within window |
| --- | ---: | ---: | ---: | ---: |
| Current | 122 | 740 | 674 | 514 |
| Active | 100 | 563 | 514 | 394 |
| History | 252 | 2,121 | 1,837 | 1,069 |
| Total | 474 | 3,424 | 3,012 globally deduplicated | 1,629 globally deduplicated |

## Local evidence

The ignored run directory is:

`data/reddit-discovery/20260914T042928Z-full/`

It contains the query plan, raw response JSONL, normalized URLs, signal-post
scaffold, subreddit candidates, per-series stopping evidence, checkpoints and
run metrics. API key material was not written to any output.

## Next stage boundary

The next stage is not more Serper discovery. It is to validate real Reddit post
dates and community activity, separate posts into boards/topics, apply safety
exclusions, and merge semantically equivalent needs. Until that happens, result
counts must not be described as demand volume or market size.
