import json
import unittest
from unittest.mock import Mock

from sql_tools.expansion_batches import prepare_expansion_batches, request_expansion_batches, process_expansion_batch


def rows(count):
    return [{"answer": "SELECT id FROM t", "context": "CREATE TABLE t (id INT)",
             "dialect": "mysql"} for _ in range(count)]


class BatchTests(unittest.TestCase):
    def test_batches_and_reordered_results(self):
        source = rows(3)
        batches = list(prepare_expansion_batches(source, 2))
        self.assertEqual([len(batch[0]) for batch in batches], [2, 1])
        client = Mock()
        ddl = "CREATE TABLE t (id INT, name VARCHAR(255))"
        client.send_request.side_effect = [
            (json.dumps({"results": [{"id": 1, "FAILURE": "no"}, {"id": 0, "context": ddl}]}), 0, 0, 0, 0),
            (json.dumps({"results": [{"id": 0, "context": ddl}]}), 0, 0, 0, 0),
        ]
        expanded, unchanged, failed, _seconds, _batch_count = request_expansion_batches(batches, client=client, logger=Mock())
        self.assertEqual(expanded, [source[0], source[2]])
        self.assertEqual(unchanged, [])
        self.assertEqual(failed, [source[1]])
        self.assertEqual(client.send_request.call_args.kwargs["context_length"], 32768)

    def test_missing_and_duplicate_ids(self):
        source = rows(2)
        expanded, unchanged, failed = process_expansion_batch(source, '{"results":[{"id":0},{"id":0}]}')
        self.assertEqual(expanded, [])
        self.assertEqual(unchanged, [])
        self.assertEqual(failed, source)
        self.assertTrue(all(row["status_history"] == ["extension_failed"] for row in source))
