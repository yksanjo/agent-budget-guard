# agent-budget-guard

Offline budget accounting for agent runs.

Agent runs spend calls, tokens, money and wall-clock time. This tool reads a recorded run and a budget, and reports which limits were exceeded and how close the run came to the rest. It is accounting, not enforcement: it never runs the agent, never calls a model, and cannot stop anything.

## Inputs

Trace (JSONL, one tool call per line):

```jsonl
{"tool": "web.search", "ts": 1.0, "tokens": 420, "cost": 0.0013}
{"tool": "web.fetch",  "ts": 2.0, "tokens": 900, "cost": 0.0030}
```

- `tool` - required, non-empty string.
- `ts` - optional finite number; wall time is `max ts - min ts`.
- `tokens`, `cost` - optional non-negative finite numbers.

Budget (JSON object, every field optional):

```json
{
  "max_calls": 50,
  "per_tool": {"web.search": 5},
  "max_tokens": 100000,
  "max_cost": 0.50,
  "max_wall_seconds": 300,
  "warn_fraction": 0.8
}
```

`warn_fraction` defaults to 0.8: at or above that fraction of a limit you get a warning, even inside budget.

## What it reports

| Code | Meaning |
| --- | --- |
| `calls-budget` | total call count vs `max_calls` |
| `tool-budget` | per-tool call count vs `per_tool` |
| `token-budget` | summed `tokens` vs `max_tokens` (skipped if no record carries tokens) |
| `cost-budget` | summed `cost` vs `max_cost` (skipped if no record carries cost) |
| `wall-budget` | `max ts - min ts` vs `max_wall_seconds` (skipped if no record carries ts) |

Over the limit is an error; at or above `warn_fraction` of it is a warning. Findings are sorted deterministically (severity, code, message), so the same inputs always produce the same report.

## Use

```sh
python3 budget_guard.py trace.jsonl budget.json   # score a run
python3 demo.py                                   # synthetic demo
python3 -m unittest test_budget_guard             # 18 tests
```

Exit codes: `0` within budget (warnings may still print), `1` a limit was exceeded, `2` invalid input.

## Safety caps

The CLI refuses either input over 1 MiB, JSON nested deeper than 64 levels, and traces with more than 10,000 records - all as exit 2, never a crash.

## Tests

18 tests, including an exhaustive independent oracle: every trace of length 0..8 over two tools with 0/1 tokens (87,381 traces) is checked against a separately written plain-loop accumulation, plus the exact 1 MiB accept/reject boundary.

## Limits

- **Accounting, not enforcement.** Nothing here throttles, blocks or kills a run. Pair it with whatever actually executes your agent if you want limits enforced live.
- **Trusted numbers.** Tokens and cost are whatever the trace claims. If the recorder undercounts, the verdict undercounts.
- **After the fact.** It scores recorded runs (or replays of runs like the one you are about to repeat). It does not predict what a new run will cost.
- **Wall time needs timestamps.** Without `ts` fields there is no wall-time verdict, and timestamps are taken at face value.
- **No semantic judgment.** A run inside budget can still be useless; a run over budget can be worth it. This only counts.
- **Trusted-input API.** The caps are enforced by the CLI and the input loaders; calling `check_budget` directly with hand-built objects bypasses record-level field validation only for fields you did not validate yourself.

## License

MIT - see [LICENSE](LICENSE).
