---
license: cc-by-4.0
language:
  - en
pretty_name: sql-create-context refined
size_categories:
  - 10K<n<100K
task_categories:
  - text-generation
  - question-answering
tags:
  - text-to-sql
  - SQL
  - refinement
  - spider
source_datasets:
  - philschmid/sql-create-context-copy
  - b-mc2/sql-create-context
dataset_info:
  config_name: default
  features:
    - name: question
      dtype: string
    - name: answer
      dtype: string
    - name: context
      dtype: string
    - name: dialect
      dtype: string
    - name: syntax_status
      dtype: string
    - name: ddl_syntax_status
      dtype: string
    - name: schema_status
      dtype: string
    - name: syntax_errors
      sequence: string
    - name: ddl_syntax_errors
      sequence: string
    - name: schema_errors
      sequence: string
    - name: status_history
      sequence: string
    - name: correction_status
      dtype: string
    - name: validation_source
      struct:
        - name: split
          dtype: string
        - name: row_index
          dtype: int64
  splits:
    - name: train
      num_bytes: 52910883
      num_examples: 78577
configs:
  - config_name: default
    data_files:
      - split: train
        path: sql_context_v2.json
---

# sql-create-context refined

This dataset is a refinement of
[philschmid/sql-create-context-copy](https://huggingface.co/datasets/philschmid/sql-create-context-copy).
It is not a new collection of questions. The copy is itself a duplicate of
[b-mc2/sql-create-context](https://huggingface.co/datasets/b-mc2/sql-create-context).
Row order and the 78,577 questions are preserved. The SQL answers are
preserved except for quote normalisation and 541 structural repairs. The
`CREATE TABLE` context is preserved or expanded.

The dataset file is `data/sql/sql_context_v2.json` in the
[sql_tools](https://github.com/bayerth/sql_tools) repository. On the Hub the
same file is `sql_context_v2.json`, next to this `README.md`. Spider and
extended hardness are computed by the tools in that repository and are not
columns of this file.

## What changed relative to sql-create-context-copy

| Step | What it does to the copy | Rows affected |
| --- | --- | ---: |
| Quote rewrite | Double-quoted string literals in `answer` become single-quoted SQL strings. An apostrophe inside a literal is doubled. `question` and `context` are copied unchanged. Three `FROM "t"` table names stay double-quoted. | 62,967 statements changed; 503 apostrophes doubled |
| Validation | Generic SQLGlot check of SQL syntax, DDL syntax, and schema references. No SQL is executed. | 78,036 valid; 541 rejected |
| Repair | Only the 541 rejected rows are rewritten. A candidate is kept only when the new SQL and DDL validate. | 541 applied |
| DDL expansion | A model may add columns and give bare `VARCHAR` a length. The SQL query is not changed. Failed expansions keep the previous DDL. | 68,983 expanded; 8,025 unchanged; 1,569 failed, all kept |

`dialect` is `generic` on the 78,036 rows that passed the first validation, and
`mysql` on the 541 repaired rows. The last entry of `status_history` is
`extended` (68,983), `extension_unchanged` (8,025), or `extension_failed`
(1,569). `correction_status` is `corrected` on the 541 repaired rows and empty
otherwise.

## Columns

Training and evaluation use `question`, `answer`, `context`, and `dialect`.

| Column | Meaning |
| --- | --- |
| `question` | Natural-language question, unchanged from the copy. |
| `answer` | SQL query. Quotes are normalised. SQL text changes only on the 541 repaired rows. |
| `context` | One or more `CREATE TABLE` statements. Expanded rows have a richer DDL; unchanged and failed rows keep the previous DDL. |
| `dialect` | `generic`, or `mysql` on a repaired row. |
| `syntax_status`, `ddl_syntax_status`, `schema_status` | Result of the structural checks. |
| `syntax_errors`, `ddl_syntax_errors`, `schema_errors` | Diagnostic lists. Empty when the check passed. |
| `status_history` | Ordered record of the pipeline events for the row. |
| `correction_status` | `corrected` on repaired rows; empty otherwise. |
| `validation_source` | Split name and row index in the source copy. |

## Limitations

Validation does not execute SQL. A row can be structurally valid and still be
a poor answer to its question. The 541 repairs were accepted because the
repaired SQL and DDL parse and the names resolve, not because a database
returned the expected rows. Columns added during expansion were proposed by
`openai/gpt-oss-120b`. They are not columns from a production schema.

## License and attribution

The source dataset is released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This refinement is
distributed under the same license. Cite the source dataset as well as this
refinement.

## Citation

```bibtex
@misc{bayer2026sqlcreatecontextrefined,
  title        = {sql-create-context refined},
  author       = {Bayer, Thomas and Grok},
  year         = {2026},
  url          = {https://github.com/bayerth/sql_tools},
  note         = {Refinement of philschmid/sql-create-context-copy, itself a duplicate of b-mc2/sql-create-context.}
}

@misc{bmc2_2023_sqlcreatecontext,
  title        = {sql-create-context Dataset},
  author       = {b-mc2},
  year         = {2023},
  url          = {https://huggingface.co/datasets/b-mc2/sql-create-context},
  note         = {Built by modifying WikiSQL and Spider.}
}

@inproceedings{yu2018spider,
  title        = {Spider: A Large-Scale Human-Labeled Dataset for Complex and Cross-Domain Semantic Parsing and Text-to-{SQL} Task},
  author       = {Yu, Tao and Zhang, Rui and Yang, Kai and Yasunaga, Michihiro and Wang, Dongxu and Li, Zifan and Ma, James and Li, Irene and Yao, Qingning and Roman, Shanelle and Zhang, Zilin and Radev, Dragomir},
  booktitle    = {Proceedings of the 2018 Conference on Empirical Methods in Natural Language Processing},
  pages        = {3911--3921},
  year         = {2018},
  address      = {Brussels, Belgium}
}
```
