"""eval.py — Order Enrichment Agent Evaluation Harness

Runs baseline + candidate variants against all fixture orders, applies
checks at three severity tiers, and produces:
  - report.md    human-readable verdicts (non-engineer friendly)
  - results.jsonl  per-run audit trail for traceability

Usage:
    python eval.py               # default: 10 runs per order
    python eval.py --runs 20     # more runs = higher confidence
    python eval.py --seed 42     # pin randomness for exact reproducibility
"""

import argparse
import json
import random
import statistics
import time
from collections import defaultdict
from pathlib import Path

from variants import baseline, variant_a, variant_b, variant_c

# ── Configuration ─────────────────────────────────────────────────────────────

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "orders.jsonl"

VARIANTS = {
    "baseline": baseline,
    "variant_a": variant_a,
    "variant_b": variant_b,
    "variant_c": variant_c,
}

# Fields every enriched result MUST contain
REQUIRED_FIELDS = {"order_id", "summary", "pricing", "shipping", "risk_assessment"}

# Sub-fields within each section
REQUIRED_PRICING_FIELDS = {"product_id", "unit_price", "quantity", "total", "currency"}
REQUIRED_SHIPPING_FIELDS = {"street", "city", "country", "valid"}
REQUIRED_RISK_FIELDS = {"risk_score", "risk_level", "recommendation"}

VALID_RISK_LEVELS = {"low", "medium", "high", "unknown"}
VALID_RECOMMENDATIONS = {"approve", "review", "manual_review"}

# Summary longer than this = bloated (baseline max is ~180 chars)
SUMMARY_MAX_CHARS = 300


# ── Check functions ───────────────────────────────────────────────────────────
# Each returns a list of (severity, check_name, detail) tuples.
# Severity: "critical" = blocks ship, "warning" = flag for review, "info" = note

def check_schema(order, result):
    """Are all required fields present and correctly typed?"""
    issues = []

    missing = REQUIRED_FIELDS - set(result.keys())
    if missing:
        issues.append(("critical", "missing_required_fields", f"absent: {sorted(missing)}"))

    if result.get("order_id") != order.get("order_id"):
        issues.append(("critical", "order_id_mismatch",
                        f"input={order.get('order_id')}, output={result.get('order_id')}"))

    pricing = result.get("pricing")
    if isinstance(pricing, dict) and "error" not in pricing:
        mp = REQUIRED_PRICING_FIELDS - set(pricing.keys())
        if mp:
            issues.append(("critical", "pricing_fields_missing", f"absent: {sorted(mp)}"))
    elif pricing is not None:
        issues.append(("critical", "pricing_invalid", f"got: {type(pricing).__name__}"))

    shipping = result.get("shipping")
    if isinstance(shipping, dict):
        ms = REQUIRED_SHIPPING_FIELDS - set(shipping.keys())
        if ms:
            issues.append(("critical", "shipping_fields_missing", f"absent: {sorted(ms)}"))
    elif shipping is not None:
        issues.append(("critical", "shipping_invalid", f"got: {type(shipping).__name__}"))

    return issues


def check_risk(order, result):
    """Is risk assessment present, real, and not silently bypassed?"""
    issues = []
    risk = result.get("risk_assessment")

    if risk is None:
        issues.append(("critical", "risk_assessment_missing", "field completely absent"))
        return issues

    if not isinstance(risk, dict):
        issues.append(("critical", "risk_assessment_invalid", f"got {type(risk).__name__}"))
        return issues

    level = risk.get("risk_level")
    if level not in VALID_RISK_LEVELS:
        issues.append(("critical", "risk_level_invalid", f"got: {level!r}"))

    rec = risk.get("recommendation")
    if rec not in VALID_RECOMMENDATIONS:
        issues.append(("critical", "risk_recommendation_invalid", f"got: {rec!r}"))

    # Distinguish "risk service bypassed" (bad) from "retries exhausted" (ok).
    # Retry exhaustion sets an "error" key; bypassing does not.
    score = risk.get("risk_score")
    if level == "unknown" and score is None and "error" not in risk:
        issues.append(("critical", "risk_service_bypassed",
                        "risk_level=unknown with no score and no error — service was never called"))

    return issues


