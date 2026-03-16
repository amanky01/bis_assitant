"""app/core/logging.py — structured logging via Rich."""
import logging
from rich.console import Console
from rich.logging import RichHandler

_console = Console(stderr=True)


def setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    handler = RichHandler(console=_console, show_time=True, rich_tracebacks=True)
    handler.setLevel(log_level)
    root.setLevel(log_level)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "motor", "pymongo", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Gemini tool schema strips unsupported keys (title, default); avoid log spam
    logging.getLogger("langchain_google_genai._function_utils").setLevel(logging.ERROR)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
