"""Console logging shared by SQL workflows."""

import argparse
import logging
import sys

LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def parse_log_level(value: str) -> int:
    """Return a logging level. Names are case-insensitive."""
    name = str(value).upper()
    try:
        return LOG_LEVELS[name]
    except KeyError:
        choices = ", ".join(LOG_LEVELS)
        raise argparse.ArgumentTypeError(
            f"invalid log level {value!r}; choose from {choices}"
        ) from None


def get_console_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    for handler in logger.handlers:
        handler.setLevel(level)

    return logger
