# SQL tools

Authors: Thomas Bayer and OpenAI Codex (AI coding assistant).

Database-free SQL analysis for `sql-create-context-copy` and other SQL datasets:

- Parse one statement without resolving tables or columns.
- Syntax-check supplied DDL independently of the optional schema comparison.
- Optionally resolve schema, table, and column references against the row's DDL.
- Classify supported queries as `easy`, `medium`, `hard`, or `extra` using
  Spider's original hardness rules.
- Keep unsupported constructs and parse failures visible instead of assigning
  misleading difficulty labels.

No SQL statements are executed. The package neither imports SQLite nor opens
database connections. SQLGlot is pinned in `pyproject.toml` and `uv.lock` because
parser and optimizer changes can affect results.

## Python and notebook usage

```python
from sql_tools import classify_sql, is_valid_sql

sql_statement = "SELECT name FROM users WHERE active = 1"
ddl_statement = "CREATE TABLE users (name VARCHAR, active INTEGER)"

assert is_valid_sql(sql_statement, dialect="mysql")

result = classify_sql(
    sql_statement,
    dialect="mysql",
    ddl_statement=ddl_statement,
    validate_schema=True,
)
print(result.spider_hardness)    # easy
print(result.extended_hardness)  # easy
print(result.schema_status)  # valid
print(result.to_dict())
```

`validate_sql` and `classify_sql` default to `validate_schema=True`;
`classify_records` and the CLI make schema checking opt-in. A schema failure
does not change hardness. Pass `validate_schema=False` for syntax checks only.
Whenever `ddl_statement` is not `None`, its syntax is checked, including when
`validate_schema=False` or the query fails to parse. Empty or comment-only DDL
is invalid; `None` leaves `ddl_syntax_status="not_checked"`.
With `validate_schema=True`, invalid or unsupported DDL skips the schema
comparison and sets `schema_status="unavailable"`. SQL classification continues.

```python
result = classify_sql(
    "SELECT name FROM users",
    ddl_statement="CREATE TABLE users (",
    validate_schema=False,
)
print(result.ddl_syntax_status)  # invalid
print(result.ddl_syntax_errors)
print(result.schema_status)     # not_checked
```

`dialect=None` explicitly means SQLGlot's generic SQL dialect, not automatic
dialect detection. The raw context-copy file stores many string literals in
double quotes (`Status <> "Village"`). Generic and PostgreSQL read `"..."` as
identifiers, so schema checks report a missing column. MySQL without
`ANSI_QUOTES` reads those quotes as strings.

