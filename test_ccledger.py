"""Tests for ccledger. Standard library only: python -m unittest discover"""

import json
import os
import shutil
import tempfile
import unittest

import ccledger


def entry(**over):
    base = {
        "type": "assistant",
        "timestamp": "2026-08-20T13:56:05.636Z",
        "sessionId": "sess-1",
        "cwd": "/home/u/projects/alpha",
        "gitBranch": "main",
        "isSidechain": False,
        "requestId": "req-1",
        "uuid": "uuid-1",
        "message": {
            "id": "msg-1",
            "model": "claude-opus-4-5-20260101",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 20,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 50,
                "cache_creation": {"ephemeral_5m_input_tokens": 20,
                                   "ephemeral_1h_input_tokens": 30},
            },
        },
    }
    base.update(over)
    return base


class ExtractTokens(unittest.TestCase):
    def test_split_cache_tiers_win_over_flat_total(self):
        got = ccledger._extract_tokens(entry()["message"]["usage"])
        self.assertEqual(got["cache_write_5m"], 20)
        self.assertEqual(got["cache_write_1h"], 30)

    def test_flat_total_falls_back_to_5m(self):
        usage = {"input_tokens": 1, "output_tokens": 2,
                 "cache_creation_input_tokens": 77}
        got = ccledger._extract_tokens(usage)
        self.assertEqual(got["cache_write_5m"], 77)
        self.assertEqual(got["cache_write_1h"], 0)

    def test_zero_split_does_not_shadow_flat_total(self):
        usage = {"cache_creation_input_tokens": 9,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                    "ephemeral_1h_input_tokens": 0}}
        got = ccledger._extract_tokens(usage)
        self.assertEqual(got["cache_write_5m"], 9)

    def test_missing_fields_default_to_zero(self):
        got = ccledger._extract_tokens({})
        self.assertEqual(set(got.values()), {0})


class ParseEntry(unittest.TestCase):
    def test_project_comes_from_cwd_not_directory(self):
        rec = ccledger.parse_entry(entry(), "/root/mangled-dir/x.jsonl", "/root")
        self.assertEqual(rec.project, "alpha")
        self.assertEqual(rec.day, "2026-08-20")
        self.assertEqual(rec.agent, "main")

    def test_directory_used_when_cwd_absent(self):
        e = entry()
        del e["cwd"]
        path = os.path.join("/root", "mangled-dir", "x.jsonl")
        rec = ccledger.parse_entry(e, path, "/root")
        self.assertEqual(rec.project, "mangled-dir")

    def test_sidechain_marked_as_subagent(self):
        rec = ccledger.parse_entry(entry(isSidechain=True), "/root/a.jsonl", "/root")
        self.assertEqual(rec.agent, "subagent")

    def test_synthetic_model_is_not_billable(self):
        e = entry()
        e["message"]["model"] = "<synthetic>"
        self.assertIsNone(ccledger.parse_entry(e, "/root/a.jsonl", "/root"))

    def test_entries_without_usage_are_skipped(self):
        self.assertIsNone(ccledger.parse_entry({"type": "user", "message": {}},
                                               "/root/a.jsonl", "/root"))

    def test_all_zero_usage_is_skipped(self):
        e = entry()
        e["message"]["usage"] = {"input_tokens": 0, "output_tokens": 0}
        self.assertIsNone(ccledger.parse_entry(e, "/root/a.jsonl", "/root"))

    def test_bad_timestamp_yields_empty_day(self):
        rec = ccledger.parse_entry(entry(timestamp="not-a-date"), "/root/a.jsonl", "/root")
        self.assertEqual(rec.day, "")


class Filtering(unittest.TestCase):
    def records(self):
        return [
            ccledger.parse_entry(entry(), "/root/a.jsonl", "/root"),
            ccledger.parse_entry(entry(timestamp="2026-08-25T01:00:00Z",
                                       message=dict(entry()["message"], id="msg-2")),
                                 "/root/a.jsonl", "/root"),
        ]

    def test_since_and_until_are_inclusive_bounds(self):
        kept = list(ccledger.filter_records(self.records(), since="2026-08-21"))
        self.assertEqual([r.day for r in kept], ["2026-08-25"])
        kept = list(ccledger.filter_records(self.records(), until="2026-08-21"))
        self.assertEqual([r.day for r in kept], ["2026-08-20"])

    def test_duplicate_message_ids_are_counted_once(self):
        dupes = [ccledger.parse_entry(entry(), "/root/a.jsonl", "/root"),
                 ccledger.parse_entry(entry(uuid="other"), "/root/b.jsonl", "/root")]
        self.assertEqual(len(list(ccledger.filter_records(dupes))), 1)

    def test_project_filter_is_case_insensitive_substring(self):
        self.assertEqual(len(list(ccledger.filter_records(self.records(), project="ALP"))), 2)
        self.assertEqual(len(list(ccledger.filter_records(self.records(), project="zzz"))), 0)


