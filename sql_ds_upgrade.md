# SQL dataset upgrade

The scripts for this upgrade were run in this order. Each step consumes the
previous step's output. Spider hardness is step 6, after the final SQL and DDL
are fixed. No step executes SQL against a database.

1. Validation.
2. Repair the invalid rows.
3. Merge the valid rows.
4. Expansion.
5. Merge the expansion rows, including the failed rows.
6. Spider classification.

String quotes are rewritten once, before step 1. That rewrite is not one of
the six script steps.

Run commands from the project root with the project virtual environment:

```bash
uv run python -m ...
```

A repeat run replaces existing result files. `--no-override` refuses when any
of them already exist.

## Before step 1: adjust string quotes

`sql_context_copy` stores string values in double quotes (`Status <> "Village"`).
On MySQL without `ANSI_QUOTES`, and on SQLite, those quotes are string literals
and do not raise a database error. The generic SQLGlot dialect reads them as
identifiers and reports a missing column. That false schema error dominated
validation of the raw copy.

`sql_tools.rewrite_sql_string_quotes` rewrites paired double quotes in `answer`
to single quotes. An apostrophe inside a literal is doubled
(`"James O'Connor"` becomes `'James O''Connor'`). `question` and `context` are
copied unchanged. A double-quoted name directly after `FROM` is kept, because
three rows use `FROM "t"` as a table identifier rather than a string.

```bash
uv run python -m sql_tools.rewrite_sql_string_quotes
```

Output: `data/sql/sql_context_copy_single_quoted.json` (78,577 rows). On this
copy, 62,967 statements change, apostrophes are doubled in 503 places, and the
three `FROM "t"` names stay quoted.

## 1. Validation

Validate the rewritten file before any model call. The check covers SQL syntax,
DDL syntax, and whether query names resolve against the row's DDL.

Use `generic` for this pass. The copied DDL uses bare `VARCHAR`. The MySQL
check requires an explicit length and then skips schema resolution, so a MySQL
run would reject almost every row before the quote fix can matter. Single-quoted
literals are strings in the generic dialect as well.

```bash
uv run python -m sql_tools.run_sql_validation \
  --dataset-id data/sql/sql_context_copy_single_quoted.json \
  --output data/sql_validation_results \
  --dialect generic
```

`scripts/run_sql_validation.sh` accepts the same arguments. Its default dialect
is `mysql`, so pass `--dialect generic` when using the script for this step.
Omit `--output` to log the summary only. No result directory or JSON files are
created. Later steps need the written files, so keep `--output` for this upgrade.

Output directory, when `--output` is set:

| File | Contents |
| --- | --- |
| `correct_prompts.json` | SQL, DDL, and schema checks all passed |
| `incorrect_prompts.json` | Any failed or incomplete check |
| `summary.json` | Row counts and status totals |

Real failures that remain include syntax such as `COUNT(1 AS st_prize__)`,
missing columns in the DDL, and duplicate DDL definitions. Expressions like
`purse__$__` are legal identifiers; they are not failures by themselves.

## 2. Repair the invalid rows

Send only `incorrect_prompts.json` to identifier correction. Rows that already
validated stay out of this step. The model repairs corrupted identifiers, for
example a column split into `COUNT(1 AS st_prize__) AS $__`, split headers, and
unquoted reserved words. It also normalizes bare `VARCHAR` to `VARCHAR(255)`
on rows it rewrites. The target dialect of this prompt is MySQL. The applied
repairs for this run are `data/sql/repaired/correction_corrected_rows.json`.

Start with a dry run, then run the full set:

```bash
uv run python -m sql_tools.run_repair_sql_statements \
  --input data/sql_validation_results/incorrect_prompts.json \
  --output data/sql_validation/corrected_identifiers \
  --dry-run --max-rows 5

uv run python -m sql_tools.run_repair_sql_statements \
  --input data/sql_validation_results/incorrect_prompts.json \
  --output data/sql_validation/corrected_identifiers
```

`--local` uses the local LM Studio model. Without it, the runner uses the
remote 120B model. A candidate is applied only when the repaired SQL and DDL
pass validation. The input file is not modified.

