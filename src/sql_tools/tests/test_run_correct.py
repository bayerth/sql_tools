from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sql_tools.run_repair_sql_statements import DRY_RUN_ROWS, run_correct_sql_statements
from sql_tools.tests.test_correct import broken_row, candidate


def reply(payload):
    return json.dumps(payload), 1, 1, 0, 0.1


class CorrectionRunnerTests(unittest.TestCase):
    def run_rows(self, rows, output, client, **kwargs):
        with patch("sql_tools.run_repair_sql_statements._read_rows", return_value=iter(rows)):
            return run_correct_sql_statements("test", output, client=client, logger=Mock(), **kwargs)

    def test_dry_run_limit_and_no_files(self):
        client = Mock()
        client.send_request.return_value = reply(candidate())
        rows = [broken_row() for _ in range(25)]
        before = deepcopy(rows)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "absent"
            stats = self.run_rows(rows, output, client, dry_run=True, checkpoints=True)
            self.assertFalse(output.exists())
        self.assertEqual(stats["processed"], DRY_RUN_ROWS)
        self.assertEqual(stats["corrected"], DRY_RUN_ROWS)
        self.assertEqual(client.send_request.call_count, DRY_RUN_ROWS)
        self.assertEqual(rows, before)
        for call in client.send_request.call_args_list:
            self.assertTrue(call.kwargs["ignore_history"])
            self.assertEqual(call.kwargs["context_length"], 32768)

    def test_outputs_and_openai_messages(self):
        row = broken_row()
        row["messages"] = [{"role": "system", "content": row["context"]},
                           {"role": "user", "content": row["question"]},
                           {"role": "assistant", "content": row["answer"]}]
        client = Mock()
        client.send_request.return_value = reply(candidate())
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows([row], directory, client, openai_format=True, checkpoints=True)
            output = json.loads((Path(directory) / "correction_corrected_rows.json").read_text())[0]
            self.assertEqual(output["messages"][0]["content"], candidate()["context"])
            self.assertEqual(output["messages"][2]["content"], candidate()["answer"])
            self.assertEqual(stats["corrected"], 1)
            self.assertFalse(list(Path(directory).glob("*.tmp.jsonl")))
            with self.assertRaises(FileExistsError):
                self.run_rows([row], directory, client, override=False)
            replaced = self.run_rows([], directory, client, override=True)
            self.assertEqual(replaced["processed"], 0)
            written = json.loads((Path(directory) / "correction_stats.json").read_text())
            self.assertEqual(written["processed"], 0)

    def test_batch_result_ids_and_rounded_limit(self):
        client = Mock()
        client.send_request.return_value = reply(
                {"results": [
                        {"id": 1, "status": "ambiguous", "reason": "Unknown", "required_evidence": []},
                        {"id": 0, **candidate(True)},
                        ]}
                )
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows(
                    [broken_row() for _ in range(3)], directory, client,
                    batch_size=2, max_rows=1, require_source_confirmation=True
                    )
            pending = json.loads((Path(directory) / "correction_pending_rows.json").read_text())[0]
            self.assertEqual(pending["correction_source"]["row_index"], 0)
            self.assertEqual(pending["answer"], broken_row()["answer"])
        self.assertEqual(stats["processed"], 2)
        self.assertEqual(stats["pending"], 1)
        self.assertEqual(stats["ambiguous"], 1)

    def test_batch_accepts_valid_candidates_without_source_by_default(self):
        row = broken_row()
        row.pop("original_headers")
        client = Mock()
        client.send_request.return_value = reply({"results": [{"id": 0, **candidate(True)}]})
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows([row], directory, client, batch_size=2)
            output = json.loads((Path(directory) / "correction_corrected_rows.json").read_text())[0]
        self.assertEqual((stats["corrected"], stats["pending"]), (1, 0))
        self.assertFalse(stats["require_source_confirmation"])
        self.assertTrue(output["correction_history"][-1]["requires_source_confirmation"])

    def test_include_history_false_omits_correction_history(self):
        client = Mock()
        client.send_request.return_value = reply(candidate())
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows(
                    [broken_row()], directory, client, include_history=False, checkpoints=True,
                    checkpoint_size=1,
                    )
            output = json.loads((Path(directory) / "correction_corrected_rows.json").read_text())[0]
            self.assertFalse(list(Path(directory).glob("*.tmp.jsonl")))
        self.assertTrue(stats["processed"])
        self.assertFalse(stats["include_history"])
        self.assertNotIn("correction_history", output)
        self.assertEqual(output["correction_status"], "corrected")
        self.assertIn("correction_source", output)
        self.assertIn("answer", output)

    def test_duplicate_and_missing_ids(self):
        for responses, corrected, failed in (
                    ([{"id": 0, **candidate()}, {"id": 0, **candidate()}], 0, 2),
                    ([{"id": 1, **candidate()}], 1, 1),
                ):
            client = Mock()
            client.send_request.return_value = reply({"results": responses})
            with tempfile.TemporaryDirectory() as directory:
                stats = self.run_rows([broken_row(), broken_row()], directory, client, batch_size=2)
            self.assertEqual((stats["corrected"], stats["failed"]), (corrected, failed))

    def test_request_failures_and_invalid_input_do_not_stop_later_rows(self):
        client = Mock()
        client.send_request.side_effect = [RuntimeError("offline"), (None, 0, 0, 0, 0), reply(candidate())]
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows([{}, broken_row(), broken_row(), broken_row()], directory, client)
        self.assertEqual((stats["processed"], stats["failed"], stats["corrected"]), (4, 3, 1))
        self.assertEqual(client.clear_history.call_count, 6)

    def test_checkpoint_survives_interruption(self):
        client = Mock()
        client.send_request.side_effect = [reply(candidate()), KeyboardInterrupt()]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyboardInterrupt):
                self.run_rows(
                        [broken_row(), broken_row()], directory, client,
                        checkpoints=True, checkpoint_size=1
                        )
            checkpoint = Path(directory) / "correction_corrected_rows.tmp.jsonl"
            self.assertEqual(len(checkpoint.read_text().splitlines()), 1)
            self.assertFalse((Path(directory) / "correction_stats.json").exists())
            with self.assertRaises(FileExistsError):
                self.run_rows([broken_row()], directory, client, override=False)

    def test_context_budget_skips_oversized_single_row(self):
        client = Mock()
        with tempfile.TemporaryDirectory() as directory:
            stats = self.run_rows([broken_row()], directory, client, context_length=10)
        self.assertEqual(stats["failed"], 1)
        client.send_request.assert_not_called()