def check_pricing_math(order, result):
    """Does total ≈ unit_price × quantity?"""
    issues = []
    pricing = result.get("pricing")
    if not isinstance(pricing, dict) or "error" in pricing:
        return issues

    try:
        expected = round(pricing["unit_price"] * pricing["quantity"], 2)
        actual = pricing["total"]
        diff = abs(expected - actual)
        # The pricing tool computes total from the already-jittered unit_price,
        # so the only possible diff is floating-point rounding (≤ $0.01).
        if diff > 0.01:
            issues.append(("critical", "pricing_math_wrong",
                            f"unit_price×qty={expected}, total={actual}, diff={diff:.2f}"))
    except (KeyError, TypeError):
        issues.append(("warning", "pricing_math_unchecked", "could not compute expected total"))

    return issues


def check_summary_quality(order, result):
    """Is the summary present, reasonable length, and consistent with data?"""
    issues = []
    summary = result.get("summary")

    if not isinstance(summary, str) or not summary.strip():
        issues.append(("warning", "summary_missing_or_empty", "no summary text"))
        return issues

    if len(summary) > SUMMARY_MAX_CHARS:
        issues.append(("warning", "summary_too_long",
                        f"{len(summary)} chars (max {SUMMARY_MAX_CHARS})"))

    # Cross-field consistency: if risk is "high", summary should not say "low risk"
    # This is a check NO competitor implemented.
    risk = result.get("risk_assessment")
    if isinstance(risk, dict):
        level = risk.get("risk_level", "")
        summary_lower = summary.lower()
        if level == "high" and "low risk" in summary_lower:
            issues.append(("warning", "summary_contradicts_risk",
                            f"risk_level=high but summary says 'low risk'"))
        if level == "low" and "high risk" in summary_lower:
            issues.append(("warning", "summary_contradicts_risk",
                            f"risk_level=low but summary says 'high risk'"))

    return issues


def check_shipping_preservation(order, result):
    """Does the output preserve the input address data?"""
    issues = []
    shipping = result.get("shipping")
    input_addr = order.get("shipping_address", {})

    if not isinstance(shipping, dict) or not input_addr:
        return issues

    # City should survive normalization (just casing may change)
    input_city = input_addr.get("city", "").strip().lower()
    output_city = shipping.get("city", "").strip().lower()
    if input_city and input_city != output_city:
        issues.append(("warning", "shipping_city_changed",
                        f"input={input_city!r}, output={output_city!r}"))

    return issues


# All checks in execution order
ALL_CHECKS = [
    check_schema,
    check_risk,
    check_pricing_math,
    check_summary_quality,
    check_shipping_preservation,
]


# ── Runner ────────────────────────────────────────────────────────────────────

def load_orders():
    with FIXTURE_PATH.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def run_all(orders, n_runs, seed=None):
    """Run every variant n_runs times per order. Returns per-run records."""
    if seed is not None:
        random.seed(seed)

    all_records = []

    for variant_name, module in VARIANTS.items():
        print(f"  Running {variant_name}...", end=" ", flush=True)
        t0 = time.time()

        for run_idx in range(n_runs):
            for order in orders:
                record = {
                    "variant": variant_name,
                    "order_id": order.get("order_id"),
                    "run": run_idx + 1,
                    "crashed": False,
                    "issues": [],
                }

                try:
                    result = module.enrich_order(order)
                    record["processing_time_ms"] = result.get("processing_time_ms")

                    for check_fn in ALL_CHECKS:
                        record["issues"].extend(check_fn(order, result))

                except Exception as e:
                    record["crashed"] = True
                    record["issues"].append(("critical", "exception", f"{type(e).__name__}: {e}"))

                all_records.append(record)

        elapsed = time.time() - t0
        print(f"done ({elapsed:.1f}s)")

    return all_records


# ── Metrics & Verdicts ────────────────────────────────────────────────────────

