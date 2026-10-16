from copy import deepcopy
import unittest

from sql_tools.correct import apply_correction, correction_payload


def broken_row():
    return {"question": "What is the building?", "answer": "SELECT building_[a_] FROM t",
            "context": "CREATE TABLE t (building_ VARCHAR, a_ VARCHAR)",
            "status_history": ["validated"], "hardness": "easy", "spider_hardness": "easy",
            "extended_hardness": "easy", "original_headers": ["building_[a_]"]}


def candidate(confirmation=False):
    return {"status": "candidate", "answer": "SELECT `building_[a_]` FROM t",
            "context": "CREATE TABLE t (`building_[a_]` VARCHAR(255))",
            "changes": [{"original": "building_[a_]", "replacement": "`building_[a_]`",
                         "evidence": "Original header supplied"}],
            "requires_source_confirmation": confirmation}


class CorrectionTests(unittest.TestCase):
    def test_applied_candidate_preserves_originals_and_invalidates_hardness(self):
        row = broken_row()
        original = deepcopy(row)
        self.assertEqual(apply_correction(row, candidate()), "corrected")
        self.assertEqual(row["original_answer"], original["answer"])
        self.assertEqual(row["original_context"], original["context"])
        self.assertEqual(row["status_history"], ["validated", "correction_corrected"])
        self.assertNotIn("repair_history", row)
        self.assertNotIn("repair_complete", row)
        self.assertEqual(row["schema_status"], "valid")
        self.assertNotIn("spider_hardness", row)
        self.assertNotIn("extended_hardness", row)
        self.assertEqual(row["correction_history"][0]["response"]["changes"], candidate()["changes"])

    def test_pending_candidate_does_not_replace_original(self):
        for confirmation, supplied in ((True, True), (False, False)):
            row = broken_row()
            if not supplied:
                row.pop("original_headers")
            original = deepcopy(row)
            self.assertEqual(apply_correction(
                row, candidate(confirmation), require_source_confirmation=True), "pending")
            self.assertEqual(row["answer"], original["answer"])
            self.assertEqual(row["context"], original["context"])
            self.assertEqual(row["hardness"], original["hardness"])
            self.assertTrue(row["correction_history"][-1]["requires_source_confirmation"])

    def test_valid_candidate_without_source_is_applied_with_uncertainty(self):
        row = broken_row()
        row.pop("original_headers")
        self.assertEqual(apply_correction(row, candidate(True)), "corrected")
        self.assertEqual(row["answer"], candidate()["answer"])
        attempt = row["correction_history"][-1]
        self.assertTrue(attempt["requires_source_confirmation"])
        self.assertEqual(attempt["acceptance_policy"], "validated_candidate")

    def test_invalid_candidates_and_malformed_protocol_fail(self):
        bad_sql = candidate()
        bad_sql["answer"] = "SELECT missing FROM t"
        bad_ddl = candidate()
        bad_ddl["context"] = "CREATE TABLE t (`building_[a_]` VARCHAR)"
        for response in (bad_sql, bad_ddl, [], {"status": "candidate"},
                         {**candidate(), "requires_source_confirmation": "false"},
                         {**candidate(), "changes": []}):
            row = broken_row()
            original = deepcopy(row)
            self.assertEqual(apply_correction(row, response), "failed")
            self.assertEqual(row["answer"], original["answer"])
            self.assertIn("error", row["correction_history"][-1])

    def test_ambiguous_and_unchanged_are_distinct(self):
        row = broken_row()
        self.assertEqual(apply_correction(row, {"status": "ambiguous", "reason": "No source",
                                               "required_evidence": ["original table"]}), "ambiguous")
        response = {"status": "unchanged", "answer": row["answer"],
                    "context": row["context"], "changes": []}
        self.assertEqual(apply_correction(row, response), "failed")
        row = {"answer": "SELECT a FROM t", "context": "CREATE TABLE t (a INT)"}
        self.assertEqual(apply_correction(row, {**row, "status": "unchanged", "changes": []}), "unchanged")

    def test_payload_revalidates_and_includes_source_evidence(self):
        row = broken_row()
        row["syntax_status"] = "valid"
        row["sample_rows"] = [{"building_[a_]": "Tower"}]
        payload = correction_payload(row)
        self.assertTrue(payload["validation_errors"]["syntax_errors"])
        self.assertTrue(payload["validation_errors"]["ddl_syntax_errors"])
        self.assertEqual(payload["sample_rows"], row["sample_rows"])
        self.assertEqual(payload["original_headers"], row["original_headers"])
