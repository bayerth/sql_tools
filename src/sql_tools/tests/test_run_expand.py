import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sql_tools.run_expand_sql_statements import run_expand_sql_statements


class ExpansionRunnerTests(unittest.TestCase):
    def test_context_stays_stable_when_switching_to_batches(self):
        for context_length in (8192, 32768):
            for batch_size in (0, 10):
                with self.subTest(context_length=context_length, batch_size=batch_size):
                    source = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)",
                               "question": "List ids. " + "Additional context. " * 30}
                              for _ in range(10)]
                    client = Mock()
                    client.send_request.return_value = ('{"FAILURE":"test"}', 0, 0, 0, 0)
                    with tempfile.TemporaryDirectory() as directory:
                        with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(source)):
                            run_expand_sql_statements(
                                "test", directory, client=client, logger=Mock(), dry_run=True,
                                batch_size=batch_size, context_length=context_length)
                    self.assertTrue(client.send_request.called)
                    for call in client.send_request.call_args_list:
                        self.assertEqual(call.kwargs["context_length"], context_length)
                    if batch_size:
                        # The smaller context must split a batch that fits in 32k.
                        self.assertEqual(client.send_request.call_count, 2 if context_length == 8192 else 1)

    def setUp(self):
        dry_run_limit = patch("sql_tools.run_expand_sql_statements.DRY_RUN_ROWS", 10)
        dry_run_limit.start()
        self.addCleanup(dry_run_limit.stop)

    def test_checkpoints_survive_interruption_and_round_interval(self):
        source = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)"}
                  for _ in range(12)]
        calls = []
        def consume(batches, **kwargs):
            chunk = [row for batch, _, _ in batches for row in batch]
            calls.append(len(chunk))
            if len(calls) == 2:
                raise KeyboardInterrupt()
            return chunk[:3], [], chunk[3:], 1.5, 0
        with tempfile.TemporaryDirectory() as directory:
            with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(source)), \
                 patch("sql_tools.run_expand_sql_statements.request_expansion_batches", side_effect=consume):
                with self.assertRaises(KeyboardInterrupt):
                    run_expand_sql_statements("test", directory, client=Mock(), logger=Mock(),
                                              checkpoints=True, checkpoint_size=5, batch_size=4)
            successful = [json.loads(line) for line in (Path(directory) / "expanded_sql_rows.tmp.jsonl").read_text().splitlines()]
            failed = [json.loads(line) for line in (Path(directory) / "expansion_failed_sql_statements.tmp.jsonl").read_text().splitlines()]
            self.assertEqual(calls, [8, 4])
            self.assertEqual((len(successful), len(failed)), (3, 5))
            self.assertEqual(successful[0]["expansion_source"]["row_index"], 0)
            self.assertFalse((Path(directory) / "expanded_sql_rows.json").exists())

    def test_checkpoints_removed_after_final_save(self):
        client = Mock()
        client.send_request.return_value = ('{"context":"CREATE TABLE t (id INT, name VARCHAR(255))"}', 1, 1, 0, .1)
        with tempfile.TemporaryDirectory() as directory:
            source = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)"}]
            with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(source)):
                run_expand_sql_statements("test", directory, client=client, logger=Mock(), checkpoints=True)
            self.assertFalse(list(Path(directory).glob("*.jsonl")))
            self.assertEqual(len(json.loads((Path(directory) / "expanded_sql_rows.json").read_text())), 1)

    def test_row_limit_rounds_up_for_batches(self):
        for batch_size, maximum, dry_run, expected in (
            (0, 3, False, 3), (4, 5, False, 8),
            (4, 0, False, 20), (8, 11, True, 10),
        ):
            with self.subTest(batch_size=batch_size, maximum=maximum, dry_run=dry_run):
                source = [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)"}
                          for _ in range(20)]
                def consume(rows, **kwargs):
                    return list(rows), [], [], 0.0
                def consume_batches(batches, **kwargs):
                    return [row for batch, _, _ in batches for row in batch], [], [], 0.0, 0
                with tempfile.TemporaryDirectory() as directory:
                    with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(source)), \
                         patch("sql_tools.run_expand_sql_statements.expand_sql_statements", side_effect=consume), \
                         patch("sql_tools.run_expand_sql_statements.request_expansion_batches", side_effect=consume_batches):
                        stats = run_expand_sql_statements(
                            "test", directory, client=Mock(), logger=Mock(),
                            max_rows=maximum, batch_size=batch_size, dry_run=dry_run)
                self.assertEqual(stats["processed"], expected)

    def test_dry_run_limits_requests_and_writes_nothing(self):
        client = Mock()
        client.send_request.return_value = ('{"context":"CREATE TABLE t (id INT, name VARCHAR(255))"}', 10, 2, 3, .5)
        rows = ({"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)"} for _ in range(20))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "absent"
            with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=rows):
                stats = run_expand_sql_statements("test", output, dry_run=True, client=client, logger=Mock())
            self.assertFalse(output.exists())
        self.assertEqual(client.send_request.call_count, 10)
        self.assertEqual(stats["expanded"], 10)
        self.assertGreaterEqual(stats["elapsed_seconds"], 0)

    def test_openai_output_and_request_failure(self):
        rows = [{"messages": [
            {"role": "system", "content": "CREATE TABLE t (id INT)"},
            {"role": "user", "content": "List ids"},
            {"role": "assistant", "content": "SELECT id FROM t"},
        ]} for _ in range(2)]
        client = Mock()
        ddl = "CREATE TABLE t (id INT, name VARCHAR(255))"
        client.send_request.side_effect = [(json.dumps({"context": ddl}), 1, 2, 0, .1), RuntimeError("offline")]
        with tempfile.TemporaryDirectory() as directory:
            with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(rows)):
                stats = run_expand_sql_statements("test", directory, openai_format=True, client=client, logger=Mock())
            expanded = json.loads((Path(directory) / "expanded_sql_rows.json").read_text())
            failed = json.loads((Path(directory) / "expansion_failed_sql_statements.json").read_text())
            self.assertEqual(expanded[0]["messages"][0]["content"], ddl)
            self.assertEqual(expanded[0]["status_history"], ["extended"])
            self.assertEqual(failed[0]["status_history"], ["extension_failed"])
            self.assertEqual(stats["failed"], 1)
            self.assertEqual(rows[0]["messages"][0]["content"], ddl)
            with self.assertRaises(FileExistsError):
                with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter(rows)):
                    run_expand_sql_statements(
                        "test", directory, openai_format=True, client=client, logger=Mock(),
                        override=False,
                    )
            with patch("sql_tools.run_expand_sql_statements._read_rows", return_value=iter([])):
                replaced = run_expand_sql_statements(
                    "test", directory, openai_format=True, client=client, logger=Mock(),
                    max_rows=0, override=True,
                )
            self.assertEqual(replaced["processed"], 0)
            self.assertEqual(json.loads((Path(directory) / "expanded_sql_rows.json").read_text()), [])
