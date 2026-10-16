import copy
import json
import unittest
from unittest.mock import Mock, patch

from sql_tools.repair import (
    RepairResponseError, RepairStats, repair_statement, repair_sql_statements,
    validate_repair_candidate,
)


def run_batch(*args, **kwargs):
    """Capture counters and reconstruct input order for existing metric assertions."""
    stats = RepairStats()
    with patch("sql_tools.repair.RepairStats", return_value=stats):
        repaired, failed = repair_sql_statements(*args, **kwargs)
    processed = {id(row) for row in repaired + failed}
    rows = [row for row in args[0] if id(row) in processed]
    return rows, stats


def broken_row():
    return {"question": "How many entries?", "answer": "SELECT COUNT( FROM t",
            "context": "CREATE TABLE t (id INT)", "dialect": "mysql",
            "syntax_status": "invalid", "ddl_syntax_status": "valid",
            "schema_status": "not_checked", "syntax_errors": ["Old error"],
            "repair_status": None, "spider_hardness": "extra"}


class RepairTests(unittest.TestCase):
    def test_candidate_validation_leaves_input_and_histories_untouched(self):
        row = broken_row()
        row["status_history"] = ["extended"]
        row["repair_history"] = [{"status": "repair_failed"}]
        original = copy.deepcopy(row)
        sql, ddl, validation = validate_repair_candidate(
            row, {"answer": "SELECT missing FROM t"}, dialect="mysql")
        self.assertEqual(sql, "SELECT missing FROM t")
        self.assertEqual(ddl, original["context"])
        self.assertEqual(validation.schema_status, "invalid")
        self.assertEqual(row, original)

    def test_partition_and_structured_errors_for_current_attempt(self):
        rows = [broken_row() for _ in range(5)]
        rows[1]["repair_complete"] = True
        error = RuntimeError("Internal Server Error")
        error.status_code = 500
        client = Mock()
        client.send_request.side_effect = [
            ('{"answer":"SELECT id FROM t"}', 1, 1, 0, .1), error,
            (None, 0, 0, 0, 0), ('{"FAILURE":"ambiguous"}', 1, 1, 0, .1),
            ('{"answer":"SELECT absent FROM t"}', 1, 1, 0, .1),
        ]
        repaired, failed = repair_sql_statements(rows, client, logger=Mock())
        self.assertEqual(repaired, rows[:1])
        self.assertEqual(failed, rows[1:])
        self.assertEqual([row["repair_history"][-1]["error_type"] for row in failed],
                         ["request_error", "response_error", "model_declined", "validation_error"])
        self.assertEqual(failed[0]["repair_history"][-1]["http_status"], 500)
        self.assertFalse(failed[0]["repair_history"][-1]["repair_complete"])

    def test_repair_only_validates_and_removes_stale_classification(self):
        row = broken_row()
        row.update(hardness="extra", extended_hardness="extreme", classification_status="classified",
                   classification_errors=[], component1=4, component2=2, others=3)
        with patch("sql_tools.classifier.classify_sql", side_effect=AssertionError("Unexpected classification")):
            repair_statement(row, {"answer": "SELECT id FROM t"})
        self.assertTrue(row["repair_complete"])
        for key in ("spider_hardness", "extended_hardness", "hardness", "classification_status",
                    "classification_errors", "component1", "component2", "others"):
            self.assertNotIn(key, row)
            self.assertNotIn(key, row["repair_history"][-1]["validation"])

    def test_logging_summary_and_per_statement_levels(self):
        client = Mock()
        client.send_request.side_effect = [
            ('{"answer": "SELECT id FROM t"}', 1000, 20, 0, 0.1),
            ('{"answer": "SELECT absent FROM t"}', 1000, 20, 0, 0.1),
            ('{"FAILURE": "ambiguous"}', 1000, 20, 0, 0.1),
        ]
        log = Mock()
        rows, stats = run_batch([broken_row() for _ in range(3)], client,
                                            "Repair", logger=log)
        self.assertEqual(stats.validation_counts["dml"]["repaired"], 2)
        self.assertEqual(stats.validation_counts["ddl"]["already_valid"], 2)
        self.assertEqual(stats.validation_counts["schema"]["repaired"], 1)
        self.assertEqual(stats.validation_counts["schema"]["invalid"], 1)
        self.assertEqual(stats.initially_invalid, {"dml": 3, "ddl": 0, "schema": 0})
        self.assertEqual(stats.invalid_repaired, {"dml": 2, "ddl": 0, "schema": 0})
        self.assertEqual(stats.failure_counts["model_declined"], 1)
        self.assertEqual((stats.sql_changed, stats.ddl_changed), (2, 0))
        for counts in stats.validation_counts.values():
            self.assertEqual(counts["no_candidate"], 1)
            self.assertEqual(sum(counts.values()), 3)
        info = "\n".join(call.args[0] for call in log.info.call_args_list)
        debug = "\n".join(call.args[0] for call in log.debug.call_args_list)
        self.assertIn("3 statements", info)
        self.assertIn("prompt tokens: 3,000", info)
        self.assertIn("DML error repair: 2 of 3 originally invalid (66.67%)", info)
        self.assertIn("SCHEMA error repair: 0 of 0 originally invalid (n/a)", info)
        for label in ("DML", "DDL", "SCHEMA"):
            self.assertIn(f"{label} results", info)
        self.assertNotIn("SELECT id FROM", info)
        self.assertIn("repair successful", debug)
        self.assertIn("incomplete repair", debug)
        self.assertIn("repair failed", debug)

    def test_apply_pair_and_preserve_originals_across_attempts(self):
        row = broken_row()
        original = row["answer"]
        row["status_history"] = ["extended"]
        row["sql_dialect"] = "generic"
        repair_statement(row, {"answer": "SELECT COUNT(*) FROM t"})
        self.assertTrue(row["repair_complete"])
        self.assertEqual(row["status_history"], ["extended", "repaired"])
        self.assertNotIn("previous_statuses", row["repair_history"][-1])
        self.assertEqual(row["context"], "CREATE TABLE t (id INT)")
        self.assertEqual(row["syntax_errors"], [])
        self.assertNotIn("spider_hardness", row)
        self.assertEqual(row["dialect"], "mysql")
        self.assertNotIn("sql_dialect", row)
        repair_statement(row, {"answer": "SELECT id FROM u", "context": "CREATE TABLE u (id INT)"})
        self.assertEqual(row["original_answer"], original)
        self.assertEqual(len(row["repair_history"]), 2)
        self.assertEqual(row["status_history"], ["extended", "repaired", "repaired"])
        self.assertTrue(row["repair_complete"])

    def test_partial_repair_and_regression(self):
        row = broken_row()
        repair_statement(row, {"answer": "SELECT absent FROM t"})
        self.assertEqual(row["schema_status"], "invalid")
        self.assertFalse(row["repair_complete"])
        self.assertEqual(row["status_history"], ["repair_failed"])
        repair_statement(row, {"answer": "SELECT ("})
        self.assertEqual(row["status_history"], ["repair_failed", "repair_failed"])
        self.assertNotIn("spider_hardness", row)

    def test_bad_responses_do_not_modify_pair(self):
        for result in ({"FAILURE": "ambiguous"}, [], {}, {"answer": None},
                       {"answer": "SELECT id FROM t", "context": None}):
            with self.subTest(result=result):
                row = broken_row()
                original = copy.deepcopy(row)
                with self.assertRaises(RepairResponseError):
                    repair_statement(row, result)
                original["status_history"] = ["repair_failed"]
                self.assertEqual(row, original)

    def test_batch_metrics_json_failure_and_isolation(self):
        client = Mock()
        client.send_request.side_effect = [
            (json.dumps({"answer": "SELECT COUNT(*) FROM t"}), 100, 20, 7, 1.5),
            ("not json", 50, 10, 3, 2.0),
            (json.dumps({"FAILURE": "ambiguous"}), 30, 5, 1, 0.5),
        ]
        rows, stats = run_batch([broken_row() for _ in range(4)], client,
                                            "Repair SQL", max_rows=3, logger=Mock())
        self.assertEqual((stats.processed, stats.complete, stats.failed), (3, 1, 2))
        self.assertEqual((stats.runtime, stats.prompt_tokens, stats.completion_tokens,
                          stats.reasoning_tokens), (4.0, 180, 35, 11))
        self.assertEqual(client.clear_history.call_count, 6)
        request = client.send_request.call_args_list[0]
        self.assertEqual(json.loads(request.args[0])["answer"], broken_row()["answer"])
        self.assertTrue(request.kwargs["ignore_history"])
        self.assertEqual(rows[1]["answer"], broken_row()["answer"])
        self.assertEqual([row["status_history"] for row in rows],
                         [["repaired"], ["repair_failed"], ["repair_failed"]])
        self.assertIn("ambiguous", rows[2]["repair_history"][-1]["error"])

    def test_request_failure_continues(self):
        client = Mock()
        client.send_request.side_effect = [ConnectionError("offline"),
                                          ('{"answer": "SELECT id FROM t"}', 1, 2, 0, 0.2)]
        rows, stats = run_batch([broken_row(), broken_row()], client, "Repair", logger=Mock())
        self.assertEqual((stats.failed, stats.complete), (1, 1))
        self.assertEqual([row["status_history"] for row in rows],
                         [["repair_failed"], ["repaired"]])
        self.assertIn("offline", rows[0]["repair_history"][-1]["error"])

    def test_empty_batch_and_zero_limit(self):
        for rows, max_rows in (([], None), ([broken_row()], 0)):
            client = Mock()
            repaired, stats = run_batch(rows, client, "Repair", max_rows=max_rows, logger=Mock())
            self.assertEqual(repaired, [])
            self.assertEqual(stats.processed, 0)
            client.send_request.assert_not_called()

    def test_local_bug_is_not_mislabeled_as_json_error(self):
        client = Mock()
        client.send_request.return_value = ('{"answer": "SELECT id FROM t"}', 1, 2, 0, 0.1)
        with patch("sql_tools.repair.validate_sql", side_effect=RuntimeError("local bug")):
            with self.assertRaisesRegex(RuntimeError, "local bug"):
                repair_sql_statements([broken_row()], client, "Repair", logger=Mock())
