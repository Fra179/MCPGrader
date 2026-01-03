from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional, Union

RESET = "\x1b[0m"
COLORS = {
    logging.DEBUG: "\x1b[36m",     # Cyan
    logging.INFO: "\x1b[32m",      # Green
    logging.WARNING: "\x1b[33m",   # Yellow
    logging.ERROR: "\x1b[31m",     # Red
    logging.CRITICAL: "\x1b[35m",  # Magenta
}

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(filename)s:%(lineno)d | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class ColoredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        level_color = COLORS.get(record.levelno, "")
        if level_color:
            record.levelname = f"{level_color}{record.levelname}{RESET}"
        return super().format(record)


def _coerce_path(path: Optional[Union[str, os.PathLike[str], Path]]) -> Optional[Path]:
    if path is None:
        return None
    if isinstance(path, Path):
        return path
    return Path(path)


def build_logger(
    name: str = "grader",
    level: int = logging.INFO,
    log_file: Optional[Union[str, os.PathLike[str], Path]] = None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(level)
    logger.propagate = False

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(level)
    stream_handler.setFormatter(ColoredFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logger.addHandler(stream_handler)

    file_path = _coerce_path(log_file)
    if file_path:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(file_path)
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
        logger.addHandler(file_handler)

    return logger