class Pricing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "p.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return self.path

    def test_cache_tiers_derive_from_input_price(self):
        table = ccledger.load_prices(self.write({"m": {"input": 10, "output": 50}}))
        self.assertAlmostEqual(table["m"]["cache_write_5m"], 12.5)
        self.assertAlmostEqual(table["m"]["cache_write_1h"], 20.0)
        self.assertAlmostEqual(table["m"]["cache_read"], 1.0)

    def test_explicit_override_beats_derived_ratio(self):
        table = ccledger.load_prices(
            self.write({"m": {"input": 10, "output": 50, "cache_read": 7}}))
        self.assertAlmostEqual(table["m"]["cache_read"], 7.0)

    def test_underscore_keys_are_comments_not_models(self):
        table = ccledger.load_prices(
            self.write({"_comment": ["not a model"], "m": {"input": 1, "output": 2}}))
        self.assertEqual(list(table), ["m"])

    def test_shipped_example_price_file_loads(self):
        here = os.path.dirname(os.path.abspath(__file__))
        table = ccledger.load_prices(os.path.join(here, "prices.example.json"))
        self.assertTrue(table)
        self.assertNotIn("_comment", table)

    def test_missing_required_key_is_rejected(self):
        with self.assertRaises(ValueError):
            ccledger.load_prices(self.write({"m": {"input": 1}}))

    def test_longest_prefix_wins(self):
        table = {"claude-opus": {"input": 1}, "claude-opus-4-5": {"input": 2}}
        self.assertEqual(ccledger.price_lookup(table, "claude-opus-4-5-20260101"),
                         {"input": 2})

    def test_unknown_model_has_no_price(self):
        self.assertIsNone(ccledger.price_lookup({"a": {}}, "b"))

    def test_cost_matches_hand_calculation(self):
        table = ccledger.load_prices(self.write({"claude-opus-4-5": {"input": 10,
                                                                    "output": 50}}))
        rec = ccledger.parse_entry(entry(), "/root/a.jsonl", "/root")
        # 10*10 + 20*50 + 20*12.5 + 30*20 + 100*1  == 2050 per million
        self.assertAlmostEqual(ccledger.cost_of(rec, table), 2050 / 1_000_000.0)