`sql_tools.rewrite_sql_string_quotes` rewrites paired double quotes in `answer`
to single quotes and doubles an apostrophe inside a literal
(`"James O'Connor"` becomes `'James O''Connor'`). `question` and `context` are
copied unchanged. A double-quoted name directly after `FROM` stays, because
three rows use `FROM "t"` as a table identifier. The rewritten file is
`data/sql/sql_context_copy_single_quoted.json`: 78,577 rows, 62,967 statements
changed, and 503 apostrophes doubled. Single-quoted literals are strings in
the generic dialect as well, so that file no longer needs MySQL just to avoid
the false column errors. Counts from the full runs are in
[Single-quoted context-copy runs](#single-quoted-context-copy-runs).

The notebook kernel must use the environment containing this package and its
dependencies. From the project root, this project's existing `venv314` can be
synchronized with `UV_PROJECT_ENVIRONMENT=venv314 uv sync`. If the project is not
installed, set `PYTHONPATH=src` when starting Python from the project root.

## Process the full dataset

For validation without Spider classification:

```python
from sql_tools import validate_sql

validation = validate_sql(
        sql_statement,
        ddl_statement=ddl_statement,
        dialect="mysql",
        validate_schema=True,
        )
print(validation.to_dict())
```

`validation.py` owns SQL/DDL syntax and schema validation and returns a
`ValidationResult`. `classifier.py` owns `classify_sql` and returns a
`ClassificationResult` extending it with Spider hardness fields. Classification
reuses the validation parser's AST instead of parsing SQL twice. The public
result dictionaries contain statuses and diagnostics, not mutable syntax trees.
`sql_tools.dataset_validation.check_sql_statements` uses only `validate_sql` for
filtering, logging, and statistics; queries unsupported by Spider can still pass
validation. Disabled schema checks remain `not_checked` and do not reject rows.

```python
from datasets import load_from_disk
from sql_tools import classify_records

dataset = load_from_disk("data/sql_context_copy")
results = classify_records(
    dataset["train"],
    dialect="mysql",
    validate_schema=True,
)
for result in results:
    # Consume or write each result without accumulating the whole dataset.
    print(result["row_index"], result["spider_hardness"], result["extended_hardness"])
```

Defaults use `answer` for SQL and `context` for DDL. Override `sql_column` and
`ddl_column` for other formats. Each row yields one result, in input order;
the input records are never modified. Missing SQL columns raise configuration
errors; missing DDL is reported per row when schema validation is enabled.

For streaming JSONL output, including split names and row indices:

```bash
PYTHONPATH=src venv314/bin/python -m sql_tools \
  data/sql_context_copy outputs/sql_context_classification.jsonl \
  --dialect mysql --validate-schema
```

The CLI processes every saved split unless `--split` is specified, prints progress
every 5,000 rows, and refuses to overwrite an existing output file. It preserves
partial output if interrupted. Row indices are zero-based within each split.

## Repair candidates and LLM batches

```python
from sql_tools import repair_statement, repair_sql_statements

# Local validation/application only; no LLM call.
repair_statement(row, {"answer": "SELECT COUNT(*) FROM t"})

# The batch function makes the LLM requests and applies each response.
repaired_rows, failed_rows = repair_sql_statements(
        rows,
        client=lms_client,
        system_message=system_message,
        max_rows=6,  # None processes all rows; zero processes none.
        logger=logger,
        )
```

Both functions update input row dictionaries in place. The batch returns
`(repaired_rows, failed_rows)` in input order within each list. Partitioning uses
the current attempt, including incomplete candidates in failed rows, even if a
previous attempt succeeded. Each attempt records `error_type`: `request_error`,
`response_error`, `model_declined`, or `validation_error`; successful attempts
record null. Request failures include `http_status` when the exception exposes
it (for example, 500). A connector that returns None instead of raising loses
that HTTP information and is recorded as a response error. Failed attempts
record `repair_complete=False` in history without invalidating the existing pair.
Nothing is automatically written to
disk. The client must provide the `rwu_llmconnector` `send_request` and
`clear_history` methods; each request is isolated from previous chat history.
Requests use JSON serialization rather than Python dictionary string formatting.

`validate_sql` and `classify_sql` accept an optional `status_history` list.
The result copies existing events and appends `"validated"` when all requested
validation checks pass, otherwise `"validation_failed"`. Supplied DDL must pass
syntax validation even when schema checking is disabled. Unsupported or
unavailable requested checks count as validation failures. This event does not
indicate successful hardness classification. Dataset validation automatically
retains and updates each row's history; `classify_records` preserves input events
in its results. Standalone callers can pass `status_history=row.get("status_history")`
and assign `row["status_history"] = result.status_history` after validation.
Internal repair validation does not add a separate row event: the repair attempt
appends only `"repaired"` or `"repair_failed"`.

### Dataset validation program

```bash
PYTHONPATH=src python -m sql_tools.run_sql_validation \
  --dataset-id data/sql_context_copy \
  --output data/sql_validation/new_run \
  --max-rows 1000
```

`--dataset-id` also accepts a Hugging Face dataset ID or a local JSON, JSONL,
CSV or Parquet file. Directories must be datasets saved with `save_to_disk`.
Omit `--max-rows` to process all rows across all splits. Zero processes no rows.
The CLI default for `--dialect` is generic. Calling `run_sql_validation`
without a dialect still uses MySQL.
The program checks SQL syntax, DDL syntax and schema references, preserving
status history. With `--output` it writes `correct_prompts.json`,
`incorrect_prompts.json`, and `summary.json`. Omit `--output` to log that
summary and write no files. Each failed row appears once, with all validation
diagnostics.
Existing result files are kept unless `--override` is set. Rows use `answer` for SQL and
`context` for DDL. The wrapper calls `validate_sql_statements`, which delegates
each row to `validate_sql`; it does
not convert chat formats. No repair or expansion requests are made.

Use `validate_sql_statements(rows, max_rows=None, dialect="mysql", logger=logger)`
directly for in-memory batches. It updates rows in place and returns
`(valid_rows, invalid_rows)`, preserving history and order within each list.
Invalid rows include unavailable or unsupported requested checks.
`validate_schema=False` disables schema checks.
Without a dialect override, each row's `dialect` is used, falling back to
generic SQL. The result stores that dialect and drops a stale `sql_dialect`.
INFO reports totals and DEBUG reports individual results.

### MySQL validation guards and unclassified-corpus audit

Parser acceptance alone is not proof of valid MySQL. In addition to AST
completeness and schema name resolution, explicit `dialect="mysql"` applies
these checks to statements and embedded queries in DDL:

- Reject bracket subscripts such as `amount_issued_[€]`. Backtick-quoted names
  and bracket characters inside strings or JSON paths remain valid syntax.
- Require LIMIT in the same query block as OFFSET. LIMIT in an enclosing
  query does not authorize OFFSET in a child query.
- Require nonnegative integer literals for numeric LIMIT/OFFSET arguments;
  reject decimal, negative, string, or computed arguments. Prepared-statement
  `?` markers and bare routine parameter names remain accepted; their integer
  types and runtime values are not checked by this static validator.
- Require explicit lengths in VARCHAR, NVARCHAR and VARBINARY column
  declarations. Bare CHAR, TEXT and integer types are not rejected by this rule.
  No default VARCHAR length is inserted automatically.

These rules follow the MySQL documentation for
[SELECT and LIMIT](https://dev.mysql.com/doc/refman/8.4/en/select.html) and
[string column types](https://dev.mysql.com/doc/refman/8.4/en/string-type-syntax.html).
They do not apply to other dialects. The checker is still not a complete MySQL
engine validator: SQL modes, version-specific reserved words, runtime types,
permissions and the intended meaning of a natural-language question are not
fully checked. Double-quoted text is treated as a string, not an identifier (the default behavior without ANSI_QUOTES).

The 2026-10-12 audit of `test_rows_classified/unclassified_rows.json` found:

| Query group                          | Rows | Interpretation                                           |
|--------------------------------------|-----:|----------------------------------------------------------|
| Bracket subscripts                   |   34 | Invalid MySQL query syntax                               |
| OFFSET without LIMIT                 |   36 | Invalid MySQL query syntax                               |
| Other expressions rejected by Spider |   44 | Legal expressions; potentially damaged dataset semantics |

79 of the 114 DDLs also contain missing VARCHAR lengths. Query and DDL errors
overlap: 113 rows fail at least one validation check. The remaining query,
`SELECT MAX(inflation_index__2000) = 100 AS _ FROM table_name_34 WHERE year < 1980`,
passes these static checks but uses a projection unsupported by Spider.

Examples such as `SELECT 2007`, `SELECT "player"`, constant predicates, and
bitwise `a & b` are not automatically invalid SQL or unknown-column errors.
They may reflect lost identifier quoting, but recovering the intended columns
requires source evidence. Missing column references only become schema errors
when the parsed expression actually refers to a column absent from the DDL.

Per-row diagnostics are saved separately in
`data/sql_validation/test_rows_classified/unclassified_rows_validation_audit.json`.
The source file is unchanged. Rerun validation to refresh previous `validated`
results; classification deliberately preserves existing validation fields.
Validation progress logging does not consume iterators before processing.

### Single-quoted context-copy runs

Both runs below use `data/sql/sql_context_copy_single_quoted.json` (78,577 rows)
on 2026-10-14. A row is valid only when SQL syntax, DDL syntax, and schema
resolution all pass. Invalid DDL sets schema to `unavailable` and skips name
resolution.

| Check                  | generic |  mysql |
|------------------------|--------:|-------:|
| SQL syntax valid       |  78,390 | 77,677 |
| SQL syntax invalid     |     187 |    900 |
| DDL syntax valid       |  78,577 |  1,720 |
| DDL syntax invalid     |       0 | 76,857 |
| Schema valid           |  78,036 |  1,677 |
| Schema invalid         |     191 |      1 |
| Schema unavailable     |     163 | 75,999 |
| Schema not checked     |     187 |    900 |
| Rows valid             |  78,036 |  1,677 |
| Rows invalid           |     541 | 76,900 |

Generic accepts bare `VARCHAR`, so DDL is valid for every row. 78,036 rows
(99.3%) pass. The 541 failures are 187 syntax errors, mostly aggregates written
as `COUNT(1 AS name)`, 191 unresolved columns, and 163 rows whose DDL is missing
or duplicated. Output: `data/sql/sql_validation_results_generic`.

MySQL rejects bare `VARCHAR`, `NVARCHAR`, and `VARBINARY`. That length rule
accounts for 76,843 of the 76,857 DDL failures, and those rows never reach
schema resolution. 1,677 rows (2.1%) pass. One row fails schema resolution
after a valid DDL. The 900 SQL syntax failures include 36 queries that use
`OFFSET` without `LIMIT` in the same block. Output:
`data/sql/sql_validation_results_mysql`.

### Spider hardness runner

For Spider hardness classification, use the same dataset inputs as the
validation runner (Hub ID, saved dataset, JSON/JSONL, CSV, or Parquet):

```bash
PYTHONPATH=src python -m sql_tools.run_spider_hardness \
  --dataset-id data/sql_validation/correct_rows.json \
  --output data/sql_hardness --dialect mysql --max-rows 100
```

SQL comes from `answer`. The runner preserves original fields and validation
history, appending `classified`, `unsupported`, or `not_classified` when
`status_history` is present. Every output row includes `spider_hardness` and
`extended_hardness`. It also adds classification status/errors and component counts,
and records the split and row index in `hardness_source`. It writes
`classified_rows.json`, `unclassified_rows.json`, and `summary.json`. Existing
result files are replaced. `--no-override` refuses when any of them already
exist. Unsupported or invalid SQL gets no hardness
label. No LLM calls or database access occur. Omit `--max-rows` for all rows;
zero processes none, matching the validation runner.
Add `--dry-run` (or `--dry_run`) to classify at most 20 rows and log the summary
without creating result files or directories. A smaller `--max-rows` is honored;
existing output files remain untouched. Dataset loading may still use its cache.

### Identifier correction with an LLM

`run_repair_sql_statements.py` uses the identifier-reconstruction prompt in
`correct.DEFAULT_SYSTEM_MESSAGE`. It is separate from DDL expansion and the
general SQL repair API because it implements the candidate/unchanged/ambiguous
protocol and records source-confirmation requirements. Both workflows reuse
`repair.request_repair` for isolated LLM calls, `repair.validate_repair_candidate`
for SQL/DDL validation without mutation, and `repair.apply_repair_candidate`
for preserving originals, updating the pair and clearing stale classifications.
`repair_sql_statements` still retains incomplete candidates for further repair;
the correction runner applies only validated candidates that pass its source
confirmation policy. Their prompts, result protocols and histories remain distinct.

```bash
PYTHONPATH=src python -m sql_tools.run_repair_sql_statements \
  --input data/sql_validation/test_rows_classified/unclassified_rows.json \
  --output data/sql_validation/corrected_identifiers --dry-run --max-rows 5

PYTHONPATH=src python -m sql_tools.run_repair_sql_statements \
  --input data/sql_validation/test_rows_classified/unclassified_rows.json \
  --output data/sql_validation/corrected_identifiers \
  --batch-size 4 --checkpoints --checkpoint-size 100
```

The input formats and flags follow the expansion runner: JSON/JSONL, saved HF
datasets or a Hub ID; `--input`, `--output`, `--openai-format`, `--dry-run`,
`--local`, `--batch-size`, `--max-rows`, `--context-length`, `--checkpoints`, and
`--checkpoint-size` (hyphenated options also accept underscore aliases).
`--include-history` defaults to true and writes each row unchanged, including
`correction_history`. `--no-include-history` omits only that field from result
and checkpoint files.
Direct execution of `run_repair_sql_statements.py` also works.
`scripts/run_repair_sql_statements.sh` accepts the same run shape as the
expansion launcher: dry-runs, `--local`, and `--foreground` stay in this
process, and a remote full run starts with `nohup`. Full runs enable checkpoints. The default input
is `data/sql/sql_validation_results_generic/incorrect_prompts.json`; each run
gets its own `data/sql/correction_<timestamp>_<pid>` directory. `--input` and
`--output` are mandatory; `CORRECTION_INPUT` and `CORRECTION_OUTPUT` may
supply them instead.
The default remote model is `openai/gpt-oss-120b`, local is the expansion
runner's `MODEL` (20B); `--model` overrides either. The same LM Studio URL/key
environment variables and default context length (32768) are used. This Python
runner itself does not detach into the background.

Every input row is submitted, even if old validation flags say `valid`:
identifier corruption can form legal SQL. Fresh MySQL syntax/DDL/schema
diagnostics, the question, and optional `original_headers` / `sample_rows` are
included. No database execution occurs. The prompt includes a worked split-header
example and warns that DDL fragments may originate from the same corrupted SQL.
It preserves complete reconstructed names and prohibits changing aggregates or
adding DISTINCT. Bare VARCHAR is normalized to VARCHAR(255) as an explicit repair
convention; existing lengths are preserved and changes must be documented.
Missing source data alone is not a reason to return ambiguous: the model should
propose a coherent best-effort candidate and record its source uncertainty.
A column the query already names, but the DDL omits, may be added to the one
table the question supports. The type is chosen to fit: INTEGER for a numeric
magnitude such as an invoice total, VARCHAR(255) for text. The type choice is
documented and marked as requiring source confirmation.
Materially competing interpretations still require an ambiguous response.
These prompt instructions do not independently prove semantic fidelity.

Results are separated into:

| File                             | Meaning                                                                                                 |
|----------------------------------|---------------------------------------------------------------------------------------------------------|
| `correction_corrected_rows.json` | Candidate passed SQL syntax, DDL syntax and schema validation and was applied |
| `correction_pending_rows.json`   | Strict mode only: validated candidate lacks source confirmation; original SQL/DDL retained |
| `correction_ambiguous_rows.json` | Model cannot determine a repair; reason and required evidence retained                                  |
| `correction_unchanged_rows.json` | Model returned exactly the original pair and it passes validation                                       |
| `correction_failed_rows.json`    | Invalid input, request/response error, invalid candidate, or exceeded context budget                    |
| `correction_stats.json`          | Outcome counts, model, elapsed time, and seconds per row                                                |

Each output row retains input metadata and receives `correction_source`,
`correction_status`, and, unless `--no-include-history` is set, a
`correction_history` entry. The entry records the
original pair, model response/evidence and candidate validation when available.
Pending candidates are available under `correction_history[-1].response`;
their validation does not replace validation fields belonging to the input.
`status_history` gets `correction_corrected`, `correction_pending`,
`correction_ambiguous`, `correction_unchanged`, or `correction_failed`.
Applied corrections preserve `original_answer` / `original_context`, refresh
validation fields, clear stale hardness fields and synchronize OpenAI system
and assistant messages. Existing question and user messages are preserved.

Passing static validation is not proof of semantic equivalence. The model's
source-grounding judgment is not independently verified. By default, validated
candidates are applied even without source confirmation. Each candidate attempt
records `requires_source_confirmation` and `acceptance_policy` in its history;
`corrected` means locally validated and applied, not independently source-proven.
Use `--require-source-confirmation` to restore strict review: supplied evidence
and the model's `requires_source_confirmation=false` are then required for
application, otherwise the candidate stays pending. The policy is also recorded
in run statistics. The original input file is never modified.

Dry-run makes real LLM requests for at most 10 rows, logs attempts and statistics,
and writes no result files. `--max-rows 0` means all; batch mode rounds up to a
full requested batch, while the dry-run cap remains strict. Batches use IDs to
match results and may split further using a character-based context estimate (not an exact token count). Calls run
sequentially and histories are cleared
before and after each request. There are no automatic retries.

Checkpoints use one `correction_<outcome>_rows.tmp.jsonl` file per outcome.
The interval rounds up to full batches. Completed checkpoints survive an
interruption and are removed only after all final files are written and synced.
Work since the last checkpoint can be lost; automatic resume is not supported.
A repeat run replaces every result and checkpoint file in the output directory.
`--no-override` refuses when any of them already exist. Log files from an
earlier background start are left in place.

### DDL expansion

Start expansion with the Bash launcher from the project root:

```bash
./scripts/run_expand_sql_statements.sh --dry-run --batch-size 4
./scripts/run_expand_sql_statements.sh --local --batch-size 4
./scripts/run_expand_sql_statements.sh --batch-size 4
```

Dry-runs, `--local`, and `--foreground` stay in this process. Remote full runs
use `nohup` in the background and survive closing the shell. The launcher prints
the PID and log path; `tail -f` follows progress and `kill <PID>` stops the run.
Full runs enable checkpoints. The default input is
`data/sql_validation/correct_rows.json`; each run gets its own
`data/sql_validation/expansion_<timestamp>_<pid>` output directory.
Dry-runs create no output files. Batch size defaults to zero.
Use `--max-rows 100` to limit processing; the default `0` processes all rows.
Batch mode rounds the limit up to a full batch; the dry-run cap still applies.
Override paths with `EXPANSION_INPUT` and `EXPANSION_OUTPUT` (relative to the
project root). Set `EXPANSION_PYTHON` to an interpreter's full path if needed;
otherwise the active or an existing project virtual environment is used.
LM Studio URL and token environment variables are inherited unchanged.

Enable `--checkpoints --checkpoint-size 100` to append intermediate results to
`expanded_sql_rows.tmp.jsonl` and `expansion_failed_sql_statements.tmp.jsonl`.
The interval counts successful and failed input rows together and rounds up to
a multiple of batch size (100 with batch size 8 becomes 104). Each checkpoint
flushes and fsyncs both files; the final remainder is also saved. On interruption,
completed checkpoints remain. Final JSON files are flushed and synced before
temporary files are deleted. Dry-run never writes checkpoints. Existing checkpoint
files block a new run unless `--override` is set; automatic resume is not implemented.
Rows include `expansion_source` with the input identifier and zero-based row
position across splits. Work since the last completed checkpoint can be lost.

Run the LM Studio expansion program with:

```bash
PYTHONPATH=src python -m sql_tools.run_expand_sql_statements \
  --input data/sql_validation/correct_prompts.json \
  --output data/sql_validation/expanded --dry_run
```

Remove `--dry_run` for the full run. Dry-run processes at most ten rows with real
LLM requests. `--batch_size 8` sends up to eight examples per request; the default
zero keeps individual requests. Batch requests use context length 32768 and
estimate output tokens as JSON-encoded DDL characters / 3 plus 512 per row.
This is a heuristic, not a tokenizer guarantee. Oversized batches are split;
oversized single rows are recorded as failures. Responses are matched by integer
ID and validated individually. Missing/duplicate IDs fail the affected rows;
unknown IDs or malformed batch JSON fail the batch. No automatic retries occur.
Dry-run makes real
LLM requests but writes no files. `--openai_format` enables single-example
`messages`/`text` chat rows (default false); successful output updates the system
message as well as `context`. Input may also be a saved HF dataset directory or
Hub ID. The model constant is `openai/gpt-oss-20b`, matching the notebook's local
client. Set `LMSTUDIO_BASE_URL` and `LMSTUDIO_API_KEY` as needed; the default URL
is `http://localhost:1234`. Requests use context length 8192.

The shared prompt is `expand.DEFAULT_SYSTEM_MESSAGE`. Output files are
`expanded_sql_rows.json`, `expansion_failed_sql_statements.json`, and
`expansion_stats.json`. Existing files are kept unless `--override` is set. Successful attempts
append `extended` (the existing pipeline event), failures append
`extension_failed`. Runner statistics include counts, elapsed time and seconds
per row. The batch function logs token totals returned by the connector; the
runner does not instrument the client or perform throughput measurements.

```python
from sql_tools import expand_sql_statements

expanded_rows, failed_rows = expand_sql_statements(
    rows, client, system_message, probability=0.25, logger=logger,
)
```

The caller supplies the DDL expansion prompt. Responses must contain `context`;
an optional `answer` must equal the original query. `expand_statement` applies
decoded candidates without calling the LLM. Only changed, schema-valid DDL is
accepted. Rejected candidates leave the original pair intact. Structural
validation does not guarantee preservation of query meaning or enforce every
enrichment rule in the prompt.

Omit `probability` to select every row; otherwise it must be strictly between
zero and one. Selection is independent per row, not an exact sample size.
`max_rows` limits visited rows, including skipped rows. The function returns
`(expanded_rows, failed_rows)` in input order within each list and updates their
dictionaries in place. Randomly skipped rows are omitted from both lists and
remain unchanged in the caller's input apart from history initialization. Skipped rows
have no new event; attempts append `extended` or `extension_failed` to
`status_history`. Details are stored in `extension_history`. INFO logs report
counts, percentages and token/runtime totals; DEBUG logs report individual rows.

`repair_statement` accepts a decoded response object. Missing `context` preserves
the original DDL; explicit null/empty DDL is rejected. It validates the candidate
SQL/DDL pair using `validate_sql`, updates validation diagnostics and
`dialect`, and removes stale hardness/classification fields when a candidate
is applied. Repair does not compute Spider hardness; call `classify_sql` separately
after successful validation. Original SQL and DDL are retained as `original_answer` and
`original_context`; `repair_history` records candidate details. Each processed row
gets a `status_history` list if missing. Each attempt appends exactly one event:
`"repaired"` for a schema-valid candidate, otherwise `"repair_failed"` (including
request failures and unusable responses). Existing events are preserved, for
example `["extended", "repaired"]`. New attempts do not store `previous_statuses`;
before/after validation comparisons are used only for batch statistics.
Partial candidates are kept with
`repair_complete=False`; schema-valid candidates set it to True. This measures
structural validation, not semantic equivalence to the question.

Malformed responses, explicit `FAILURE`, and request errors leave the existing
SQL/DDL pair intact and record a failed attempt. A failed new request does not
invalidate a previously validated pair. Unexpected local validation exceptions
propagate rather than being mislabeled as JSON errors. Batch totals separate
complete, incomplete, and failed attempts. Request runtime uses the fifth
connector return value; the fourth is the reasoning-token count. Elapsed wall
time is tracked separately, and empty batches have defined zero averages.

The default logger is created with `get_console_logger("SQL Repair")`. INFO
reports the input/selected statement count, overall results, and separate DML,
DDL, and schema totals: repaired, already valid, still invalid, regressed,
unavailable, unsupported, not checked, and no usable candidate. Counts include
thousands separators and percentages use all processed statements as denominator.
Each category sums to the processed row count; no-candidate failures are not
counted as new validation results. The same counters are available through
`stats.validation_counts`. DEBUG logs individual inputs, successful repairs,
partial repairs, and failures. For iterators without a known length, the exact
statement count is reported at completion.

## Result interpretation

| Field                                                                          | Values / meaning                                        |
|--------------------------------------------------------------------------------|---------------------------------------------------------|
| `syntax_status`                                                                | `valid`, `invalid`, `unsupported`                       |
| `ddl_syntax_status`                                                            | `not_checked`, `valid`, `invalid`, `unsupported`        |
| `schema_status`                                                                | `not_checked`, `valid`, `invalid`, `unavailable`        |
| `classification_status`                                                        | `classified`, `unsupported`, `not_classified`           |
| `spider_hardness`                                                              | `easy`, `medium`, `hard`, `extra`, or `None`            |
| `extended_hardness`                                                            | `easy`, `medium`, `hard`, `extra`, `extreme`, or `None` |
| `syntax_errors`, `ddl_syntax_errors`, `schema_errors`, `classification_errors` | Separate diagnostic lists                               |
| `component1`, `component2`, `others`                                           | Original Spider feature counts, or `None`               |
| `dialect`                                                                      | Selected dialect, or `generic`                          |

`syntax_status="valid"` means acceptance by SQLGlot plus basic AST checks.
SQLGlot is deliberately permissive, not a complete database grammar validator.
This check catches parse errors, empty SELECT lists, and multiple statements;
it does not prove engine acceptance or validate function existence or types.
Opaque SQLGlot command fallbacks are `unsupported`, not accepted as valid SQL.
`is_valid_sql` returns False for unsupported commands; use `classify_sql` for
the detailed distinction. Incorrect dialect configuration raises immediately.

DDL validation supports explicit `CREATE TABLE` definitions, including multiple
statements, schema/catalog-qualified tables, quoted identifiers, aliases, CTEs,
derived tables, and correlated query scopes. DDL is assumed complete for each
row. Unqualified table names must have a unique match. Missing or unsupported DDL,
duplicate definitions, or DDL without explicit columns produces `unavailable`.
There is no ALTER/CREATE VIEW interpreter, data type checker, or function catalog.
Up to 2,048 DDL/dialect combinations are cached as immutable metadata.
The independent DDL syntax check also caches up to 2,048 immutable results. It
accepts multiple DDL statements and checks basic CREATE TABLE completeness.
It never resolves references: CREATE TABLE AS/LIKE or ALTER/DROP may pass syntax
checking even though they cannot supply schema metadata for the comparison.
Duplicate table/column definitions are metadata errors, not syntax errors.
As with query syntax, DDL syntax acceptance is not a complete engine guarantee.
For MySQL, column names and column aliases are matched case-insensitively;
table/schema names and table aliases retain SQLGlot's case-sensitive policy (comparable to MySQL on Unix with
`lower_case_table_names=0`). See the
[MySQL identifier rules](https://dev.mysql.com/doc/refman/8.4/en/identifier-case-sensitivity.html).

## Spider provenance and compatibility

Source: [Spider](https://github.com/taoyds/spider),
[evaluation.py at b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c](https://github.com/taoyds/spider/blob/b7b5b8c890cd30e35427348bb9eb8c6d1350ca7c/evaluation.py).

`spider_hardness.py` contains the original `Evaluator.eval_hardness` method and
its constants/helper functions with their bodies unchanged. Only the hardness
subset was extracted; matching, database access, CLI code, and NLTK dependencies
were removed. Attribution: Copyright 2024 XLANG NLP Lab. The Apache-2.0 license
is included in [SPIDER_LICENSE](SPIDER_LICENSE).

Reference: Tao Yu et al. (2018),
[Spider: A Large-Scale Human-Labeled Dataset for Complex and Cross-Domain
Semantic Parsing and Text-to-SQL Task](https://aclanthology.org/D18-1425/).

`spider_adapter.py` is project-specific. It builds the original SQL dictionary
shape from SQLGlot's AST, without requiring a schema. Symbolic column identifiers
are sufficient for hardness, but this adapter is **not an exact-match evaluator**.
Original counter quirks are deliberately retained for compatibility with the
original hardness implementation.

Supported classification includes column projections, the six Spider aggregate
operations (including no aggregation), simple column arithmetic, joins, boolean
predicates, condition subqueries, derived tables in FROM/JOIN, grouping, HAVING, ordering, integer LIMIT, and
representable UNION/INTERSECT/EXCEPT queries.

CTEs, windows, table functions, arbitrary scalar functions/literal projections,
IN value lists, OFFSET, USING/NATURAL joins, mixed sort directions, ALL set
operations, and other unrepresentable constructs currently yield `unsupported`.
Schema validation can still succeed for such queries. They must not be silently
classified as `extra`: extending the mapping requires a documented rule.

The hardness rules are unchanged, but parsing is different. For example, the
original Spider parser can drop the second predicate in `a < b OR a > c` when
the right operands are columns. SQLGlot preserves both predicates; the feature
counts and resulting label can consequently differ. This package does not
reproduce original parser bugs to force matching labels.

### Spider hardness rules

`spider_hardness` is the original `Evaluator.eval_hardness` applied to the
adapted query. The label order is `easy < medium < hard < extra`. The stored
label is `spider_hardness`. `component1`, `component2`, and `others` are
the three counters below. Each counter looks only at the outermost adapted SQL
object. A nested query increments `component2` and does not add its own
WHERE, GROUP BY, aggregates, or joins to the outer counters. A query nested
inside that nested query is not counted again at the outer level.

`component1` adds:

- 1 when WHERE is non-empty
- 1 when GROUP BY is non-empty
- 1 when ORDER BY is non-empty
- 1 when LIMIT is present
- one less than the number of FROM/JOIN table units, when at least one unit is
  present (each join; a single table adds 0)
- 1 for each `OR` between FROM, WHERE, or HAVING predicates (`AND` adds nothing)
- 1 for each `LIKE` predicate in FROM, WHERE, or HAVING

`component2` is the number of nested SQL objects attached directly to this
object: a condition value in FROM, WHERE, or HAVING that is itself a query,
plus `INTERSECT`, `EXCEPT`, or `UNION` when that set operation is present.
Each of those adds one. A subquery that appears only as a FROM or JOIN table
unit is not a nested SQL object. Its internal clauses therefore do not affect
Spider hardness. A `COUNT` over a derived table can be `easy` even when the
derived table contains WHERE, GROUP BY, and HAVING.

`others` adds 1, not one point per item, for each of these:

- more than one aggregate among SELECT, WHERE, GROUP BY, ORDER BY, and HAVING
  (a single aggregate adds nothing)
- more than one selected column
- a WHERE list longer than one entry, which means more than one predicate
  (the list alternates predicates and `AND`/`OR`)
- more than one GROUP BY expression

The first matching row wins. Easy is tested before medium, so a query that
also fits a medium clause stays `easy`.

| Label | Condition |
|-------|-----------|
| easy | `component1 <= 1` and `others = 0` and `component2 = 0` |
| medium | (`others <= 2` and `component1 <= 1` and `component2 = 0`) or (`component1 <= 2` and `others < 2` and `component2 = 0`) |
| hard | (`others > 2` and `component1 <= 2` and `component2 = 0`) or (`2 < component1 <= 3` and `others <= 2` and `component2 = 0`) or (`component1 <= 1` and `others = 0` and `component2 <= 1`) |
| extra | every remaining adapted query |

### Extended hardness

`extended_hardness` is a project-specific structural label. It is not an
official Spider metric and it is not a measurement of human difficulty. It
never changes `spider_hardness` or the three counters when the official adapter
accepts the statement. The two labels are comparable only when both are set.
If that adapter rejects a parsed statement, `spider_hardness` stays null and
`extended_hardness` is still set: an expression Spider cannot store counts as
one ordinary column, and clauses it cannot store, such as `OFFSET`, are
ignored. Nesting rules then apply to that score. A statement that does not
parse under the requested dialect is tried once as MySQL, so backtick
identifiers still receive a label. A statement that parses in neither dialect
keeps both fields null.

The walk uses SQLGlot `SELECT` nodes. A nested SELECT is a `SELECT` that has
another `SELECT` as an ancestor. Depth is the number of those ancestors.
`UNION`, `INTERSECT`, and `EXCEPT` branches are sibling `SELECT` nodes under
the set operation. Neither branch is an ancestor of the other, so the set
operation alone adds no nesting and no depth. The same rules apply to
supported subqueries in conditions and in FROM/JOIN.

A nested SELECT has aggregation when that node itself has `GROUP BY` or
`HAVING`, or when an aggregate function's nearest `SELECT` ancestor is that
node. An aggregate, `GROUP BY`, or `HAVING` in a child `SELECT` does not count
for its parent. The outermost `SELECT` is not in the nested set, so its own
aggregation cannot raise the extended label.

A nested SELECT is correlated when one of its own columns has a table
qualifier that is absent from that SELECT's source names and equal to a
source name of an enclosing SELECT. A column belongs to a SELECT when that
SELECT is its nearest SELECT ancestor, so a column inside a deeper subquery
does not make the outer nested SELECT correlated. A source name is the alias
of a FROM or JOIN item of that SELECT, or the table name when the item has
no alias. A name introduced by the nested SELECT's own FROM or JOIN hides
the same name from every enclosing SELECT. An unqualified column is never
correlation. A reference that would be outer only after schema resolution is
not inferred.

Let N be the set of nested SELECTs. Apply the first matching rule:

1. If N is empty, `extended_hardness` is `spider_hardness`.
2. Otherwise assign `extreme` when any of the following is true:
    - some SELECT in N has depth 2 or more
    - N contains at least two SELECTs and at least one of them has aggregation
      (the aggregation and the second SELECT may be different blocks; two
      blocks without aggregation do not match; one block with aggregation
      does not match)
    - some one SELECT in N is both correlated and aggregated (correlation
      without aggregation does not match, and aggregation without correlation
      does not match)
    - some SELECT in N is Spider `extra` when scored on its own with
      `eval_hardness`. That score uses the same counters as the outer query
      and still ignores a FROM subquery inside that SELECT. Four joins in one
      derived table are `extra` and therefore `extreme`. Three joins are
      Spider `hard` and stay at the extended floor of `hard`
3. Otherwise `extended_hardness` is `hard`, unless `spider_hardness` is
   already `extra`, in which case it stays `extra`. This step does not raise
   `hard` to `extra` and it does not lower `extra`.

| SQL | Spider | Extended | Why the extended label differs, or does not |
|-----|--------|----------|-----------------------------------------------|
| `SELECT a FROM t` | easy | easy | No nested SELECT |
| `SELECT COUNT(*) FROM (SELECT * FROM endowment WHERE amount > 8.5 GROUP BY school_id HAVING COUNT(*) > 1)` | easy | hard | The derived table is invisible to Spider. One nested SELECT has aggregation, depth 1, and no correlation, so the floor is `hard` |
| `SELECT a FROM (SELECT a FROM (SELECT a FROM t) x) y` | easy | extreme | The innermost SELECT has depth 2 |
| `SELECT a FROM t WHERE a IN (SELECT b FROM u)` | hard | hard | `component2 = 1` is already Spider `hard`. One nested SELECT has no aggregation |
| `SELECT a, b FROM t WHERE a IN (SELECT b FROM u)` | extra | extra | Two selected columns plus one nested query is Spider `extra`. The floor keeps `extra` |
| `SELECT a FROM t WHERE a > (SELECT AVG(b) FROM u WHERE u.id = t.id)` | hard | extreme | The same nested SELECT is correlated and aggregated |
| `SELECT a FROM t WHERE a > (SELECT AVG(b) FROM u AS t WHERE t.id = 1)` | hard | hard | The inner alias `t` hides the outer table, so `t.id` is not correlation |
| `SELECT a FROM t WHERE a IN (SELECT b FROM u) AND a > (SELECT AVG(c) FROM v)` | extra | extreme | Two nested SELECTs, and one of them has aggregation |
| `SELECT a FROM t UNION SELECT b FROM u` | hard | hard | The branches are siblings, so N is empty |
| `SELECT a FROM (SELECT t.a AS a FROM t JOIN u ON t.id = u.id JOIN v ON t.id = v.id JOIN w ON t.id = w.id) x` | easy | hard | Three inner joins are Spider `hard`, not `extra`, so the floor stays `hard` |
| `SELECT a FROM (SELECT t.a AS a FROM t JOIN u ON t.id = u.id JOIN v ON t.id = v.id JOIN w ON t.id = w.id JOIN z ON t.id = z.id) x` | easy | extreme | Four inner joins are Spider `extra` on that nested SELECT |

The runner summary reports both distributions. Existing result files are not
migrated; rerun classification to apply a changed rule.

## Validation

Run the behavioral tests from the project root:

```bash
PYTHONPATH=src venv314/bin/python -m unittest discover -s src/sql_tools/tests -v
```

A full local validation before the independent DDL syntax check was added used
SQLGlot 30.21.0 on 2026-10-01, explicit `mysql`, and
DDL checking on 78,577 context-copy rows:

| Classification |   Rows |
|----------------|-------:|
| easy           | 50,962 |
| medium         | 22,629 |
| hard           |    914 |
| extra          |    695 |
| unsupported    |  3,184 |
| parse failure  |    193 |

DDL statuses: 78,027 valid, 192 invalid, 165 unavailable, and 193 not checked
because parsing failed. These are diagnostics under the selected parser/dialect,
not claims that every rejected row is invalid in every SQL dialect.

A comparison on the first 4,500 rows against Spider's original parser/evaluator
produced 3,993 matching labels, one difference caused by the dropped OR predicate
described above, 493 original-parser failures, and 13 adapter-unsupported rows.
The comparison used NLTK's `preserve_line=True` tokenizer option to avoid the
sentence-tokenizer resource dependency; it did not use SQLite.
