"""Validate a Hugging Face or local dataset and save rows for subsequent repair."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from logging import Logger
import logging

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sql_tools.validation import validate_sql_statements
else:
    from .validation import validate_sql_statements
from sql_tools.logging_utils import get_console_logger, parse_log_level


def _load_dataset(dataset_id):
    """Load saved HF datasets or local JSON/JSONL/CSV/Parquet files."""
    from datasets import load_dataset, load_from_disk

    path = Path(dataset_id).expanduser()
    if path.is_dir():
        return load_from_disk(str(path))
    if path.is_file():
        formats = {
            ".json": "json",
            ".jsonl": "json",
            ".csv": "csv",
            ".parquet": "parquet",
        }
        if path.suffix.lower() not in formats:
            raise ValueError(f"Unsupported local dataset format: {path.suffix}")
        return load_dataset(formats[path.suffix.lower()], data_files=str(path))
    if path.is_absolute() or str(dataset_id).startswith((".", "~")):
        raise FileNotFoundError(path)
    return load_dataset(str(dataset_id))


def run_sql_validation(
    dataset_id: str,
    output: str | Path | None = None,
    max_rows: int | None = None,
    *,
    dialect: str = "mysql",
    logger: logging.Logger = None,
    override: bool = True,
    foreground: bool = False,
    log_level: int = logging.INFO,
) -> dict:
    """Validate up to max_rows across all splits; None processes every row.

    Rows contain answer (SQL) and context (DDL), as in the validation pipeline.
    This wrapper only loads and saves; validate_sql_statements owns processing.
    Every failed row is written once, even when several checks fail. No LLM
    requests or SQL execution occur. Existing result files are replaced.
    override false refuses when any of them already exist. None output logs
    the summary and writes no files.
    """
    if max_rows is not None and max_rows < 0:
        raise ValueError("max_rows must be non-negative or None")
    logger = logger or get_console_logger("SQL Validation", level=log_level)
    destination = None if output is None else Path(output).expanduser()
    logger.info(
        f"Parameters: dataset_id={dataset_id}; output={destination}; "
        f"max_rows={max_rows}; dialect={dialect}; override={override}; "
        f"foreground={foreground}; log_level={logging.getLevelName(log_level)}"
    )
    paths = {}
    if destination is not None:
        paths = {
            name: destination / f"{name}.json"
            for name in ("correct_prompts", "incorrect_prompts", "summary")
        }
        if not override:
            for path in paths.values():
                if path.exists():
                    raise FileExistsError(f"Result already exists: {path}")
    dataset = _load_dataset(dataset_id)
    splits = dataset if isinstance(dataset, dict) else {"data": dataset}

    def iter_rows():
        for split_name, split in splits.items():
            for index, source in enumerate(split):
                row = dict(source)
                row["validation_source"] = {"split": split_name, "row_index": index}
                yield row

    valid_rows, invalid_rows = validate_sql_statements(
        iter_rows(),
        max_rows=max_rows,
        dialect=dialect,
        logger=logger,
    )
    results = {"correct_prompts": valid_rows, "incorrect_prompts": invalid_rows}
    rows = valid_rows + invalid_rows
    summary = {
        "dataset_id": str(dataset_id),
        "dialect": dialect,
        "processed": len(rows),
        "valid": len(results["correct_prompts"]),
        "invalid": len(results["incorrect_prompts"]),
        "checks": {
            field: dict(Counter(row[field] for row in rows))
            for field in ("syntax_status", "ddl_syntax_status", "schema_status")
        },
    }
    if destination is None:
        logger.info(f"Validation summary: {json.dumps(summary, ensure_ascii=False)}")
        return summary
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in {**results, "summary": summary}.items():
        with paths[name].open("w" if override else "x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    logger.info(f"Validation results saved to {destination}")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset-id",
        "--dataset_id",
        required=True,
        help="Hugging Face dataset ID, saved dataset directory, or local data file",
    )
    parser.add_argument(
        "--max-rows",
        "--max_rows",
        type=int,
        default=None,
        help="Maximum rows across all splits (default: all)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Result directory. Omit to log the summary without writing files",
    )
    parser.add_argument(
        "--override",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replace every existing result file (default). "
        "--no-override refuses when any of them already exist.",
    )
    parser.add_argument(
        "--dialect", default="generic", help="SQL dialect (default: generic)"
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Stay in this process. Shell launchers then skip nohup (default: false)",
    )
    parser.add_argument(
        "--log-level", "--log_level", type=parse_log_level, default=logging.INFO,
        help="Console log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL (default: INFO)",
    )
    args = parser.parse_args(argv)
    logger = get_console_logger("SQL Validation", level=args.log_level)
    try:
        run_sql_validation(
            args.dataset_id,
            args.output,
            args.max_rows,
            dialect=args.dialect,
            logger=logger,
            override=args.override,
            foreground=args.foreground,
            log_level=args.log_level,
        )
    except Exception:
        logger.exception("Validation failed")
        sys.exit(1)
    logger.info("Validation completed successfully")


if __name__ == "__main__":
    main()
