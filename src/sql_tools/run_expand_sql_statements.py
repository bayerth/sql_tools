"""Run validated DDL expansion using the notebook's local LM Studio model."""

import argparse
from collections.abc import Mapping
from itertools import islice
import json
import logging
import os
from pathlib import Path
from time import perf_counter
from rwu_llmconnector import LMStudioClient

if __package__ in (None, ""):
    # Direct script execution (including IDE run configurations) needs src on sys.path.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sql_tools.expand import DEFAULT_SYSTEM_MESSAGE, expand_sql_statements
    from sql_tools.expansion_batches import prepare_expansion_batches, request_expansion_batches
else:
    from .expand import DEFAULT_SYSTEM_MESSAGE, expand_sql_statements
    from .expansion_batches import prepare_expansion_batches, request_expansion_batches
from sql_tools.logging_utils import get_console_logger, parse_log_level

MODEL = "openai/gpt-oss-20b"
# Match the remote model's loaded context in both single-row and batch mode.
CONTEXT_LENGTH = 32768
LMSTUDIO_TOKEN = "sk-lm-t3GXFGt7:EFGg4PK6myygECIdyA0U"
LMSTUDIO_MAC_URL = "http://141.69.86.240:1234"
MAC_STUDIO_API_KEY = "sk-lm-fxWPcVe9:3oFQNF8wO68jfPtTxABn"

DRY_RUN_ROWS = 20


def _read_rows(source):
    """Iterate local JSON/JSONL files, saved datasets, or a Hub dataset."""
    from datasets import load_dataset, load_from_disk

    path = Path(source).expanduser()
    if path.is_dir():
        dataset = load_from_disk(str(path))
    elif path.is_file():
        dataset = load_dataset("json", data_files=str(path), streaming=True)
    else:
        if path.is_absolute() or str(source).startswith((".", "~")):
            raise FileNotFoundError(path)
        dataset = load_dataset(str(source), streaming=True)
    splits = dataset.values() if isinstance(dataset, Mapping) else [dataset]
    for split in splits:
        yield from split


def _normalize_row(source, openai_format):
    """Expose question/answer/context directly on the input row."""
    row = source
    if openai_format:
        messages = row.get("messages", row.get("text"))
        if not isinstance(messages, list):
            raise ValueError("OpenAI rows require a messages or text list")
        for role, field in (("system", "context"), ("user", "question"), ("assistant", "answer")):
            matches = [message for message in messages if message.get("role") == role]
            if len(matches) != 1 or not isinstance(matches[0].get("content"), str):
                raise ValueError(f"Expected exactly one textual {role} message")
            row[field] = matches[0]["content"]
    if not isinstance(row.get("answer"), str) or not isinstance(row.get("context"), str):
        raise ValueError("Rows require answer and context strings")
    return row


