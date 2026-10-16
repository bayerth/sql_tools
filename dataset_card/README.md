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
    - name: spider_hardness
      dtype: string
    - name: extended_hardness
      dtype: string
    - name: classification_status
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
    - name: classification_errors
      sequence: string
    - name: status_history
      sequence: string
    - name: correction_status
      dtype: string
    - name: component1
      dtype: int64
    - name: component2
      dtype: int64
    - name: others
      dtype: int64
    - name: validation_source
      struct:
        - name: split
          dtype: string
        - name: row_index
          dtype: int64
  splits:
    - name: train
      num_bytes: 70395555
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
`CREATE TABLE` context is preserved or expanded, and every row has
`extended_hardness`. Where the official Spider adapter accepts the statement,
the row also has `spider_hardness`.

The dataset file is `data/sql/sql_context_v2.json` in the
[sql_tools](https://github.com/bayerth/sql_tools) repository. On the Hub the
same file is `sql_context_v2.json`, next to this `README.md`.

## What changed relative to sql-create-context-copy

| Step | What it does to the copy | Rows affected |
| --- | --- | ---: |
| Quote rewrite | Double-quoted string literals in `answer` become single-quoted SQL strings. An apostrophe inside a literal is doubled. `question` and `context` are copied unchanged. Three `FROM "t"` table names stay double-quoted. | 62,967 statements changed; 503 apostrophes doubled |
| Validation | Generic SQLGlot check of SQL syntax, DDL syntax, and schema references. No SQL is executed. | 78,036 valid; 541 rejected |
| Repair | Only the 541 rejected rows are rewritten. A candidate is kept only when the new SQL and DDL validate. | 541 applied |
| DDL expansion | A model may add columns and give bare `VARCHAR` a length. The SQL query is not changed. Failed expansions keep the previous DDL. | 68,983 expanded; 8,025 unchanged; 1,569 failed, all kept |
| Hardness | `spider_hardness` where the official adapter accepts the statement, and `extended_hardness` on every row. | 78,577 extended labels; 75,382 Spider labels |

`dialect` is `generic` on the 78,036 rows that passed the first validation, and
`mysql` on the 541 repaired rows. `correction_status` is `corrected` on those
541 rows and empty otherwise. Compare `spider_hardness` and
`extended_hardness` only when both are set. They use the same names
(`easy`, `medium`, `hard`, `extra`). `extended_hardness` can also be
`extreme`.

## Hardness counts

Spider shares are of the 75,382 rows with a Spider label. Extended shares are
of all 78,577 rows.

| Label | Spider | Extended |
| --- | ---: | ---: |
| easy | 51,082 | 53,001 |
| medium | 22,693 | 23,957 |
| hard | 913 | 921 |
| extra | 694 | 690 |
| extreme |  | 8 |
| labeled | 75,382 | 78,577 |

`classification_status` is `classified` when both labels are set (75,382 rows)
and `extended` when only `extended_hardness` is set (3,195 rows). No row lacks
an extended label. The last entry of `status_history` is the same value.

## Columns

Training and evaluation use `question`, `answer`, `context`, `dialect`,
`spider_hardness`, and `extended_hardness`.

| Column | Meaning |
| --- | --- |
| `question` | Natural-language question, unchanged from the copy. |
| `answer` | SQL query. Quotes are normalised. SQL text changes only on the 541 repaired rows. |
| `context` | One or more `CREATE TABLE` statements. Expanded rows have a richer DDL; unchanged and failed rows keep the previous DDL. |
| `dialect` | `generic`, or `mysql` on a repaired row. |
| `spider_hardness` | `easy`, `medium`, `hard`, `extra`, or empty. |
| `extended_hardness` | `easy`, `medium`, `hard`, `extra`, or `extreme`. Always set. |
| `classification_status` | `classified` or `extended`. |
| `syntax_status`, `ddl_syntax_status`, `schema_status` | Result of the structural checks. |
| `syntax_errors`, `ddl_syntax_errors`, `schema_errors`, `classification_errors` | Diagnostic lists. Empty when the check passed. |
| `status_history` | Ordered record of the pipeline events for the row. |
| `correction_status` | `corrected` on repaired rows; empty otherwise. |
| `component1`, `component2`, `others` | Spider counters. Empty when `spider_hardness` is empty. |
| `validation_source` | Split name and row index in the source copy. |

## Limitations

Validation and hardness do not execute SQL. A row can be structurally valid
and still be a poor answer to its question. The 541 repairs were accepted
because the repaired SQL and DDL parse and the names resolve, not because a
database returned the expected rows. Columns added during expansion were
proposed by `openai/gpt-oss-120b`. They are not columns from a production
schema. Hardness measures statement shape, not how difficult a person would
find the question.

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
