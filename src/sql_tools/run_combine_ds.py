"""Combine the expansion products into one JSON dataset.

Documentation of the merge after expansion. Expanded rows come first, then
unchanged rows, then rows whose expansion failed. Audit fields from repair
and expansion are removed. The SQL and DDL of each row stay as they are.
"""

import json
import logging
from pathlib import Path

if __package__ in (None, ""):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sql_tools.logging_utils import get_console_logger

# Paths are relative to the project root, independent of the working directory.
ROOT = Path(__file__).resolve().parents[2]
path_expanded = ROOT / "data/sql/expanded/expanded_sql_rows.json"
path_expansion_unchanged = ROOT / "data/sql/expanded/unchanged_sql_rows.json"
path_expansion_failed = ROOT / "data/sql/expanded/expansion_failed_sql_statements.json"
v2_rows_path = ROOT / "data/sql/sql_context_v2.json"

logger = get_console_logger("Combine Dataset", level=logging.INFO)

with path_expanded.open("r", encoding="utf-8") as file:
    expanded_rows = json.load(file)

with path_expansion_unchanged.open("r", encoding="utf-8") as file:
    unchanged_rows = json.load(file)
expanded_rows.extend(unchanged_rows)

with path_expansion_failed.open("r", encoding="utf-8") as file:
    expansion_failed_rows = json.load(file)
expanded_rows.extend(expansion_failed_rows)

for row in expanded_rows:
    for field in (
        "correction_source",
        "original_answer",
        "original_context",
        "correction_history",
        "expansion_source",
        "extension_history",
        "sql_dialect",
    ):
        row.pop(field, None)

with v2_rows_path.open("w", encoding="utf-8") as file:
    json.dump(expanded_rows, file, ensure_ascii=False, indent=2)
    file.write("\n")

logger.info(f"Rows: {len(expanded_rows):,}")
