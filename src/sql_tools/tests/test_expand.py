import unittest
from unittest.mock import Mock, patch

from sql_tools.expand import ExpansionResponseError, expand_statement, expand_sql_statements


def example():
    return {"answer": "SELECT age FROM head", "context": "CREATE TABLE head (age INT)",
            "dialect": "mysql", "status_history": ["validated"]}


class ExpansionTests(unittest.TestCase):
    def test_validation_without_history_and_empty_row_history(self):
        validation = Mock(schema_status="valid", dialect="mysql", status_history=["validated"])
        validation.to_dict.return_value = {
                "schema_status": "valid", "dialect": "mysql", "status_history": ["validated"],
                }
        for history in (None, [], ["validated"]):
            with self.subTest(history=history):
                row = example()
                if history is None:
                    row.pop("status_history")
                else:
                    row["status_history"] = history
                with patch("sql_tools.expand.validate_sql", return_value=validation):
                    expand_statement(row, {"context": "CREATE TABLE head (age INT, id INT)"})
                self.assertEqual(row["status_history"], (history or []) + ["extended"])

    def test_valid_extension_preserves_history_and_sql(self):
        row = example()
        expand_statement(row, {"context": "CREATE TABLE head (age INT, name VARCHAR(255))"})
        self.assertEqual(row["status_history"], ["validated", "extended"])
        self.assertEqual(row["answer"], example()["answer"])
        self.assertEqual(row["original_context"], example()["context"])

    def test_invalid_candidate_is_not_applied(self):
        row = example()
        expand_statement(row, {"context": "CREATE TABLE head (name INT)"})
        self.assertEqual(row["context"], example()["context"])
        self.assertEqual(row["status_history"][-1], "extension_failed")

    def test_changed_query_is_rejected(self):
        row = example()
        with self.assertRaises(ExpansionResponseError):
            expand_statement(row, {"answer": "SELECT name FROM head", "context": "CREATE TABLE head (name INT)"})
        self.assertEqual(row["answer"], example()["answer"])

    def test_random_selection_and_limit_preserve_rows(self):
        client = Mock()
        client.send_request.return_value = ('{"context":"CREATE TABLE head (age INT, id INT)"}', 1, 2, 3, .1)
        rows = [example() for _ in range(3)]
        with patch("sql_tools.expand.random.random", side_effect=[.2, .8]):
            expanded, unchanged, failed, _seconds = expand_sql_statements(
                    rows, client, "Extend", probability=.5, max_rows=2, logger=Mock()
                    )
        self.assertEqual(expanded, rows[:1])
        self.assertEqual(unchanged, [])
        self.assertEqual(failed, [])
        self.assertEqual(rows[0]["status_history"], ["validated", "extended"])
        self.assertEqual(rows[1]["status_history"], ["validated"])
        client.send_request.assert_called_once()
        self.assertEqual(client.clear_history.call_count, 2)

    def test_failures_and_default_selection(self):
        client = Mock()
        client.send_request.side_effect = [ConnectionError("offline"), ("bad json", 1, 2, 0, .1),
                                           ('{"FAILURE":"ambiguous"}', 1, 2, 0, .1)]
        expanded, unchanged, rows, _seconds = expand_sql_statements(
                [example() for _ in range(3)], client, "Extend", logger=Mock()
                )
        self.assertEqual(expanded, [])
        self.assertEqual(unchanged, [])
        self.assertEqual(client.send_request.call_count, 3)
        for row in rows:
            self.assertEqual(row["status_history"], ["validated", "extension_failed"])

    def test_limits_and_probability(self):
        for probability in (0, 1, -.1, 1.1, float("nan")):
            with self.assertRaises(ValueError):
                expand_sql_statements([], Mock(), "Extend", probability=probability)
        client = Mock()
        self.assertEqual(
                expand_sql_statements([example()], client, "Extend", max_rows=0, logger=Mock()),
                ([], [], [], 0),
                )

    def test_unchanged_ddl_keeps_context(self):
        row = example()
        expand_statement(row, {"context": row["context"]})
        self.assertEqual(row["context"], example()["context"])
        self.assertEqual(row["status_history"], ["validated", "extension_unchanged"])
        self.assertNotIn("original_context", row)
