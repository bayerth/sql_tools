"""Extend DDL through an LLM and validate it against the unchanged SQL query."""

from collections.abc import Iterable, Mapping
from itertools import islice
import json
import logging
import random
from time import perf_counter

from .validation import set_row_dialect, validate_sql
from sql_tools.logging_utils import get_console_logger

logger = get_console_logger("SQL Expansion", level=logging.INFO)

DEFAULT_SYSTEM_MESSAGE = r"""
You extend SQL table definitions for a text-to-SQL training dataset.

INPUT
A JSON object containing:
- question: the natural-language question
- answer: the SQL query
- context: one or more CREATE TABLE statements
- dialect: optional; default is mysql

TASK
Extend table definitions with plausible columns while preserving the
original question, query, table names, and existing column names.
A table that already has two or more columns can still gain a missing
companion column when an existing column clearly implies one.

RULES
1. If a table has fewer than two columns, add one or two plausible columns
   appropriate to the table, question, and surrounding schema.
   Count columns, not data rows. Do not generate INSERT statements.
2. Companion columns override the two-column limit. If one side of an
   obvious pair is present and the other is absent, append the missing
   side even when the table already has two or more columns.
   Match the existing abbreviation and data type.
   lat means latitude, so add long with lat's type; do not spell it
   longitude and do not use lng. Apply the same idea to other obvious
   pairs, such as first_name/last_name or start_date/end_date.
   Add only that missing partner. Do not invent unrelated columns.
3. Preserve existing identifiers, data types, constraints, and column order.
   Append all new columns after existing columns. Never rename tables or
   columns, even if their names contain spelling mistakes. The
   VARCHAR-length rule is the only permitted adjustment to an existing
   data type.
4. Prefer an existing identifier column. Do not add another id when a
   suitable identifier already exists.
5. If no identifier exists and adding one is appropriate, add:
   id INTEGER PRIMARY KEY AUTO_INCREMENT
   Use the equivalent syntax when another dialect is explicitly supplied.
6. Existing VARCHAR identifiers must remain VARCHAR and must not become
   AUTO_INCREMENT. Do not infer PRIMARY KEY, UNIQUE, or FOREIGN KEY
   constraints merely from column names or joins.
7. For MySQL, give VARCHAR columns without a length an explicit length
   of 255. Preserve any existing explicit length.
8. Write SQL keywords and data types in uppercase. Preserve identifier
   spelling and quote identifiers when required by the dialect.
9. Otherwise leave a table unchanged, except for keyword formatting and
   the VARCHAR-length rule. A table that already contains both sides of
   a pair, or that has no missing companion, gets no extra columns.
10. Keep question and answer unchanged. Added columns are synthetic schema
    enrichment, not recovered facts about the original database.
11. Check the entire query, including subqueries and CTEs, before extending
    the DDL. If any projection uses SELECT * or table.*, or the query uses
    NATURAL JOIN, return {"FAILURE": "Schema extension could change query semantics"}.
    Do not modify the query to bypass this restriction.
12. COUNT(*) and COUNT(1) count rows; they are not wildcard projections or
    references to the first table column. They are allowed. Positional
    ORDER BY/GROUP BY expressions refer to the SELECT list, not directly
    to the table's column order. Preserve that SELECT list unchanged.
13. Return valid DDL consistent with all table and column references in
    answer. If an existing inconsistency prevents this within these rules,
    return {"FAILURE": "reason"}.

OUTPUT
On success, return exactly one JSON object containing the complete DDL
in "context", including unchanged tables. If no table needs extension,
return the DDL with only the permitted formatting/type-length adjustments.
On failure, return exactly {"FAILURE": "specific reason"}.
Do not include Markdown or explanations outside the JSON object.

EXAMPLE 1
Input:
{
  "question": "How many heads of the departments are older than 56?",
  "answer": "SELECT COUNT(*) FROM head WHERE age > 56",
  "context": "CREATE TABLE head (age INTEGER)",
  "dialect": "mysql"
}

Output:
{
  "context": "CREATE TABLE head (age INTEGER, id INTEGER PRIMARY KEY AUTO_INCREMENT)"
}

EXAMPLE 2
Input:
{
  "question": "How many registered students does each course have? List the course name and the number of registered students.",
  "answer": "SELECT T3.course_name, COUNT(*) FROM students AS T1 JOIN student_course_registrations AS T2 ON T1.student_id = T2.student_id JOIN courses AS T3 ON T2.course_id = T3.course_id GROUP BY T2.course_id",
  "context": "CREATE TABLE students (student_id VARCHAR); CREATE TABLE courses (course_name VARCHAR, course_id VARCHAR); CREATE TABLE student_course_registrations (course_id VARCHAR, student_id VARCHAR)",
  "dialect": "mysql"
}

Output:
{
  "context": "CREATE TABLE students (student_id VARCHAR(255), name VARCHAR(255)); CREATE TABLE courses (course_name VARCHAR(255), course_id VARCHAR(255)); CREATE TABLE student_course_registrations (course_id VARCHAR(255), student_id VARCHAR(255))"
}

EXAMPLE 3
Input:
{
  "question": "What are the names of stations that have latitude lower than 37.5?",
  "answer": "SELECT name FROM station WHERE lat < 37.5",
  "context": "CREATE TABLE station (name VARCHAR, lat INTEGER)",
  "dialect": "mysql"
}

Output:
{
  "context": "CREATE TABLE station (name VARCHAR(255), lat INTEGER, long INTEGER)"
}
"""


