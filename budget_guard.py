"""agent-budget-guard: offline budget accounting for agent runs.

Takes a JSONL trace of tool calls and a JSON budget, and reports which
limits were exceeded and how close the run came to the rest. Purely
accounting: it never runs the agent, never calls a model, and cannot stop
anything. It answers "did this run stay inside its budget?" after the fact,
or "would this recorded run have fit?" before re-running one like it.

Trace: one JSON object per line, one per tool call:
  {"tool": "web.search", "ts": 1.0, "tokens": 420, "cost": 0.0013}
  - tool   required, non-empty string
  - ts     optional finite number (seconds; wall time = max ts - min ts)
  - tokens optional non-negative finite number
  - cost   optional non-negative finite number

Budget (JSON object, all fields optional):
  {"max_calls": 50, "per_tool": {"web.search": 5},
   "max_tokens": 100000, "max_cost": 0.50, "max_wall_seconds": 300,
   "warn_fraction": 0.8}
  warn_fraction defaults to 0.8: at or above that fraction of a limit, the
  run gets a warning even when it is technically inside budget.
"""

import json
import math
import sys

MAX_BYTES = 1 << 20          # 1 MiB cap per input file
MAX_RECORDS = 10_000
MAX_DEPTH = 64

ERROR = "error"
WARNING = "warning"


class InvalidInput(Exception):
    """Raised when an input cannot be parsed within the safety caps."""


def _check_depth(node, depth=0):
    if depth > MAX_DEPTH:
        raise InvalidInput(f"JSON nesting deeper than {MAX_DEPTH}")
    if isinstance(node, dict):
        for v in node.values():
            _check_depth(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _check_depth(v, depth + 1)


def _load_jsonl(text, what):
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise InvalidInput(f"{what} larger than {MAX_BYTES} bytes")
    records = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(records) >= MAX_RECORDS:
            raise InvalidInput(f"more than {MAX_RECORDS} records")
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, RecursionError) as e:
            raise InvalidInput(f"{what} line {lineno}: invalid JSON: {e}")
        _check_depth(rec)
        if not isinstance(rec, dict):
            raise InvalidInput(f"{what} line {lineno}: record is not an object")
        records.append((lineno, rec))
    return records


def _load_json(text, what):
    if len(text.encode("utf-8", "replace")) > MAX_BYTES:
        raise InvalidInput(f"{what} larger than {MAX_BYTES} bytes")
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as e:
        raise InvalidInput(f"{what}: invalid JSON: {e}")
    _check_depth(obj)
    if not isinstance(obj, dict):
        raise InvalidInput(f"{what} must be a JSON object")
    return obj


def _finite_nonneg(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def check_budget(records, budget):
    """Return deterministically ordered findings for parsed inputs.

    `records` is a list of (lineno, dict) from the trace; `budget` is the
    parsed budget object. Each finding is {severity, code, message}.
    """
    findings = []

    def add(sev, code, msg):
        findings.append({"severity": sev, "code": code, "message": msg})

    # Validate budget shape.
    limits = {}
    for key in ("max_calls", "max_tokens", "max_cost", "max_wall_seconds"):
        v = budget.get(key)
        if v is None:
            continue
        if not _finite_nonneg(v):
            raise InvalidInput(f"budget {key} must be a non-negative finite number")
        limits[key] = v
    per_tool = budget.get("per_tool", {})
    if not isinstance(per_tool, dict) or any(
            not isinstance(k, str) or not _finite_nonneg(v) for k, v in per_tool.items()):
        raise InvalidInput("budget per_tool must map tool names to non-negative numbers")
    warn_fraction = budget.get("warn_fraction", 0.8)
    if not (isinstance(warn_fraction, (int, float)) and not isinstance(warn_fraction, bool)
            and math.isfinite(warn_fraction) and 0 < warn_fraction <= 1):
        raise InvalidInput("budget warn_fraction must be in (0, 1]")

    # Account for the trace.
    calls = 0
    tool_counts = {}
    total_tokens = 0.0
    total_cost = 0.0
    saw_tokens = False
    saw_cost = False
    ts_min = None
    ts_max = None
    for lineno, rec in records:
        tool = rec.get("tool")
        if not isinstance(tool, str) or not tool:
            raise InvalidInput(f"trace line {lineno}: missing non-empty string tool")
        calls += 1
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        if "tokens" in rec:
            if not _finite_nonneg(rec["tokens"]):
                raise InvalidInput(f"trace line {lineno}: tokens must be a non-negative finite number")
            total_tokens += rec["tokens"]
            saw_tokens = True
        if "cost" in rec:
            if not _finite_nonneg(rec["cost"]):
                raise InvalidInput(f"trace line {lineno}: cost must be a non-negative finite number")
            total_cost += rec["cost"]
            saw_cost = True
        if "ts" in rec:
            ts = rec["ts"]
            if not (isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts)):
                raise InvalidInput(f"trace line {lineno}: ts must be a finite number")
            ts_min = ts if ts_min is None else min(ts_min, ts)
            ts_max = ts if ts_max is None else max(ts_max, ts)
    wall = (ts_max - ts_min) if ts_min is not None else None

    def verdict(code, label, used, limit, unit):
        if used > limit:
            add(ERROR, code, f"{label} {used:g}{unit} exceeded limit {limit:g}{unit}")
        elif used >= limit * warn_fraction and limit > 0:
            add(WARNING, code,
                f"{label} {used:g}{unit} at {used / limit:.0%} of limit {limit:g}{unit}")

    if "max_calls" in limits:
        verdict("calls-budget", "total calls", calls, limits["max_calls"], "")
    for tool, limit in sorted(per_tool.items()):
        used = tool_counts.get(tool, 0)
        verdict("tool-budget", f"calls to {tool!r}", used, limit, "")
    if "max_tokens" in limits and saw_tokens:
        verdict("token-budget", "tokens", total_tokens, limits["max_tokens"], "")
    if "max_cost" in limits and saw_cost:
        verdict("cost-budget", "cost", total_cost, limits["max_cost"], "")
    if "max_wall_seconds" in limits and wall is not None:
        verdict("wall-budget", "wall time", wall, limits["max_wall_seconds"], "s")

    order = {ERROR: 0, WARNING: 1}
    findings.sort(key=lambda f: (order[f["severity"]], f["code"], f["message"]))
    return findings


def has_errors(findings):
    return any(f["severity"] == ERROR for f in findings)


def main(argv):
    if len(argv) != 3:
        print("usage: budget_guard.py TRACE.jsonl BUDGET.json", file=sys.stderr)
        return 2
    try:
        with open(argv[1], "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidInput(f"trace larger than {MAX_BYTES} bytes")
        records = _load_jsonl(raw.decode("utf-8"), "trace")
        with open(argv[2], "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise InvalidInput(f"budget larger than {MAX_BYTES} bytes")
        budget = _load_json(raw.decode("utf-8"), "budget")
        findings = check_budget(records, budget)
    except (OSError, UnicodeDecodeError, InvalidInput) as e:
        print(f"invalid input: {e}", file=sys.stderr)
        return 2
    if not findings:
        print("within budget")
    for f in findings:
        print(f"{f['severity']}: {f['code']}: {f['message']}")
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
