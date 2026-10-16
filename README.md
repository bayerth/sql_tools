# SQL dataset tools

Standalone SQL/DDL validation, schema checking, Spider/extended hardness,
LLM repair and DDL expansion. The Python package remains `sql_tools`.
Training libraries such as PyTorch, Transformers and PEFT are not required.

## Setup

```bash
uv sync --extra data --extra llm --extra notebooks
uv run sql-validate --dataset-id data/sql_context_copy --output outputs/validation
uv run sql-classify --help
uv run sql-repair --help
uv run sql-expand --help
```

The base installation requires only SQLGlot. `data` enables dataset loaders;
`llm` enables LM Studio clients; `notebooks` includes interactive dependencies.
Configure model endpoints and credentials with the environment variables
described in the [workflow reference](src/sql_tools/README.md).

## Single-quoted dataset

`data/sql_context_copy` is a local copy of `philschmid/sql-create-context-copy`.
It stores string literals in double quotes (`Status <> "Village"`). The generic
SQLGlot dialect reads those quotes as identifiers and reports a missing column.

Rewrite `answer` to single quotes before validation:

```bash
uv run python -m sql_tools.rewrite_sql_string_quotes
```

The default input is `data/sql_context_copy`. The default output is
`data/sql/sql_context_copy_single_quoted.json`. An apostrophe inside a literal
is doubled (`"James O'Connor"` becomes `'James O''Connor'`). `question` and
`context` are copied unchanged. A double-quoted name directly after `FROM`
stays, because three rows use `FROM "t"` as a table identifier. On this copy
the rewrite writes 78,577 rows, changes 62,967 statements, and doubles
apostrophes in 503 places.

## Refined dataset

The refined dataset is `data/sql/sql_context_v2.json`: 78,577 rows, with
normalised quotes, 541 repaired statements, expanded DDL where expansion
succeeded, and `spider_hardness` plus `extended_hardness`. Its Hugging Face
card is `dataset_card/README.md`.

## Tests

```bash
uv run --extra data --extra llm python -m unittest discover -s src/sql_tools/tests -v
```

## Migration from PEFT

Code, tests, the expansion launcher and `data/sql_context_copy` and
`data/sql_test_ds` were moved from the sibling `peft` checkout. Dataset hashes
were verified; see `migration_manifest.json`. No notebooks were moved.

PEFT retains compatibility symlinks for `src/sql_tools` and the two datasets,
a forwarding dataset-validation module, and a forwarding Bash launcher.
Its package configuration depends on this editable sibling project, excluding
the compatibility symlink from its own distribution.

Move the SQL notebooks manually to `notebooks/dev/sql` to preserve their
`../../../data` paths. Use `sql_tools.*`, `sql_tools.dataset_validation` and
`sql_tools.logging_utils` imports. Some notebook imports were already updated
before migration was interrupted. Existing SQL result files and `sql_openai`
remain in PEFT; update those notebook input paths or copy the required inputs.
The new launcher defaults to `data/sql_validation/correct_rows.json`; provide
`EXPANSION_INPUT` until a validation result has been generated there.

The Spider evaluator license is retained in `src/sql_tools/SPIDER_LICENSE`.
