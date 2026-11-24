"""Offline watch-only replay of the AI advisor. No network and no orders.

The expected win rates below are counted by hand from the labels in
examples/ai-eval.synthetic.json. They are a check of the logging and the
report arithmetic. They are not a profitability result.

Default gates, unchanged: AI-only confidence 0.7, confluence confidence 0.5.

confluence = confluence-win (win, R 2), confluence-flat (win, R 0),
confluence-unknown (win unknown, R unknown).
  decided trades = 2, wins = 2, unknown = 1
  win rate = 2/2 = 1
  average R = (2 + 0) / 2 = 1
  Coercing the unknown win into a loss and its missing R into 0 would instead
  give win rate 2/3 and average R (2 + 0 + 0) / 3.

rules = rules-win (loss, R -1), rules-unknown (unknown).
  decided trades = 1, wins = 0, unknown = 1
  win rate = 0/1 = 0
  average R = -1
  Coercing the unknown into a loss with R 0 would give average R -0.5.

ai = ai-win only (win, R 1). Win rate 1, average R 1.

gap is only confluence-unknown: decided trades 0, so the win rate stays
unknown rather than 0. pennant is only confluence-flat: average R is the
supplied 0, which is a real flat result, not a stand-in for unknown.
"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from dataclasses import replace

from momentum.ai_advisor import AIAdvisor, AdvisorConfig
from momentum.ai_eval import EvalError, StubModel, _ImmediateExecutor, main, replay, replay_bytes
from momentum.ai_report import build_report, render_text
from momentum.session import PaperSession
from worker_fakes import FakeVenue, armed_config


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ai-eval.synthetic.json"

# Hand-counted from the labels above. Do not derive these from the code under test.
EXPECTED_SOURCE = {
    "confluence-win": "confluence",
    "rules-win": "rules",
    "ai-win": "ai",
    "confluence-unknown": "confluence",
    "rules-unknown": "rules",
    "confluence-flat": "confluence",
    "ai-below-threshold": None,
    "ai-not-buy": None,
}
EXPECTED_BY_SOURCE = {
    "ai": {"verdicts": 1, "trades": 1, "wins": 1, "unknown": 0, "win_rate": 1.0, "avg_r": 1.0},
    "confluence": {"verdicts": 3, "trades": 2, "wins": 2, "unknown": 1, "win_rate": 1.0, "avg_r": 1.0},
    "rules": {"verdicts": 2, "trades": 1, "wins": 0, "unknown": 1, "win_rate": 0.0, "avg_r": -1.0},
}


def _document():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


class ReplayTests(unittest.TestCase):
    def replay(self, document, directory):
        return replay(document, log_dir=directory)

    def test_synthetic_replay_matches_hand_checked_report(self):
        document = _document()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch("momentum.ai_advisor.GeminiClient", side_effect=AssertionError("gemini")):
                result = self.replay(document, directory)
            records = (Path(directory) / "advisor_feedback.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(result["evaluation"]["mode"], "watch_only")
        self.assertEqual(result["evaluation"]["model"], "stub")
        self.assertFalse(result["evaluation"]["execution_enabled"])
        self.assertEqual(result["evaluation"]["orders_submitted"], 0)
        self.assertFalse(result["evaluation"]["paper_trading_enabled"])
        self.assertFalse(result["evaluation"]["profitability_claimed"])
        self.assertEqual(result["evaluation"]["source_verification"], "not_performed")
        self.assertEqual(result["evaluation"]["min_confidence"], 0.7)
        self.assertEqual(result["evaluation"]["confluence_min_confidence"], 0.5)
        self.assertEqual(result["evaluation"]["stub_calls"], 8)
        self.assertEqual({row["id"]: row["source"] for row in result["decisions"]}, EXPECTED_SOURCE)
        self.assertTrue(all(row["submitted"] is False for row in result["decisions"]))
        logged = [json.loads(line) for line in records]
        candidates = [row for row in logged if row["type"] == "candidate"]
        outcomes = [row for row in logged if row["type"] == "outcome"]
        signals = [row for row in logged if row["type"] == "signal"]
        self.assertEqual(len(candidates), 6)
        self.assertTrue(all(row["submitted"] is False and row["mode"] == "advise" for row in candidates))
        self.assertTrue(all(row["intent"]["qty"] is None for row in candidates))
        self.assertEqual(sorted(row["setup_id"] for row in candidates),
                         ["ai-win", "confluence-flat", "confluence-unknown", "confluence-win",
                          "rules-unknown", "rules-win"])
        self.assertEqual({row["setup_id"]: row["source"] for row in candidates},
                         {key: value for key, value in EXPECTED_SOURCE.items() if value is not None})
        self.assertEqual(len(outcomes), 6)
        unknown = [row for row in outcomes if row["setup_id"] in {"confluence-unknown", "rules-unknown"}]
        self.assertEqual(len(unknown), 2)
        for row in unknown:
            self.assertIsNone(row["win"])
            self.assertIsNone(row["r_multiple"])
        flat = next(row for row in outcomes if row["setup_id"] == "confluence-flat")
        self.assertIs(flat["win"], True)
        self.assertEqual(flat["r_multiple"], 0.0)
        self.assertEqual(len(signals), 8)
        self.assertTrue(all(row["status"] == "ok" for row in signals))
        self.assertEqual(result["report"]["signals"]["calls"], {"buy": 5, "sell": 2, "wait": 1})
        self.assertEqual((result["report"]["candidates"], result["report"]["outcomes"], result["report"]["matched"]),
                         (6, 6, 6))
        self.assertEqual(result["report"]["by_source"], EXPECTED_BY_SOURCE)
        gap = result["report"]["by_pattern"]["gap"]
        self.assertEqual((gap["trades"], gap["unknown"], gap["win_rate"], gap["avg_r"]), (0, 1, None, None))
        pennant = result["report"]["by_pattern"]["pennant"]
        self.assertEqual((pennant["trades"], pennant["wins"], pennant["win_rate"], pennant["avg_r"]),
                         (1, 1, 1.0, 0.0))
        self.assertIsNotNone(pennant["avg_r"])
        double_top = result["report"]["by_pattern"]["double_top"]
        self.assertIsNone(double_top["win_rate"])
        self.assertIsNone(double_top["avg_r"])
        self.assertEqual(result["report"]["by_pattern"]["range"],
                         {"verdicts": 1, "trades": 1, "wins": 0, "unknown": 0, "win_rate": 0.0, "avg_r": -1.0})
        buy = result["report"]["by_ai_call"]["buy"]
        # Four buy candidates; the below-threshold buy is not a decision.
        # Known R values are 2, 1 and 0. (2 + 1 + 0) / 3 = 1. The unknown R is absent.
        self.assertEqual(buy, {"verdicts": 4, "trades": 3, "wins": 3, "unknown": 1, "win_rate": 1.0, "avg_r": 1.0})
        self.assertIsNone(result["report"]["by_ai_call"]["sell"]["win_rate"])
        text = render_text(result["report"])
        self.assertIn("By source (rules vs AI vs confluence)", text)
        self.assertIn("0%", text)
        self.assertIn("not evidence of profitability", result["report"]["note"])
        blob = json.dumps(result)
        self.assertNotIn("GEMINI", blob)
        self.assertNotIn("api_key", blob)
        self.assertNotIn("generativelanguage", blob)

    def test_recorded_provenance_is_not_treated_as_verified(self):
        document = _document()
        document["provenance"] = "recorded"
        with tempfile.TemporaryDirectory() as directory:
            result = self.replay(document, directory)
        self.assertEqual(result["evaluation"]["provenance"], "recorded")
        self.assertEqual(result["evaluation"]["source_verification"], "not_performed")
        self.assertEqual(result["report"]["by_source"], EXPECTED_BY_SOURCE)

    def test_environment_flag_is_left_alone(self):
        before = os.environ.get("PAPER_TRADING_ENABLED")
        with tempfile.TemporaryDirectory() as directory:
            result = self.replay(_document(), directory)
        self.assertEqual(os.environ.get("PAPER_TRADING_ENABLED"), before)
        self.assertFalse(result["evaluation"]["paper_trading_enabled"])

    def test_stub_model_has_no_key_and_replay_never_constructs_gemini(self):
        stub = StubModel()
        self.assertFalse(hasattr(stub, "api_key"))
        with self.assertRaises(TypeError):
            StubModel(api_key="should-not-exist")
        source = Path(replay.__code__.co_filename).read_text(encoding="utf-8")
        for banned in ("GeminiClient(", "urllib", "import socket", "os.environ", "generativelanguage",
                       ".cancel(", ".replace("):
            self.assertNotIn(banned, source)

    def test_rejects_order_requests_and_coerced_outcomes(self):
        document = _document()
        document["execute"] = True
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvalError) as caught:
                self.replay(document, directory)
        self.assertEqual(caught.exception.code, "input_schema_invalid")
        bad = _document()
        bad["frames"][0]["outcome"]["win"] = 0
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvalError) as caught:
                self.replay(bad, directory)
        self.assertEqual(caught.exception.code, "outcome_invalid")
        missing_r = _document()
        missing_r["frames"][3]["outcome"]["r_multiple"] = "0"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvalError) as caught:
                self.replay(missing_r, directory)
        self.assertEqual(caught.exception.code, "outcome_invalid")
        attached = _document()
        attached["frames"][-1]["outcome"] = {"win": False, "r_multiple": 0}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvalError) as caught:
                self.replay(attached, directory)
        self.assertEqual(caught.exception.code, "outcome_without_decision")

    def test_duplicate_key_and_cli(self):
        raw = EXAMPLE.read_bytes()
        broken = raw.replace(b'"kind"', b'"kind"', 1)
        # A second kind key is a duplicate wherever it is inserted beside the first.
        broken = broken.replace(b'"kind": "ai_advisor_watch_replay"',
                                b'"kind": "ai_advisor_watch_replay", "kind": "ai_advisor_watch_replay"', 1)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(EvalError) as caught:
                replay_bytes(broken, log_dir=directory)
        self.assertEqual(caught.exception.code, "duplicate_json_key")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            from contextlib import redirect_stderr, redirect_stdout
            import io
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                self.assertEqual(main([str(EXAMPLE), "--output", str(output)]), 0)
            self.assertEqual(out.getvalue().strip(), "ai_eval_complete")
            self.assertEqual(err.getvalue(), "")
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(saved["report"]["by_source"], EXPECTED_BY_SOURCE)
            self.assertNotIn("log_path", saved)
            again = io.StringIO()
            with redirect_stderr(again):
                self.assertEqual(main([str(EXAMPLE), "--output", str(output)]), 2)
            self.assertEqual(again.getvalue().strip(), "ai_eval_error: output_exists")


class ReportUnknownTests(unittest.TestCase):
    def test_unknown_outcomes_stay_unknown(self):
        # Two rules rows. Neither win is a boolean. One R is a real zero.
        # Win rate must stay unknown. The zero R is kept because it was supplied.
        # Treating both wins as losses would make the win rate 0.
        records = [
            {"type": "candidate", "setup_id": "u1", "source": "rules", "pattern": "gap",
             "call": "buy", "submitted": False},
            {"type": "outcome", "setup_id": "u1", "win": None, "r_multiple": None},
            {"type": "candidate", "setup_id": "u2", "source": "rules", "pattern": "gap",
             "call": "buy", "submitted": False},
            {"type": "outcome", "setup_id": "u2", "win": 0, "r_multiple": 0.0},
            {"type": "candidate", "setup_id": "loss", "source": "ai", "pattern": "range",
             "call": "wait", "submitted": False},
            {"type": "outcome", "setup_id": "loss", "win": False, "r_multiple": -1},
            {"type": "candidate", "setup_id": "bool-r", "source": "confluence", "pattern": "pennant",
             "call": "buy", "submitted": False},
            {"type": "outcome", "setup_id": "bool-r", "win": True, "r_multiple": True},
        ]
        report = build_report(records)
        rules = report["by_source"]["rules"]
        self.assertEqual((rules["verdicts"], rules["trades"], rules["wins"], rules["unknown"]), (2, 0, 0, 2))
        self.assertIsNone(rules["win_rate"])
        self.assertEqual(rules["avg_r"], 0.0)
        self.assertIsNotNone(rules["avg_r"])
        # A known loss is allowed to be a zero win rate. That zero is not an unknown.
        ai = report["by_source"]["ai"]
        self.assertEqual(ai["win_rate"], 0.0)
        self.assertEqual(ai["unknown"], 0)
        self.assertEqual(ai["avg_r"], -1.0)
        # A boolean is not an R multiple, so it does not become 1.
        confluence = report["by_source"]["confluence"]
        self.assertEqual(confluence["win_rate"], 1.0)
        self.assertIsNone(confluence["avg_r"])
        gap = report["by_pattern"]["gap"]
        self.assertIsNone(gap["win_rate"])
        self.assertNotEqual(gap["win_rate"], 0)
        text = render_text(report)
        self.assertIn("-", text)


class WatchOnlySessionTests(unittest.TestCase):
    def test_session_watch_cycle_logs_without_broker_mutations(self):
        class ReadOnlyBooks:
            def __init__(self):
                self.calls = []

            def account(self):
                self.calls.append("account")
                return {"status": "ACTIVE", "trading_blocked": False, "account_blocked": False,
                        "equity": "10000", "buying_power": "10000"}

            def positions(self):
                self.calls.append("positions")
                return []

            def open_orders(self):
                self.calls.append("open_orders")
                return []

            def submit(self, *_args, **_kwargs):
                self.calls.append("submit")
                raise AssertionError("watch-only cycle must not submit")

            def cancel(self, *_args, **_kwargs):
                self.calls.append("cancel")
                raise AssertionError("watch-only cycle must not cancel")

            def replace(self, *_args, **_kwargs):
                self.calls.append("replace")
                raise AssertionError("watch-only cycle must not replace")

        with tempfile.TemporaryDirectory() as directory:
            # The reviewed hash is computed with execution off. The snapshot is the
            # existing invented one: rules still reject it, and the intent builder
            # is not given looser gates.
            cfg = replace(armed_config(directory), paper_enabled=False,
                          paper_reviewed_config_hash="")
            cfg = replace(cfg, paper_reviewed_config_hash=cfg.review_hash())
            self.assertFalse(cfg.paper_enabled)
            self.assertEqual(cfg.paper_blockers(), ["paper_execution_not_enabled"])
            books = ReadOnlyBooks()
            venue = FakeVenue(cfg)
            now = venue.now
            stub = StubModel()
            stub.push(json.dumps({"symbol": "DEMO-A", "call": "buy", "pattern": "breakout",
                                  "confidence": 0.9, "reason": "Stub read for a watch-only cycle."}))
            advisor = AIAdvisor(AdvisorConfig(enabled=True, api_key="", model="stub-local", mode="advise",
                                              min_interval_seconds=0,
                                              log_dir=str(Path(directory) / "ai")),
                                client=stub, executor=_ImmediateExecutor(), today=lambda: "2026-10-16")
            with PaperSession(cfg, books, clock=lambda: now, advisor=advisor) as session:
                result = session.cycle(snapshots=[venue.snapshot(bid="9.79", ask="9.80")],
                                       session_ledger=venue.ledger(), execute=False)
            self.assertTrue(result["reconciled"], result.get("errors"))
            self.assertFalse(result["execute_requested"])
            self.assertFalse(result["execution_configured"])
            self.assertEqual(books.calls, ["account", "positions", "open_orders"])
            self.assertFalse(any(action.get("submitted") for action in result["actions"]))
            decision = result["decisions"][0]
            self.assertEqual(decision["strategy"]["reasons"], ["waiting_for_breakout"])
            self.assertFalse(decision["strategy"]["accepted"])
            logged = [json.loads(line) for line in (Path(directory) / "ai" / "advisor_feedback.jsonl").read_text().splitlines()]
            candidates = [row for row in logged if row["type"] == "candidate"]
            self.assertEqual(len(candidates), 1)
            self.assertEqual((candidates[0]["source"], candidates[0]["submitted"], candidates[0]["outcome"],
                              candidates[0]["call"]), ("ai", False, "advise_only_not_traded", "buy"))
            self.assertEqual(stub.calls, 1)
            self.assertFalse(cfg.paper_enabled)


if __name__ == "__main__":
    unittest.main()
