# Reddit physical-product demand-card extraction rules v1

You will receive Reddit evidence records. Each record contains an ASCII `evidence_id`, thread title, subreddit, evidence source, and a real post/comment excerpt. Fill the supplied JSON Schema exactly. Do not rank opportunities and do not invent facts absent from the evidence.

Rules:

1. Analyze the concrete statement in `evidence_text`. Thread title and subreddit are context only and cannot replace evidence.
2. A mention of a replacement part is not automatically a demand. Distinguish self need, advice to another user, general discussion, seller marketing, and unrelated mention.
3. Set `is_physical_product_demand=true` only when the evidence identifies a physical product/component plus a problem or desired resolution.
4. If the product cannot be determined, use empty strings, `specificity=insufficient`, and `sector_family=unclear`. Never guess.
5. `evidence_quote` must be a verbatim excerpt from `evidence_text`, at most 300 characters.
6. `product_object` is the complete product being used or owned, such as `portable air conditioner`, `Steam Deck LCD`, or `car headlamp`; never write only `part`.
7. `component` is the exact affected component. Leave it empty if absent.
8. Classify by the product in the evidence, not subreddit or fabrication method. A 3D-printed air-conditioner adapter belongs to `appliances_cleaning`, not `3d_printers_maker_tools`.
9. Use `repair_cost_too_high` for repair/part cost approaching replacement-product cost. Use `landed_cost_too_high` only for shipping, duty, tax, or import cost.
10. `sector_family` is the stable broad category; `sector_suggestion` may add a narrower human-readable category.
11. `solution_form` describes what would resolve the demand. `demand_key` must be a normalized, short English phrase in the form `desired solution + component/product`, suitable for later semantic deduplication.
12. Judge `supply_chain_fit` as whether Chinese manufacturing/sourcing could reasonably supply a compliant solution, not whether a supplier currently exists. Standard small plastic/metal parts are usually `likely` or `partial`; proprietary firmware, authorization, safety certification, tiny one-off volume, or high-liability parts are `partial`/`unlikely`.
13. Flag vehicle braking/steering, load-bearing, electrical safety, children, food contact, medical, weapons, proprietary structure/IP, certification, and hazardous shipping risks.
14. Fill `market_country` only when the evidence explicitly identifies a country or region.
15. Output exactly one item per input record and preserve every `evidence_id` byte-for-byte.