class ExpansionResponseError(ValueError):
    """The model did not provide a usable DDL extension."""


def _record_failure(row, message):
    """Keep the current pair intact and record one unsuccessful attempt."""
    row["status_history"] = list(row.get("status_history") or []) + ["extension_failed"]
    row.setdefault("extension_history", []).append({"status": "extension_failed", "error": message})


def expand_statement(row: dict, result: Mapping, *, dialect: str | None = None) -> dict:
    """Validate a decoded extension and apply it in place, without an LLM call.

    Only a changed DDL that passes SQL, DDL, and schema validation is applied.
    An unchanged DDL is recorded as extension_unchanged and leaves context as it is.
    A candidate that fails validation is not applied. The SELECT must stay unchanged.
    """
    row["status_history"] = list(row.get("status_history") or [])
    set_row_dialect(row, dialect if dialect is not None else row.get("dialect"))
    try:
        if not isinstance(result, Mapping):
            raise ExpansionResponseError("Expected a JSON object")
        if "FAILURE" in result:
            raise ExpansionResponseError(f"Model declined extension: {result['FAILURE']}")
        ddl = result.get("context")
        if not isinstance(ddl, str) or not ddl.strip():
            raise ExpansionResponseError("Response must contain non-empty context")
        if "answer" in result and result["answer"] != row["answer"]:
            raise ExpansionResponseError("Extension must not change the SQL query")
        if ddl == row.get("context"):
            row.setdefault("extension_history", []).append(
                    {"status": "extension_unchanged", "context": ddl}
                    )
            row["status_history"].append("extension_unchanged")
            return row
    except ExpansionResponseError as error:
        _record_failure(row, str(error))
        raise

    selected_dialect = dialect if dialect is not None else row.get("dialect")
    validation = validate_sql(
            row["answer"], ddl_statement=ddl,
            dialect=None if selected_dialect == "generic" else selected_dialect,
            validate_schema=True
            )
    # validated means SQL syntax, DDL syntax, and schema all passed.
    success = validation.status_history[-1] == "validated"
    outcome = "extended" if success else "extension_failed"
    # Store candidate diagnostics separately: a rejected candidate must not replace
    # validation results belonging to the row's existing SQL/DDL pair.
    attempt = {
            "previous_context": row.get("context"), "context": ddl,
            "validation": validation.to_dict(), "status": outcome,
            }
    if not success:
        attempt["error"] = "Extended DDL failed validation"
    row.setdefault("extension_history", []).append(attempt)
    if success:
        row.setdefault("original_context", row.get("context"))
        row["context"] = ddl
        fields = validation.to_dict()
        # Older validation results (including cached notebook imports) may omit it.
        fields.pop("status_history", None)
        row.update(fields)
        set_row_dialect(row, validation.dialect)
    row["status_history"].append(outcome)
    return row


