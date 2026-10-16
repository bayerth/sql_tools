import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sql_tools.run_sql_validation import run_sql_validation


class RunnerTests(unittest.TestCase):
    def test_splits_limit_outputs_and_history(self):
        dataset = {"train": [
            {"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)",
             "status_history": ["extended"]},
            {"answer": "SELECT (", "context": "CREATE TABLE t ("},
        ], "test": [{"answer": "SELECT 1", "context": None}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch("sql_tools.run_sql_validation._load_dataset", return_value=dataset):
                result = run_sql_validation("example/dataset", directory, max_rows=2)
                self.assertEqual(result["processed"], 2)
                self.assertEqual(result["valid"], 1)
                correct = json.loads((Path(directory) / "correct_prompts.json").read_text())
                failed = json.loads((Path(directory) / "incorrect_prompts.json").read_text())
                self.assertEqual(correct[0]["status_history"], ["extended", "validated"])
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0]["status_history"], ["validation_failed"])
                with self.assertRaises(FileExistsError):
                    run_sql_validation("example/dataset", directory, override=False)
                replaced = run_sql_validation(
                    "example/dataset", directory, max_rows=0, override=True
                )
            self.assertEqual(replaced["processed"], 0)
            written = json.loads((Path(directory) / "summary.json").read_text())
            self.assertEqual(written["processed"], 0)

    def test_standard_rows_and_empty_limit(self):
        dataset = {"train": [{"question": "List ids", "answer": "SELECT id FROM t",
                              "context": "CREATE TABLE t (id INT)"}]}
        for limit in (None, 0):
            with tempfile.TemporaryDirectory() as directory:
                with patch("sql_tools.run_sql_validation._load_dataset", return_value=dataset):
                    summary = run_sql_validation("example/dataset", directory, max_rows=limit)
                self.assertEqual(summary["valid"], 1 if limit is None else 0)

    def test_missing_output_logs_summary_without_files(self):
        dataset = {"train": [{"answer": "SELECT id FROM t",
                              "context": "CREATE TABLE t (id INT)"}]}
        logger = Mock()
        with patch("sql_tools.run_sql_validation._load_dataset", return_value=dataset):
            summary = run_sql_validation("example/dataset", logger=logger)
        self.assertEqual(summary["valid"], 1)
        messages = [call.args[0] for call in logger.info.call_args_list]
        self.assertTrue(any(message.startswith("Validation summary:") for message in messages))
        self.assertFalse(any("saved to" in message for message in messages))