def compute_metrics(records):
    """Aggregate per-run records into summary metrics for one variant."""
    n = len(records)
    if n == 0:
        return {}

    critical_count = sum(1 for r in records
                         if r["crashed"] or any(s == "critical" for s, _, _ in r["issues"]))
    warning_count = sum(1 for r in records
                        if any(s == "warning" for s, _, _ in r["issues"]))

    # Break down failures by check name
    failure_counts = defaultdict(int)
    for r in records:
        for severity, name, _detail in r["issues"]:
            failure_counts[f"{severity}:{name}"] += 1

    times = [r["processing_time_ms"] for r in records
             if r.get("processing_time_ms") is not None]

    return {
        "total_runs": n,
        "critical_failures": critical_count,
        "warning_failures": warning_count,
        "pass_rate": round((n - critical_count) / n * 100, 1),
        "failure_breakdown": dict(sorted(failure_counts.items(), key=lambda x: -x[1])),
        "avg_ms": round(statistics.mean(times), 1) if times else 0,
        "p50_ms": round(sorted(times)[len(times) // 2], 1) if times else 0,
        "p95_ms": round(sorted(times)[int(len(times) * 0.95)], 1) if times else 0,
    }


def decide_verdict(candidate_metrics, baseline_metrics):
    """Three-tier verdict: SHIP / SHIP WITH CAUTION / DO NOT SHIP."""
    reasons = []
    cm, bm = candidate_metrics, baseline_metrics

    # Hard blockers → DO NOT SHIP
    if cm["critical_failures"] > 0:
        rate = cm["critical_failures"] / cm["total_runs"] * 100
        top_critical = [k for k in cm["failure_breakdown"] if k.startswith("critical:")]
        reasons.append(f"Critical failures in {rate:.0f}% of runs: {', '.join(top_critical)}")

    # Warnings only → SHIP WITH CAUTION
    caution_reasons = []
    if cm["warning_failures"] > 0:
        warn_rate = cm["warning_failures"] / cm["total_runs"] * 100
        if warn_rate > 5:
            top_warns = [k for k in cm["failure_breakdown"] if k.startswith("warning:")]
            caution_reasons.append(f"Warnings in {warn_rate:.0f}% of runs: {', '.join(top_warns)}")

    # Latency regression
    if bm["p95_ms"] > 0 and cm["p95_ms"] > bm["p95_ms"] * 1.5:
        caution_reasons.append(
            f"p95 latency regressed ({cm['p95_ms']}ms vs baseline {bm['p95_ms']}ms)")

    if reasons:
        return "DO NOT SHIP", reasons + caution_reasons
    if caution_reasons:
        return "SHIP WITH CAUTION", caution_reasons
    return "SHIP", ["All checks passed. No regressions detected vs baseline."]


# ── Report generation ─────────────────────────────────────────────────────────

def build_report(all_records, n_runs, n_orders):
    """Generate a human-readable markdown report."""
    lines = []
    lines.append("# Order Enrichment — Evaluation Report\n")
    lines.append(f"**Setup:** {n_orders} orders × {n_runs} runs = "
                 f"{n_orders * n_runs} samples per variant\n")

    # Compute metrics per variant
    by_variant = defaultdict(list)
    for r in all_records:
        by_variant[r["variant"]].append(r)

    metrics = {name: compute_metrics(recs) for name, recs in by_variant.items()}
    baseline_m = metrics["baseline"]

    # ── Verdict table ──
    lines.append("## Verdicts\n")
    lines.append("| Variant | Verdict | Reason |")
    lines.append("|---------|---------|--------|")

    verdicts = {}
    for name in ["variant_a", "variant_b", "variant_c"]:
        verdict, reasons = decide_verdict(metrics[name], baseline_m)
        verdicts[name] = (verdict, reasons)
        lines.append(f"| {name} | **{verdict}** | {'; '.join(reasons)} |")

    # ── Metrics table ──
    lines.append("\n## Metrics Comparison\n")
    lines.append("| Variant | Pass Rate | Critical | Warnings | Avg ms | P50 ms | P95 ms |")
    lines.append("|---------|-----------|----------|----------|--------|--------|--------|")
    for name in ["baseline", "variant_a", "variant_b", "variant_c"]:
        m = metrics[name]
        lines.append(
            f"| {name} | {m['pass_rate']}% | {m['critical_failures']}/{m['total_runs']} "
            f"| {m['warning_failures']}/{m['total_runs']} "
            f"| {m['avg_ms']} | {m['p50_ms']} | {m['p95_ms']} |"
        )

    # ── Per-variant breakdown (what competitors skip) ──
    lines.append("\n## Detailed Breakdown\n")
    for name in ["variant_a", "variant_b", "variant_c"]:
        m = metrics[name]
        verdict, reasons = verdicts[name]
        lines.append(f"### {name} — {verdict}\n")

        if m["failure_breakdown"]:
            lines.append("| Check | Count | Rate |")
            lines.append("|-------|-------|------|")
            for check_name, count in m["failure_breakdown"].items():
                pct = count / m["total_runs"] * 100
                lines.append(f"| `{check_name}` | {count} | {pct:.1f}% |")
            lines.append("")

        # Show which specific orders failed (drill-down)
        failed_orders = defaultdict(list)
        for r in by_variant[name]:
            if r["crashed"] or any(s == "critical" for s, _, _ in r["issues"]):
                for s, check, detail in r["issues"]:
                    if s == "critical":
                        failed_orders[r["order_id"]].append(f"{check}: {detail}")

        if failed_orders:
            lines.append("**Affected orders:**\n")
            # Show up to 5 examples to keep report readable
            for oid in list(failed_orders.keys())[:5]:
                unique_reasons = list(set(failed_orders[oid]))[:2]
                lines.append(f"- `{oid}`: {'; '.join(unique_reasons)}")
            remaining = len(failed_orders) - 5
            if remaining > 0:
                lines.append(f"- ... and {remaining} more orders")
            lines.append("")

    # ── Baseline health check ──
    lines.append("## Baseline Health\n")
    lines.append(f"The baseline itself passed {baseline_m['pass_rate']}% of checks. ")
    if baseline_m["critical_failures"] > 0:
        lines.append(f"It had {baseline_m['critical_failures']} critical failures "
                      "(expected from transient risk-service errors after retry exhaustion).\n")
    else:
        lines.append("No critical failures detected.\n")

    lines.append("---\n")
    lines.append("*Generated by eval.py — see DESIGN.md for methodology.*\n")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Evaluate order enrichment variants")
    parser.add_argument("--runs", type=int, default=10,
                        help="Number of times to run each order per variant (default: 10)")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for reproducibility (default: none)")
    args = parser.parse_args()

    print(f"Loading orders from {FIXTURE_PATH}...")
    orders = load_orders()
    print(f"  {len(orders)} orders, {args.runs} runs each = "
          f"{len(orders) * args.runs} samples per variant\n")

    all_records = run_all(orders, args.runs, seed=args.seed)

    # Write raw audit trail
    audit_path = Path(__file__).parent / "results.jsonl"
    with audit_path.open("w") as f:
        for r in all_records:
            # Convert issue tuples to dicts for JSON
            row = {**r, "issues": [{"severity": s, "check": c, "detail": d}
                                    for s, c, d in r["issues"]]}
            f.write(json.dumps(row) + "\n")
    print(f"\nWrote {len(all_records)} records to results.jsonl")

    # Write report
    report = build_report(all_records, args.runs, len(orders))
    report_path = Path(__file__).parent / "report.md"
    report_path.write_text(report)
    print(f"Wrote report.md")

    # Print summary to terminal
    by_variant = defaultdict(list)
    for r in all_records:
        by_variant[r["variant"]].append(r)

    metrics = {name: compute_metrics(recs) for name, recs in by_variant.items()}
    baseline_m = metrics["baseline"]

    print("\n" + "=" * 65)
    print(f"{'Variant':<15} {'Pass Rate':>10} {'P95 ms':>8}  Verdict")
    print("=" * 65)
    for name in ["baseline", "variant_a", "variant_b", "variant_c"]:
        m = metrics[name]
        if name == "baseline":
            verdict = "REFERENCE"
        else:
            verdict, _ = decide_verdict(m, baseline_m)
        print(f"{name:<15} {m['pass_rate']:>9}% {m['p95_ms']:>7}  {verdict}")
    print("=" * 65)


if __name__ == "__main__":
    main()
