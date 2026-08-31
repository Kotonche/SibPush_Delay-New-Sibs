"""Logging helpers for the SibPush add-on."""

from __future__ import annotations

from datetime import datetime
import logging
import os
from pathlib import Path
from typing import Union


def _addon_root_path() -> Path:
    """Return the add-on root directory.

    Returns:
        pathlib.Path: The directory containing the add-on entrypoint file.
    """

    return Path(__file__).resolve().parents[1]


addon_path = str(_addon_root_path())
LOG_FILE_path = os.path.join(addon_path, "log.txt")

# Create a named logger for the add-on
logger = logging.getLogger("ProgressiveSiblings")
logger.setLevel(logging.DEBUG)
logger.propagate = False

def _ensure_file_handler() -> None:
    """Create the log lazily so debug=false never writes into the add-on directory."""

    if logger.handlers:
        return
    file_handler = logging.FileHandler(LOG_FILE_path, encoding="UTF-8")
    formatter = logging.Formatter("%(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def _clear_log_file() -> None:
    """Truncate the log file while keeping the active file handler open.

    Returns:
        None: This function is performed for its side effect.
    """

    _ensure_file_handler()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.stream is not None:
            handler.acquire()
            try:
                handler.flush()
                handler.stream.seek(0)
                handler.stream.truncate(0)
            finally:
                handler.release()
            return

    with open(LOG_FILE_path, "w", encoding="UTF-8"):
        pass


def logThis(arg: Union[str, object], clear: bool = False) -> None:
    """Write a debug message to the add-on log when logging is enabled.

    Args:
        arg (Union[str, object]): The message to log, or a zero-argument callable that
            returns the message.
        clear (bool, optional): Whether to clear the log file before writing. Defaults to False.

    Returns:
        None: The message is written only when debug logging is enabled.
    """

    from .config.parser import config_settings

    if config_settings["debug"]:
        _ensure_file_handler()
        message: str = str(arg() if callable(arg) else arg)

        # Clear the log file if the 'clear' flag is set.
        if clear:
            _clear_log_file()

        # Log the message using our named logger.
        logger.debug(message)


def initialize_log_file() -> None:
    """Seed the log file with a timestamp and legend.

    Returns:
        None: The log file is truncated and initialized for a fresh debug session.
    """

    logThis(
        str(datetime.today()) + """
# Legend for card details:
#   Type: 0=new, 1=learning, 2=due
#   Queue: same as above, plus: -1=suspended, -2=user buried, -3=sched buried
#   Due is used differently for different queues.
#       new queue: position
#       rev queue: integer day
#       lrn queue: integer timestamp
""",
        True,
    )
