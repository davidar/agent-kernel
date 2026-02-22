"""Centralized logging configuration.

Each process gets its own log files:
- {data_dir}/system/logs/{process}.log (+ .YYYY-MM-DD rotations)

Usage in each process entry point:
    from .logging_config import setup_process_logging
    setup_process_logging("agent")

Then in any module:
    from .logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Hello")
"""

import logging
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

from .config import data_dir

# Track which process we're in (set by setup_process_logging)
_current_process: str | None = None


class FlushingStreamHandler(logging.StreamHandler):
    """StreamHandler that flushes after every emit."""

    def emit(self, record):
        super().emit(record)
        self.flush()


def setup_process_logging(
    process_name: str,
    level: int = logging.INFO,
    console: bool = True,
    file: bool = True,
) -> logging.Logger:
    """
    Set up logging for an agent process.

    Call this ONCE at the entry point of each process:
    - agent.py main() -> setup_process_logging("agent")
    Args:
        process_name: Process identifier (e.g. "agent", "watcher")
        level: Minimum log level (default INFO)
        console: Whether to log to stderr
        file: Whether to log to rotating files

    Returns:
        Root logger for this process
    """
    global _current_process
    _current_process = process_name

    # Configure root logger for this process
    root = logging.getLogger()
    root.setLevel(level)

    # Clear any existing handlers
    root.handlers.clear()

    # Format: [HH:MM:SS] [LEVEL] module: message
    # For files, include date: [YYYY-MM-DD HH:MM:SS]
    console_fmt = logging.Formatter(
        fmt=f"[%(asctime)s] [{process_name}] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    file_fmt = logging.Formatter(
        fmt=f"[%(asctime)s] [{process_name}] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler (stderr) - flushes immediately
    if console:
        console_handler = FlushingStreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(console_fmt)
        root.addHandler(console_handler)

    # File handlers with rotation
    if file:
        log_dir = data_dir() / "system" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Daily rotating log file: agent.log -> agent.log.2024-01-26, etc.
        # Keeps 14 days of history
        daily_handler = TimedRotatingFileHandler(
            log_dir / f"{process_name}.log",
            when="midnight",
            interval=1,
            backupCount=14,
            encoding="utf-8",
        )
        daily_handler.setLevel(level)
        daily_handler.setFormatter(file_fmt)
        daily_handler.suffix = "%Y-%m-%d"
        root.addHandler(daily_handler)

        # Size-based rotating handler for high-volume logs
        # 5MB per file, keep 5 backups (catches runaway logging in a single day)
        size_handler = RotatingFileHandler(
            log_dir / f"{process_name}-current.log",
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=5,
            encoding="utf-8",
        )
        size_handler.setLevel(level)
        size_handler.setFormatter(file_fmt)
        root.addHandler(size_handler)

    return root


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a module.

    Call at module level: logger = get_logger(__name__)

    If process logging hasn't been set up yet, returns a basic logger.
    Once setup_process_logging() is called, all loggers automatically
    use the configured handlers.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Logger instance
    """
    return logging.getLogger(name)
