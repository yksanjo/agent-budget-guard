"""Synthetic demo: score one tidy run and one runaway run against a budget.

All traces here are made up. No model calls, no network, nothing executes.
"""
import json
import budget_guard

budget = {
    "max_calls": 10,
    "per_tool": {"web.search": 3},
    "max_tokens": 5000,
    "max_cost": 0.05,
    "max_wall_seconds": 60,
    "warn_fraction": 0.8,
}

tidy = [
    {"tool": "web.search", "ts": 1, "tokens": 400, "cost": 0.002},
    {"tool": "web.fetch", "ts": 2, "tokens": 900, "cost": 0.003},
    {"tool": "text.summarize", "ts": 4, "tokens": 1200, "cost": 0.004},
]

runaway = [
    {"tool": "web.search", "ts": 1, "tokens": 400, "cost": 0.002},
    {"tool": "web.search", "ts": 3, "tokens": 400, "cost": 0.002},
    {"tool": "web.search", "ts": 5, "tokens": 400, "cost": 0.002},
    {"tool": "web.search", "ts": 7, "tokens": 400, "cost": 0.002},
    {"tool": "web.fetch", "ts": 9, "tokens": 1500, "cost": 0.010},
    {"tool": "text.summarize", "ts": 40, "tokens": 2500, "cost": 0.030},
    {"tool": "text.summarize", "ts": 70, "tokens": 2500, "cost": 0.030},
]

for label, calls in [("tidy", tidy), ("runaway", runaway)]:
    recs = budget_guard._load_jsonl("\n".join(json.dumps(c) for c in calls), "trace")
    findings = budget_guard.check_budget(recs, budget)
    print(f"--- {label} run ({len(calls)} calls) vs budget {json.dumps(budget)} ---")
    if not findings:
        print("within budget")
    for f in findings:
        print(f"{f['severity']}: {f['code']}: {f['message']}")
    print()