def _append_checkpoint(path, rows):
    """Append complete JSONL records and synchronize them to disk."""
    with path.open("a", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_expand_sql_statements(input_path, output, *, client, openai_format=False,
                              dry_run=False, batch_size=0, max_rows=0, checkpoints=False,
                              checkpoint_size=100, context_length=CONTEXT_LENGTH, logger=None,
                              override=True, foreground=False,
                              log_level=logging.INFO
                              ):
    """Expand every input row, or at most ten in dry-run mode, and report usage.

    Dry-run still calls the LLM but creates no output directory or files. Output
    rows preserve metadata; OpenAI system messages are synchronized on success.
    override replaces every result and checkpoint file; False refuses when any
    of them already exist.
    """
    if client is None:
        raise ValueError("client must be provided")
    if batch_size < 0:
        raise ValueError("batch_size must be non-negative")
    if max_rows < 0:
        raise ValueError("max_rows must be non-negative")
    if checkpoint_size <= 0:
        raise ValueError("checkpoint_size must be positive")
    if context_length <= 0:
        raise ValueError("context_length must be positive")
    logger = logger or get_console_logger("SQL Expansion", level=log_level)
    destination = Path(output).expanduser()
    model = getattr(client, "model", None)
    model = model if isinstance(model, str) else "unknown"
    logger.info(
        f"Parameters: input={input_path}; output={destination}; "
        f"openai_format={openai_format}; dry_run={dry_run}; batch_size={batch_size}; "
        f"max_rows={max_rows}; checkpoints={checkpoints}; checkpoint_size={checkpoint_size}; "
        f"context_length={context_length}; model={model}; override={override}; "
        f"foreground={foreground}; log_level={logging.getLevelName(log_level)}"
    )
    paths = {"expanded": destination / "expanded_sql_rows.json",
             "unchanged": destination / "unchanged_sql_rows.json",
             "failed": destination / "expansion_failed_sql_statements.json",
             "stats": destination / "expansion_stats.json"}
    if not dry_run and not override:
        for path in paths.values():
            if path.exists():
                raise FileExistsError(f"Result already exists: {path}")
    checkpoint_paths = {
            "expanded": destination / "expanded_sql_rows.tmp.jsonl",
            "unchanged": destination / "unchanged_sql_rows.tmp.jsonl",
            "failed": destination / "expansion_failed_sql_statements.tmp.jsonl",
            }
    save_checkpoints = checkpoints and not dry_run
    if save_checkpoints:
        if not override:
            for path in checkpoint_paths.values():
                if path.exists():
                    raise FileExistsError(
                        f"Checkpoint already exists; preserve or move it before restarting: {path}"
                    )
        destination.mkdir(parents=True, exist_ok=True)
        for path in checkpoint_paths.values():
            if override and path.exists():
                path.unlink()
            path.touch(exist_ok=False)
    started = perf_counter()
    rows = _read_rows(input_path)
    # Finish the last requested batch; dry-run remains a hard ten-row cap.
    row_limit = max_rows or None
    if row_limit is not None and batch_size > 0:
        row_limit = ((row_limit + batch_size - 1) // batch_size) * batch_size
    if dry_run:
        row_limit = min(row_limit, DRY_RUN_ROWS) if row_limit is not None else DRY_RUN_ROWS
    # Streaming inputs have no known total; a row limit is only an upper bound.
    progress_total = f"up to {row_limit:,}" if row_limit is not None else "unknown total"
    if row_limit is not None:
        rows = islice(rows, row_limit)
    input_failures = []

    def normalized_rows(chunk):
        for index, source in chunk:
            logger.info(f"Processing row {index + 1:,} of {progress_total}")
            source["expansion_source"] = {"input": str(input_path), "row_index": index}
            try:
                row = _normalize_row(source, openai_format)
            except ValueError as error:
                row = source
                row["status_history"] = list(row.get("status_history") or []) + ["extension_failed"]
                row.setdefault("extension_history", []).append(
                        {"status": "extension_failed", "error": str(error)}
                        )
                input_failures.append(row)
                logger.debug(f"Row {index:,}: invalid input: {error}")
            else:
                row.pop("sql_dialect", None)
                if not row.get("dialect"):
                    row["dialect"] = "mysql"
                yield row

    interval = checkpoint_size
    if batch_size > 0:
        interval = ((interval + batch_size - 1) // batch_size) * batch_size
    indexed_rows = enumerate(rows)

    def chunks():
        if save_checkpoints:
            while chunk := list(islice(indexed_rows, interval)):
                yield chunk
        else:
            yield indexed_rows

    expanded, unchanged, failed = [], [], []
    request_seconds = 0.0
    batch_offset = 0
    for chunk in chunks():
        input_failures.clear()
        if batch_size > 0:
            # Keep the model's load-time context configuration stable across modes.
            batches = prepare_expansion_batches(
                    normalized_rows(chunk), batch_size=batch_size, context_length=context_length
                    )
            successful, same, unsuccessful, chunk_seconds, batch_count = request_expansion_batches(
                    batches, client=client, logger=logger, context_length=context_length,
                    batch_offset=batch_offset,
                    )
            batch_offset += batch_count
        else:
            successful, same, unsuccessful, chunk_seconds = expand_sql_statements(
                    rows=normalized_rows(chunk), client=client, system_message=DEFAULT_SYSTEM_MESSAGE,
                    context_length=context_length, logger=logger,
                    )
        request_seconds += chunk_seconds
        unsuccessful.extend(input_failures)
        if openai_format:
            for row in successful:
                for key in ("messages", "text"):
                    if isinstance(row.get(key), list):
                        for message in row[key]:
                            if message.get("role") == "system":
                                message["content"] = row["context"]
        expanded.extend(successful)
        unchanged.extend(same)
        failed.extend(unsuccessful)
        if save_checkpoints:
            _append_checkpoint(checkpoint_paths["expanded"], successful)
            _append_checkpoint(checkpoint_paths["unchanged"], same)
            _append_checkpoint(checkpoint_paths["failed"], unsuccessful)
            logger.info(
                    f"Checkpoint saved: {len(expanded) + len(unchanged) + len(failed):,} rows; "
                    f"expanded: {len(expanded):,}; unchanged: {len(unchanged):,}; "
                    f"failed: {len(failed):,}"
                    )
    elapsed = perf_counter() - started
    count = len(expanded) + len(unchanged) + len(failed)
    request_seconds_per_row = request_seconds / count if count else 0
    stats = {
            "model": model, "dry_run": dry_run, "processed": count,
            "expanded": len(expanded), "unchanged": len(unchanged), "failed": len(failed),
            "elapsed_seconds": elapsed,
            "seconds_per_row": elapsed / count if count else 0,
            "request_seconds": request_seconds,
            "request_seconds_per_row": request_seconds_per_row,
            }
    logger.info(
            f"Processed: {count:,}; expanded: {len(expanded):,}; "
            f"unchanged: {len(unchanged):,}; failed: {len(failed):,}; "
            f"elapsed: {elapsed:,.2f}s; seconds/row: {stats['seconds_per_row']:,.2f}; "
            f"LLM requests: {request_seconds:,.3f}s; {request_seconds_per_row:,.3f}s/row"
            )
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
        for name, data in (
                ("expanded", expanded), ("unchanged", unchanged), ("failed", failed), ("stats", stats)
                ):
            with paths[name].open("w" if override else "x", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
        logger.info(f"Rows saved to {destination}")
        # Keep checkpoints if any final write fails or the run is interrupted.
        if save_checkpoints:
            for path in checkpoint_paths.values():
                path.unlink()
    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="JSON/JSONL file, saved dataset directory or HF dataset ID")
    parser.add_argument("--output", required=True, help="Output directory")
    parser.add_argument("--openai-format", "--openai_format", action="store_true", default=False)
    parser.add_argument("--dry-run", "--dry_run", action="store_true")
    parser.add_argument("--local", action="store_true", default=False)
    parser.add_argument("--batch-size", "--batch_size", type=int, default=0)
    parser.add_argument(
            "--context-length", "--context_length", type=int, default=CONTEXT_LENGTH,
            help="Context tokens for both modes; match the loaded model to avoid reloading."
            )
    parser.add_argument(
            "--max-rows", "--max_rows", type=int, default=0,
            help="Row limit; 0 means all. Batch mode rounds up to a full batch."
            )
    parser.add_argument(
        "--override",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Replace every existing result and checkpoint file (default). "
        "--no-override refuses when any of them already exist.",
    )
    parser.add_argument(
        "--foreground", action="store_true",
        help="Stay in this process. Shell launchers then skip nohup (default: false)",
    )
    parser.add_argument("--checkpoints", action="store_true")
    parser.add_argument("--checkpoint-size", "--checkpoint_size", type=int, default=100)
    parser.add_argument("--model", help="Override the local 20B / remote 120B model")
    parser.add_argument(
        "--log-level", "--log_level", type=parse_log_level, default=logging.INFO,
        help="Console log level: DEBUG, INFO, WARNING, ERROR, or CRITICAL (default: INFO)",
    )
    args = parser.parse_args(argv)
    logger = get_console_logger("SQL Expansion", level=args.log_level)
    if args.local:
        base_url = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234")
        model = args.model or MODEL
        api_key = os.getenv("LMSTUDIO_API_KEY", LMSTUDIO_TOKEN)
    else:
        base_url = os.getenv("LMSTUDIO_MAC_URL", LMSTUDIO_MAC_URL)
        model = args.model or "openai/gpt-oss-120b"
        api_key = os.getenv("MAC_STUDIO_API_KEY", MAC_STUDIO_API_KEY)
    target = "local LM Studio" if args.local else "Mac Studio"
    logger.info(f"Using {target}: {base_url}; model: {model}")
    rc = LMStudioClient(
            base_url=base_url,
            model=model,
            api_key=api_key,
            system_messgage="You are a helpful assistant."
            )
    run_expand_sql_statements(
            args.input,
            args.output,
            openai_format=args.openai_format,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            context_length=args.context_length,
            max_rows=args.max_rows,
            checkpoints=args.checkpoints,
            checkpoint_size=args.checkpoint_size,
            client=rc,
            logger=logger,
            override=args.override,
            foreground=args.foreground,
            log_level=args.log_level,
            )


if __name__ == "__main__":
    main()
