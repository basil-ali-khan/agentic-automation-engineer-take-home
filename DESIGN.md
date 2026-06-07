# DESIGN.md

## What I chose to measure, and why

The question is: **does this variant produce output that downstream systems and humans can rely on?** I split checks into three severity tiers so the verdict distinguishes "this will break things" from "this is suboptimal."

### Critical checks (any failure → DO NOT SHIP)

| Check | Rationale |
|-------|-----------|
| Required fields present (`order_id`, `pricing`, `shipping`, `risk_assessment`, `summary`) | A downstream service consuming the enriched order will crash or silently drop data if any of these are missing. |
| `order_id` echoed correctly | A mismatch corrupts the audit trail — the enriched result cannot be traced back to the original order. |
| Pricing sub-fields present and `total ≈ unit_price × quantity` | Catches arithmetic bugs in pricing refactors. The ±2% dynamic jitter from the pricing tool means exact equality is never expected; I allow $0.01 of tolerance, which absorbs the jitter but catches a hardcoded or zeroed total. |
| Risk assessment present and well-formed | The risk block drives fraud holds and fulfillment gates. Its absence is silent data loss. |
| Risk service bypass detection | I distinguish "risk_level=unknown with an error key" (legitimate retry exhaustion, ~0.1% chance) from "risk_level=unknown with no error key" (service was never called — a design break). This separation is important: the first is expected baseline behavior, the second means the variant removed the safety net. |

### Warning checks (frequent failures → SHIP WITH CAUTION)

| Check | Rationale |
|-------|-----------|
| Summary length ≤ 300 chars | Baseline summaries are 20–180 chars across all three templates. 300 is generous enough to avoid false positives but catches the kind of bloat where a variant prepends hundreds of characters of filler text. Mobile UIs, CRM fields, and push notifications all have length constraints. |
| Summary–risk consistency | If `risk_level` is "high" but the summary says "low risk" (or vice versa), a human reading the summary gets the wrong signal. This catches a class of bug where structured data is correct but the prose contradicts it. |
| Shipping city preserved | The city in the output must match the input after normalization. Catches address-rewriting bugs that would misroute shipments. |

### Why 10 runs per order (default)

The pipeline has four sources of randomness: dynamic pricing jitter, risk-service transient failures (~10%), template selection, and any probabilistic bugs in variants. The probability of a 30% bug producing zero failures in 20 orders on a single run is 0.7²⁰ ≈ 8% — too high for a reliable verdict. With 10 runs × 20 orders = 200 samples per variant, the miss probability drops below 10⁻¹⁰.

### Three-tier verdict

I use three tiers because the business decision is not always binary:

- **SHIP** — all checks pass, no regressions vs baseline.
- **SHIP WITH CAUTION** — no critical failures, but warnings indicate degraded quality that a product owner should review before deployment.
- **DO NOT SHIP** — critical failures detected; deploying this variant risks data loss, incorrect fraud decisions, or downstream crashes.

## What this eval would NOT catch

1. **Semantic accuracy of the summary beyond risk level.** I check that the summary does not contradict the risk level, but I do not verify that it quotes the correct total, product name, or quantity. Catching that would require extracting values from the summary and cross-referencing them — or an LLM judge.

2. **Correctness of the risk score itself.** I verify the score exists and is in [0, 1], not that it is the right value for a given customer and order. There is no ground-truth dataset to compare against.

3. **Pricing currency regression.** I check the total arithmetic but not whether the currency changed from USD to something unexpected.

4. **Concurrency and ordering effects.** The eval runs single-threaded. A variant that is safe in isolation might behave differently under concurrent load.

5. **Adversarial inputs.** The fixture orders are benign. A variant that is safe on clean data might fail on orders with empty fields, unicode in addresses, or extremely large quantities.

## Rough cost and runtime per run

| Item | Value |
|------|-------|
| Fixture orders | 20 |
| Runs per variant | 10 (configurable via `--runs`) |
| Total executions | 4 × 200 = 800 |
| Wall time | ~30–40 seconds |
| LLM API calls | 0 — all checks are deterministic Python |
| Estimated cost | **$0.00** |

Increasing `--runs` to 20 doubles confidence and still completes in under 90 seconds. The `--seed` flag pins all randomness for exact reproducibility across machines.
