# Reddit physical-product demand-card extraction rules v2

You will receive Reddit evidence records. Each record contains an ASCII `evidence_id`, thread title, subreddit, evidence source, and a real post/comment excerpt. Fill the supplied JSON Schema exactly. Do not rank opportunities and do not invent facts absent from the evidence.

## What counts as a demand signal

1. Analyze the concrete statement in `evidence_text`. Thread title and subreddit are context only and cannot replace evidence. For comment evidence, never copy a product from the thread title when the comment names a different product or does not clearly refer to the title product.
2. Set `is_physical_product_demand=true` when the evidence contains an actionable physical-product pain, desired feature, sourcing requirement, repair requirement, or purchase criterion. This may be unresolved, temporarily worked around, already solved, or advice based on direct experience.
3. A physical-product mention without a pain, requirement, desired physical resolution, or physical-product decision criterion is not a demand signal. Seller promotion, news, jokes, and generic brand praise are not demand by themselves. A software/firmware surprise, legal escalation, or request for information about a physical device is not a physical-product demand unless the evidence also calls for a physical item, repair, redesign, fit, or sourcing solution.
4. Do not reject a signal merely because it is advice to another user. Preserve that distinction in `demand_role` and use `demand_status=historical_or_advice` when no current unresolved need remains.
5. A user's explicit choice based on part availability, repairability, fit, durability, or total cost is a purchase-evaluation signal even before purchase.

## Status, intent, and problem

6. `demand_status` describes the state at the time of the evidence:
   - `active_unresolved`: user still needs a solution.
   - `active_workaround`: a temporary workaround is in use but the underlying need remains.
   - `solved_with_replacement`: user bought or installed an off-the-shelf alternative.
   - `solved_with_custom_fix`: user fabricated, modified, or improvised a solution that appears to work.
   - `purchase_evaluation`: the requirement is actively shaping a purchase choice.
   - `requirement_or_preference`: an explicit product requirement without an active purchase or repair event.
   - `historical_or_advice`: past experience or advice that still reveals a concrete product pain.
   - `no_demand`: no actionable physical-product demand signal.
7. `purchase_intent` must be explicit in the evidence. A broken product, successful DIY repair, or recommendation does not by itself imply intent to buy. Use `strong` for direct language such as "where can I buy/find/order" or a firm decision to buy, `medium` for active comparison or likely purchase, `weak` for tentative consideration, and `none` otherwise.
8. `problem_state` is the primary underlying need, not merely the workaround. If a missing damper forced the user to replace a whole seatpost, use `replacement_unavailable` or `forced_whole_assembly`; do not call the solution a complete assembly if the desired solution is the damper.
9. Use `repair_cost_too_high` for repair/part/service cost approaching replacement-product cost. Use `landed_cost_too_high` only for shipping, duty, tax, or import cost.
10. Use `durability_or_supply_requirement` for explicit commercial-grade durability, reliable replenishment, or long-life requirements. Generic praise such as "will last forever" without a stated requirement remains non-demand.

## Product and solution normalization

11. If the product cannot be determined, use empty strings, `specificity=insufficient`, and `sector_family=unclear`. Never guess.
12. `product_object` is the complete product being used or considered, such as `portable air conditioner`, `FPV goggles`, `e-bike`, or `car headlamp`; never write only the component. Put `battery`, `button pad`, or `damper` in `component`, not in place of the complete product.
13. `component` is the exact affected component. Leave it empty if absent.
14. Classify by the product in the evidence, not subreddit or fabrication method. A 3D-printed air-conditioner adapter belongs to `appliances_cleaning`; a 3D-printer AC board belongs to `3d_printers_maker_tools`; FPV drone equipment belongs to `models_rc_collectibles` unless the evidence clearly concerns video gaming.
15. `solution_form` describes the smallest desired physical solution, not the workaround already used. A missing seatpost damper is `replacement_component` even when the user bought a whole new seatpost.
16. `demand_key` must be a normalized, short English phrase in the form `desired solution + component/product`, suitable for later semantic deduplication. Leave it empty for non-demand.
17. `evidence_quote` must be a verbatim contiguous excerpt from `evidence_text`, at most 300 characters.

## Supply-chain fit and risk

18. Judge `supply_chain_fit` as whether Chinese manufacturing/sourcing could reasonably supply a compliant physical solution, not whether a supplier currently exists. Standard small plastic/metal parts are usually `likely` or `partial`; proprietary firmware, authorization, safety certification, tiny one-off volume, or high-liability parts are `partial`/`unlikely`.
19. For non-demand records, use `supply_chain_fit=unclear` unless the evidence itself makes a clear physical-supply judgment possible.
20. Flag vehicle braking/steering/occupant protection, structural load-bearing parts, mains/high-voltage electrical safety, children, food contact, medical use, weapons, proprietary structures/IP, required certification, and hazardous shipping risks. A standalone lithium battery should normally receive `hazardous_shipping` and may require `certification`. Do not flag ordinary eyewear as intimate, or normal lights, dashboards, trim, mounts, and engine-air-intake parts as safety-critical without explicit evidence of a safety function.
21. `demand_role` identifies whose concrete need is being described. First-person past experience remains `self_need` even when used to advise others; use `advice_to_other` when the evidence only gives advice about another person's need.
22. Fill `market_country` only when the evidence explicitly identifies a country or region.
23. Before answering, verify that each `evidence_quote` is a literal contiguous substring of its record's `evidence_text`, and that every product/component claim is grounded in either `evidence_text` or unambiguous thread context.
24. Output exactly one item per input record and preserve every `evidence_id` byte-for-byte.