`scripts/run_repair_sql_statements.sh` accepts the same arguments. A remote
full run starts in the background. `--foreground` keeps that run in this
process. A repeat run replaces existing result and checkpoint files;
`--no-override` refuses instead. Its default input is
`data/sql/sql_validation_results_generic/incorrect_prompts.json`, and each run
writes a new `data/sql/correction_<timestamp>_<pid>` directory.

| File | Contents |
| --- | --- |
| `correction_corrected_rows.json` | Validated repair, applied |
| `correction_unchanged_rows.json` | Model kept the original pair and it validates |
| `correction_pending_rows.json` | Valid candidate held back when source confirmation is required |
| `correction_ambiguous_rows.json` | Model could not choose a repair |
| `correction_failed_rows.json` | Request error or candidate that still fails validation |
| `correction_stats.json` | Counts and timing |

Passing validation here means the statement is structurally consistent. It does
not prove that the repair preserved the question's meaning.

## 3. Merge the valid rows

Append the repaired rows to the rows that passed validation. Drop
`correction_source`, `original_answer`, `original_context`, and
`correction_history` from the combined list. This run writes
`data/sql/sql_context_copy_single_quoted_preprocessed.json`: 78,036 rows from
`data/sql/sql_validation_results_generic/correct_prompts.json` plus 541 rows
from `data/sql/repaired/correction_corrected_rows.json`, 78,577 rows in total.

## 4. Expansion

Expand DDL only after the statement is valid. Expansion adds plausible columns
and missing companions (for example `long` next to `lat`) and gives bare
`VARCHAR` an explicit length when it rewrites a table. It must not change the
SQL query. The input is the merged file from step 3, not the raw incorrect
file.

```bash
scripts/run_expand_sql_statements.sh \
  --input data/sql/sql_context_copy_single_quoted_preprocessed.json \
  --output data/sql/expanded \
  --dry-run --max-rows 10

scripts/run_expand_sql_statements.sh \
  --input data/sql/sql_context_copy_single_quoted_preprocessed.json \
  --output data/sql/expanded
```

A remote full run starts in the background with `nohup` and writes
`data/sql_validation/expansion_<timestamp>_<pid>/`. `--foreground`, `--local`,
and `--dry-run` stay in this process. Dry-run calls the model and writes no files.

| File | Contents |
| --- | --- |
| `expanded_sql_rows.json` | DDL accepted and schema-valid |
| `unchanged_sql_rows.json` | Model left the DDL unchanged |
| `expansion_failed_sql_statements.json` | Model or validation failure; original pair kept |
| `expansion_stats.json` | Counts and timing |

An HTTP or empty model response is a technical failure. It is recorded on the
row and does not change the SQL. This run wrote `data/sql/expanded/`: 68,983
expanded, 8,025 unchanged, and 1,569 failed.

## 5. Merge the expansion rows

Keep all three expansion products. `expanded_sql_rows.json` has the rewritten
DDL. `unchanged_sql_rows.json` kept the original DDL because the model did not
change it. `expansion_failed_sql_statements.json` keeps the original pair as
well; those 1,569 rows stay in the dataset. Together the three files are the
78,577 rows from step 3.

## 6. Spider classification

Classify last, on the rows from step 5. Correction and expansion both drop
stale hardness fields, and hardness depends on the final SQL.

```bash
scripts/run_spider_hardness.sh \
  --dataset-id data/sql/expanded/expanded_sql_rows.json \
  --output data/sql/classified \
  --foreground
```

Add `--dry-run` to classify at most 20 rows without writing files. This run
classified the expanded file: 68,983 rows, 66,162 labeled and 2,821 unlabeled.

| File | Contents |
| --- | --- |
| `classified_rows.json` | At least `extended_hardness` assigned |
| `unclassified_rows.json` | No parseable statement; both hardness fields null |
| `summary.json` | Counts by hardness |

`spider_hardness` follows Spider's original rules (`easy`, `medium`, `hard`,
`extra`) and stays null when the official adapter rejects the statement.
`extended_hardness` is still set in that case. The two labels are comparable
only when both are set. `extended_hardness` can also be `extreme`, including
when a nested SELECT is itself Spider `extra`.
Classification does not call a model and does not execute SQL.
