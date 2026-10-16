import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sql_tools.run_spider_hardness import run_spider_hardness


class SpiderHardnessRunnerTests(unittest.TestCase):
    def test_dry_run_limits_rows_and_preserves_output(self):
        dataset = {"train": [{"answer": "SELECT name FROM users"} for _ in range(30)]}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "absent"
            for limit, expected in ((None, 20), (3, 3), (0, 0), (25, 20)):
                with self.subTest(limit=limit), patch(
                    "sql_tools.run_spider_hardness._load_dataset", return_value=dataset
                ):
                    summary = run_spider_hardness("test", output, limit, dry_run=True)
                    self.assertEqual(summary["processed"], expected)
                    self.assertTrue(summary["dry_run"])
                    self.assertFalse(output.exists())
            output.mkdir()
            existing = output / "summary.json"
            existing.write_text("existing")
            with patch("sql_tools.run_spider_hardness._load_dataset", return_value=dataset):
                run_spider_hardness("test", output, 1, dry_run=True)
            self.assertEqual(existing.read_text(), "existing")
            self.assertEqual(list(output.iterdir()), [existing])

    def test_split_limit_metadata_and_unclassified_rows(self):
        source = {"answer": "SELECT name FROM users", "context": "original DDL",
                  "schema_status": "valid", "status_history": ["extended"]}
        dataset = {"train": [source], "test": [{"answer": ""}, {"answer": "SELECT 1"}]}
        with tempfile.TemporaryDirectory() as directory:
            with patch("sql_tools.run_spider_hardness._load_dataset", return_value=dataset):
                summary = run_spider_hardness("test", directory, max_rows=2)
            self.assertEqual(summary["processed"], 2)
            self.assertEqual(summary["spider_hardness"], {"easy": 1})
            self.assertNotIn("hardness", summary)
            self.assertEqual(summary["extended_hardness"], {"easy": 1})
            row = json.loads((Path(directory) / "classified_rows.json").read_text())[0]
            self.assertEqual(row["status_history"], ["extended", "classified"])
            self.assertNotIn("hardness", row)
            self.assertEqual(row["spider_hardness"], "easy")
            self.assertEqual(row["extended_hardness"], "easy")
            self.assertEqual(source["status_history"], ["extended"])
            self.assertEqual(row["schema_status"], "valid")
            self.assertEqual(row["context"], "original DDL")
            failed = json.loads((Path(directory) / "unclassified_rows.json").read_text())[0]
            self.assertEqual(failed["hardness_source"], {"split": "test", "row_index": 0})
            self.assertTrue(failed["classification_errors"])
            self.assertIsNone(failed["spider_hardness"])
            self.assertIsNone(failed["extended_hardness"])
            self.assertNotIn("status_history", failed)
            self.assertNotIn("hardness", source)
            with patch("sql_tools.run_spider_hardness._load_dataset", return_value=dataset):
                with self.assertRaises(FileExistsError):
                    run_spider_hardness("test", directory, override=False)
                replaced = run_spider_hardness("test", directory, max_rows=0, override=True)
            self.assertEqual(replaced["processed"], 0)
            written = json.loads((Path(directory) / "summary.json").read_text())
            self.assertEqual(written["processed"], 0)

    def test_local_json_loader_and_zero_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            source.write_text(json.dumps([{"answer": "SELECT name FROM users"}]))
            summary = run_spider_hardness(str(source), Path(directory) / "results")
            self.assertEqual(summary["classified"], 1)
            summary = run_spider_hardness(str(source), Path(directory) / "empty", max_rows=0)
            self.assertEqual(summary["processed"], 0)

    def test_history_records_unsuccessful_classification(self):
        dataset = {"train": [
            {"answer": "", "status_history": ["validation_failed"]},
            {"answer": "SELECT ROW_NUMBER() OVER () FROM users", "status_history": []},
            {"answer": "SELECT name FROM users", "status_history": None},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            with patch("sql_tools.run_spider_hardness._load_dataset", return_value=dataset):
                run_spider_hardness("test", directory)
            failed = json.loads((Path(directory) / "unclassified_rows.json").read_text())
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["status_history"], ["validation_failed", "not_classified"])
            classified = json.loads((Path(directory) / "classified_rows.json").read_text())
            window, plain = classified
            self.assertEqual(window["status_history"], ["extended"])
            self.assertIsNone(window["spider_hardness"])
            self.assertIsNotNone(window["extended_hardness"])
            self.assertEqual(plain["status_history"], ["classified"])

    def test_negative_limit(self):
        with self.assertRaises(ValueError):
            run_spider_hardness("unused", "unused", max_rows=-1)