def expand_sql_statements(rows: Iterable[dict], client, system_message: str = DEFAULT_SYSTEM_MESSAGE, *,
                          probability: float | None = None,
                          max_rows: int | None = None, max_tokens: int = 2048,
                          temperature: float = 0.0, dialect: str | None = None,
                          context_length: int = 8192,
                          logger: logging.Logger = logger
                          ) -> tuple[list[dict], list[dict], list[dict], float]:
    """Return (expanded_rows, unchanged_rows, failed_rows, request_seconds).

    None probability selects every row; otherwise each row is independently
    selected with 0 < probability < 1. max_rows limits visited, not selected rows.
    Rows are mutated in place. Skipped rows receive no history event and are
    omitted from both result lists; they remain in the caller's input. Requests
    are isolated, and request/response failures do not stop subsequent rows.
    """
    if probability is not None and not 0 < probability < 1:
        raise ValueError("probability must be strictly between 0 and 1, or None")
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative or None")
    selected_rows = rows if max_rows is None else islice(rows, max_rows)
    output = []
    expanded_rows, unchanged_rows, failed_rows = [], [], []
    selected = extended = unchanged = failed = 0
    prompt_tokens = completion_tokens = reasoning_tokens = 0
    runtime = 0.0
    started = perf_counter()
    logger.info(f"Starting DDL expansion; selection probability: {1 if probability is None else probability:.2%}")
    for row in selected_rows:
        output.append(row)
        row["status_history"] = list(row.get("status_history") or [])
        if probability is not None and random.random() >= probability:
            logger.debug(f"Row {len(output):,}: skipped by random selection")
            continue
        selected += 1
        payload = {key: row[key] for key in ("question", "answer", "context", "dialect") if key in row}
        if dialect is not None:
            payload["dialect"] = dialect
        try:
            client.clear_history()
            try:
                text, pt, ct, rt, seconds = client.send_request(
                        json.dumps(payload, ensure_ascii=False), system_message=system_message,
                        max_tokens=max_tokens, temperature=temperature, ignore_history=True,
                        context_length=context_length,
                        logger=logger,
                        )
            finally:
                client.clear_history()
        except Exception as error:
            _record_failure(row, f"LLM request failed: {error}")
            failed += 1
            failed_rows.append(row)
            logger.error(f"Row {len(output):,}: extension request failed: {error}")
            continue
        prompt_tokens += pt or 0
        completion_tokens += ct or 0
        reasoning_tokens += rt or 0
        runtime += seconds or 0
        try:
            result = json.loads(text)
        except (TypeError, json.JSONDecodeError) as error:
            logger.error(f"Row {len(output):,}: Invalid response: {error}")
            _record_failure(row, f"Invalid response: {error}")
        else:
            try:
                expand_statement(row, result, dialect=dialect)
            except ExpansionResponseError:
                pass  # The single-row function already recorded the failed attempt.
        outcome = row["status_history"][-1]
        if outcome == "extended":
            extended += 1
            expanded_rows.append(row)
        elif outcome == "extension_unchanged":
            unchanged += 1
            unchanged_rows.append(row)
        else:
            failed += 1
            failed_rows.append(row)
        logger.debug(f"Row {len(output):,}: {row['extension_history'][-1]}")
    total = len(output)
    logger.info(
        f"Visited: {total:,}; selected: {selected:,} ({selected / (total or 1):.2%}); "
        f"skipped: {total - selected:,} ({(total - selected) / (total or 1):.2%})"
        )
    logger.info(
        f"Extended: {extended:,} ({extended / (selected or 1):.2%}); "
        f"unchanged: {unchanged:,} ({unchanged / (selected or 1):.2%}); "
        f"failed: {failed:,} ({failed / (selected or 1):.2%}) of selected rows"
        )
    logger.info(
        f"Request runtime: {runtime:,.2f}s; elapsed: {perf_counter() - started:,.2f}s; "
        f"prompt tokens: {prompt_tokens:,}; completion tokens: {completion_tokens:,}; "
        f"reasoning tokens: {reasoning_tokens:,}"
        )
    return expanded_rows, unchanged_rows, failed_rows, runtime