class Report(unittest.TestCase):
    def test_totals_row_sums_every_bucket(self):
        recs = [ccledger.parse_entry(entry(), "/root/a.jsonl", "/root"),
                ccledger.parse_entry(entry(cwd="/home/u/projects/beta",
                                           message=dict(entry()["message"], id="m2")),
                                     "/root/a.jsonl", "/root")]
        rows, headers, total, unpriced = ccledger.build_report(recs, "project")
        self.assertEqual({r[0] for r in rows}, {"alpha", "beta"})
        self.assertEqual(total[0], "TOTAL")
        self.assertEqual(total[headers.index("total")], "360")  # 2 x (10+20+20+30+100)
        self.assertEqual(unpriced, [])

    def test_rows_sorted_by_total_tokens_descending(self):
        small = entry(cwd="/x/small", message={"id": "s", "model": "m",
                                               "usage": {"input_tokens": 1}})
        big = entry(cwd="/x/big", message={"id": "b", "model": "m",
                                           "usage": {"input_tokens": 999}})
        recs = [ccledger.parse_entry(small, "/r/a.jsonl", "/r"),
                ccledger.parse_entry(big, "/r/a.jsonl", "/r")]
        rows, _, _, _ = ccledger.build_report(recs, "project")
        self.assertEqual([r[0] for r in rows], ["big", "small"])

    def test_unpriced_models_are_reported_not_silently_zeroed(self):
        rec = ccledger.parse_entry(entry(), "/root/a.jsonl", "/root")
        rows, headers, total, unpriced = ccledger.build_report([rec], "project",
                                                               table={"other": {}})
        self.assertEqual(unpriced, ["claude-opus-4-5-20260101"])
        self.assertIn("usd", headers)


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        sub = os.path.join(self.root, "proj-dir")
        os.makedirs(sub)
        with open(os.path.join(sub, "s.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(entry()) + "\n")
            fh.write("{ this line is truncated\n")  # live sessions leave partial lines
            fh.write("\n")
            fh.write(json.dumps(entry(isSidechain=True,
                                      message=dict(entry()["message"], id="m2"))) + "\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_malformed_lines_do_not_abort_the_scan(self):
        recs = list(ccledger.load_records(self.root))
        self.assertEqual(len(recs), 2)

    def test_grouping_by_agent_separates_subagent_work(self):
        rows, headers, total, _ = ccledger.build_report(
            ccledger.filter_records(ccledger.load_records(self.root)), "agent")
        self.assertEqual({r[0] for r in rows}, {"main", "subagent"})

    def test_cli_runs_and_exits_zero(self):
        self.assertEqual(ccledger.main(["--root", self.root, "--json"]), 0)

    def test_cli_reports_no_matches_as_exit_one(self):
        self.assertEqual(ccledger.main(["--root", self.root, "--since", "2099-01-01"]), 1)

    def test_cli_rejects_missing_root(self):
        self.assertEqual(ccledger.main(["--root", os.path.join(self.root, "nope")]), 2)


class SubagentAttribution(unittest.TestCase):
    def parse(self, **over):
        return ccledger.parse_entry(entry(**over), "/root/a.jsonl", "/root")

    def test_main_loop_records_are_named_main(self):
        self.assertEqual(self.parse().subagent, "main")

    def test_attribution_agent_names_the_subagent_type(self):
        rec = self.parse(isSidechain=True, attributionAgent="Explore")
        self.assertEqual(rec.subagent, "Explore")
        self.assertEqual(rec.agent, "subagent")

    def test_attribution_agent_is_trimmed(self):
        self.assertEqual(self.parse(isSidechain=True,
                                    attributionAgent="  cash-scout \n").subagent,
                         "cash-scout")

    def test_falls_back_to_agent_id_when_type_is_absent(self):
        rec = self.parse(isSidechain=True, agentId="a5650b4cb05c62f0b")
        self.assertEqual(rec.subagent, "agent:a5650b4cb05c62f0b")

    def test_falls_back_again_when_nothing_identifies_the_subagent(self):
        self.assertEqual(self.parse(isSidechain=True).subagent, "subagent")

    def test_blank_attribution_agent_does_not_win_over_agent_id(self):
        rec = self.parse(isSidechain=True, attributionAgent="   ", agentId="x1")
        self.assertEqual(rec.subagent, "agent:x1")

    def test_main_loop_is_never_labelled_by_a_stray_attribution(self):
        # A non-sidechain line carrying attributionAgent is still main-loop work.
        self.assertEqual(self.parse(attributionAgent="Explore").subagent, "main")

    def records(self):
        return [
            self.parse(isSidechain=True, attributionAgent="Explore"),
            self.parse(isSidechain=True, attributionAgent="zip-lander",
                       message=dict(entry()["message"], id="m2")),
            self.parse(message=dict(entry()["message"], id="m3")),
        ]

    def test_subagent_filter_matches_case_insensitive_substring(self):
        got = list(ccledger.filter_records(self.records(), subagent="ZIP"))
        self.assertEqual([r.subagent for r in got], ["zip-lander"])

    def test_subagent_filter_can_isolate_the_main_loop(self):
        got = list(ccledger.filter_records(self.records(), subagent="main"))
        self.assertEqual([r.subagent for r in got], ["main"])

    def test_grouping_by_subagent_gives_one_row_per_agent_type(self):
        rows, headers, total, _ = ccledger.build_report(self.records(), "subagent")
        self.assertEqual({r[0] for r in rows}, {"Explore", "zip-lander", "main"})
        self.assertEqual(headers[0], "subagent")
        self.assertEqual(total[1], "3")

    def test_subagent_is_an_accepted_by_choice(self):
        self.assertIn("subagent", ccledger.GROUP_KEYS)


class UnfinalizedUsage(unittest.TestCase):
    """anthropics/claude-code#84223: subagent output tokens can be missing."""

    def snapshot(self, **over):
        """A mid-stream record: stop_reason is None and output is a stub."""
        e = entry(**over)
        e["message"] = dict(e["message"], stop_reason=None,
                            usage=dict(e["message"]["usage"], output_tokens=1))
        return e

    def test_snapshot_records_are_marked_not_final(self):
        rec = ccledger.parse_entry(self.snapshot(), "/root/a.jsonl", "/root")
        self.assertFalse(rec.final)

    def test_finalized_record_wins_dedup_even_when_it_comes_second(self):
        first = ccledger.parse_entry(self.snapshot(), "/root/a.jsonl", "/root")
        second = ccledger.parse_entry(entry(), "/root/a.jsonl", "/root")
        got = list(ccledger.dedupe([first, second]))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].tokens["output"], 20)

    def test_two_snapshots_keep_the_larger_output(self):
        small = ccledger.parse_entry(self.snapshot(), "/root/a.jsonl", "/root")
        big = ccledger.parse_entry(self.snapshot(), "/root/a.jsonl", "/root")
        big.tokens["output"] = 65
        got = list(ccledger.dedupe([small, big]))
        self.assertEqual(got[0].tokens["output"], 65)

    def test_health_counts_only_unfinalized_subagent_requests(self):
        recs = [
            # a subagent request that never got its final usage
            ccledger.parse_entry(self.snapshot(isSidechain=True,
                                               attributionAgent="Explore"),
                                 "/root/a.jsonl", "/root"),
            # a healthy subagent request
            ccledger.parse_entry(entry(isSidechain=True,
                                       attributionAgent="Explore"),
                                 "/root/a.jsonl", "/root"),
            # main-loop records never count, finalized or not
            ccledger.parse_entry(self.snapshot(), "/root/a.jsonl", "/root"),
        ]
        self.assertEqual(ccledger.usage_health(recs), (2, 1))

    def test_health_is_zero_when_every_subagent_request_is_final(self):
        recs = [ccledger.parse_entry(entry(isSidechain=True,
                                           attributionAgent="Explore"),
                                     "/root/a.jsonl", "/root")]
        self.assertEqual(ccledger.usage_health(recs), (1, 0))


class ReplayAttribution(unittest.TestCase):
    """A resumed session replays a response and rewrites its ids on the way."""

    def copies(self):
        """The same response as found in the original file and in a resumed one."""
        original = ccledger.parse_entry(entry(), "/root/a.jsonl", "/root")
        replay = ccledger.parse_entry(
            entry(cwd="/home/u/projects/beta", sessionId="sess-2",
                  gitBranch="feature"),
            "/root/b.jsonl", "/root")
        return original, replay

    def test_disagreeing_fields_are_flagged_on_the_survivor(self):
        got = list(ccledger.dedupe(list(self.copies())))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].ambiguous, frozenset({"project", "session", "branch"}))

    def test_day_survives_replay_because_the_timestamp_does(self):
        got = list(ccledger.dedupe(list(self.copies())))
        self.assertNotIn("day", got[0].ambiguous)
        self.assertEqual(ccledger.group_value(got[0], "day"), "2026-08-20")

    def test_ambiguous_usage_gets_its_own_row_instead_of_a_guess(self):
        records = list(ccledger.dedupe(list(self.copies())))
        rows, headers, total, _ = ccledger.build_report(records, "project")
        self.assertEqual([r[0] for r in rows], [ccledger.AMBIGUOUS])

    def test_tokens_are_neither_lost_nor_doubled_by_the_ambiguous_row(self):
        records = list(ccledger.dedupe(list(self.copies())))
        _, headers, total, _ = ccledger.build_report(records, "project")
        # one response, counted once: 10 + 20 + 20 + 30 + 100
        self.assertEqual(total[headers.index("total")], "180")

    def test_agreeing_copies_keep_their_real_project(self):
        twice = [ccledger.parse_entry(entry(), "/root/a.jsonl", "/root"),
                 ccledger.parse_entry(entry(), "/root/b.jsonl", "/root")]
        got = list(ccledger.dedupe(twice))
        self.assertEqual(got[0].ambiguous, frozenset())
        self.assertEqual(ccledger.group_value(got[0], "project"), "alpha")

    def test_health_reports_the_calls_and_tokens_it_cannot_place(self):
        records = list(ccledger.dedupe(list(self.copies())))
        self.assertEqual(ccledger.attribution_health(records, "project"), (1, 180))
        self.assertEqual(ccledger.attribution_health(records, "day"), (0, 0))


if __name__ == "__main__":
    unittest.main()
