import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import budget_guard


def trace(calls):
    return budget_guard._load_jsonl(
        "\n".join(json.dumps(c) for c in calls), "trace")


def codes(findings):
    return [f["code"] for f in findings]


class AccountingTests(unittest.TestCase):
    def test_within_budget_no_findings(self):
        recs = trace([{"tool": "a"}, {"tool": "b"}])
        self.assertEqual(budget_guard.check_budget(recs, {"max_calls": 5}), [])

    def test_calls_exceeded(self):
        recs = trace([{"tool": "a"}] * 6)
        f = budget_guard.check_budget(recs, {"max_calls": 5})
        self.assertEqual(codes(f), ["calls-budget"])
        self.assertTrue(budget_guard.has_errors(f))

    def test_per_tool_budget(self):
        recs = trace([{"tool": "a"}] * 4 + [{"tool": "b"}])
        f = budget_guard.check_budget(recs, {"per_tool": {"a": 3}})
        self.assertEqual(codes(f), ["tool-budget"])

    def test_per_tool_zero_used_is_quiet(self):
        recs = trace([{"tool": "a"}])
        f = budget_guard.check_budget(recs, {"per_tool": {"b": 2}})
        self.assertEqual(f, [])

    def test_token_and_cost_budgets(self):
        recs = trace([
            {"tool": "a", "tokens": 600, "cost": 0.40},
            {"tool": "a", "tokens": 600, "cost": 0.40},
        ])
        f = budget_guard.check_budget(recs, {"max_tokens": 1000, "max_cost": 1.0})
        c = codes(f)
        self.assertIn("token-budget", c)          # 1200 > 1000, error
        tok = [x for x in f if x["code"] == "token-budget"][0]
        self.assertEqual(tok["severity"], budget_guard.ERROR)
        cost = [x for x in f if x["code"] == "cost-budget"][0]
        self.assertEqual(cost["severity"], budget_guard.WARNING)  # 0.8 of 1.0

    def test_warn_fraction_threshold(self):
        recs = trace([{"tool": "a"}] * 7)
        f = budget_guard.check_budget(recs, {"max_calls": 10})
        self.assertEqual(f, [])  # 70% < 80% default
        f = budget_guard.check_budget(recs, {"max_calls": 10, "warn_fraction": 0.7})
        self.assertEqual(codes(f), ["calls-budget"])
        self.assertFalse(budget_guard.has_errors(f))

    def test_wall_time(self):
        recs = trace([
            {"tool": "a", "ts": 10},
            {"tool": "a", "ts": 20},
            {"tool": "a", "ts": 400},
        ])
        f = budget_guard.check_budget(recs, {"max_wall_seconds": 300})
        self.assertEqual(codes(f), ["wall-budget"])
        f = budget_guard.check_budget(recs, {"max_wall_seconds": 500})
        self.assertEqual(f, [])

    def test_missing_optional_fields_skips_that_budget(self):
        # No tokens in trace: token budget is not evaluated, no error.
        recs = trace([{"tool": "a"}] * 100)
        f = budget_guard.check_budget(recs, {"max_tokens": 5})
        self.assertEqual(f, [])

    def test_deterministic_order(self):
        recs = trace([{"tool": "b"}] * 3 + [{"tool": "a"}] * 9)
        budget = {"max_calls": 10, "per_tool": {"a": 5, "b": 2}}
        f1 = codes(budget_guard.check_budget(recs, budget))
        f2 = codes(budget_guard.check_budget(recs, budget))
        self.assertEqual(f1, f2)


class InputValidationTests(unittest.TestCase):
    def test_bad_budget_limit_rejected(self):
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": "a"}]), {"max_calls": -1})
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": "a"}]), {"warn_fraction": 1.5})
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": "a"}]), {"per_tool": {"a": "many"}})

    def test_bad_trace_fields_rejected(self):
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": 3}]), {})
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": "a", "tokens": float("nan")}]), {})
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard.check_budget(trace([{"tool": "a", "cost": -0.1}]), {})

    def test_caps(self):
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard._load_jsonl("{bad", "trace")
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard._load_jsonl("[1,2]", "trace")
        with self.assertRaises(budget_guard.InvalidInput):
            budget_guard._load_json("[1,2]", "budget")


class OracleTests(unittest.TestCase):
    """Exhaustive oracle: every trace of length 0..8 over two tools, each
    record with 0 or 1 tokens, checked against an independent accumulation
    written with plain loops (no shared code with the implementation)."""

    def test_all_small_traces(self):
        events = [("a", 0), ("a", 1), ("b", 0), ("b", 1)]
        budget = {"max_calls": 4, "per_tool": {"a": 3, "b": 2},
                  "max_tokens": 3, "warn_fraction": 0.75}
        checked = 0
        for length in range(9):
            for combo in itertools.product(events, repeat=length):
                recs = trace([{"tool": t, "tokens": k} for t, k in combo])
                got = budget_guard.check_budget(recs, budget)
                # Independent oracle.
                n_a = sum(1 for t, _ in combo if t == "a")
                n_b = sum(1 for t, _ in combo if t == "b")
                tok = sum(k for _, k in combo)
                want = []
                if length > 4:
                    want.append(("error", "calls-budget"))
                elif 4 * 0.75 <= length and length > 0 and 4 > 0:
                    want.append(("warning", "calls-budget"))
                for name, n, lim in (("a", n_a, 3), ("b", n_b, 2)):
                    if n > lim:
                        want.append(("error", "tool-budget"))
                    elif lim > 0 and n >= lim * 0.75:
                        want.append(("warning", "tool-budget"))
                if tok > 3:
                    want.append(("error", "token-budget"))
                elif tok >= 3 * 0.75 and tok > 0:
                    want.append(("warning", "token-budget"))
                self.assertEqual(
                    Counter((x["severity"], x["code"]) for x in got),
                    Counter(want), f"mismatch on {combo}")
                checked += 1
        self.assertEqual(checked, sum(4 ** k for k in range(9)))


class CliTests(unittest.TestCase):
    def run_cli(self, trace_text, budget_obj):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(trace_text)
            tpath = fh.name
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(budget_obj, fh)
            bpath = fh.name
        return subprocess.run(
            [sys.executable, str(Path(__file__).parent / "budget_guard.py"), tpath, bpath],
            capture_output=True, text=True)

    def test_cli_within_exit_0(self):
        r = self.run_cli('{"tool": "a"}\n', {"max_calls": 5})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("within budget", r.stdout)

    def test_cli_exceeded_exit_1(self):
        r = self.run_cli('{"tool": "a"}\n{"tool": "a"}\n', {"max_calls": 1})
        self.assertEqual(r.returncode, 1)
        self.assertIn("calls-budget", r.stdout)

    def test_cli_warning_only_exit_0(self):
        r = self.run_cli('{"tool": "a"}\n' * 9, {"max_calls": 10})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("calls-budget", r.stdout)

    def test_cli_invalid_exit_2(self):
        r = self.run_cli("{bad json", {"max_calls": 1})
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid input", r.stderr)

    def test_cli_exact_1mib_boundary(self):
        rec = {"tool": "a", "pad": ""}
        line = json.dumps(rec)
        rec["pad"] = "x" * (budget_guard.MAX_BYTES - len(line) - 1)
        text = json.dumps(rec) + "\n"
        self.assertEqual(len(text.encode()), budget_guard.MAX_BYTES)
        r = self.run_cli(text, {})
        self.assertEqual(r.returncode, 0, r.stderr)
        rec["pad"] += "x"
        r = self.run_cli(json.dumps(rec) + "\n", {})
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
