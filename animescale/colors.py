"""ANSI color helpers for terminal output."""
import logging
import sys


def _is_tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


# Disabled when stdout is not a terminal (piped, redirected, etc.)
ENABLED = _is_tty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if ENABLED else text


def red(text: str) -> str:    return _c("31", text)
def green(text: str) -> str:  return _c("32", text)
def yellow(text: str) -> str: return _c("33", text)
def cyan(text: str) -> str:   return _c("36", text)
def bold(text: str) -> str:   return _c("1",  text)
def dim(text: str) -> str:    return _c("2",  text)


def pct_color(value: int, total: int) -> str:
    """Return a color-coded percentage string based on completion level."""
    if total <= 0:
        return "0.0%"
    p = value / total * 100
    s = f"{p:.1f}%"
    if not ENABLED:
        return s
    if p >= 80:
        return _c("32", s)   # green
    if p >= 40:
        return _c("33", s)   # yellow
    return _c("31", s)        # red


def load_color(pct_val: int) -> str:
    """Return a color-coded load percentage (GPU/CPU usage — high load = red)."""
    s = f"{pct_val}%"
    if not ENABLED:
        return s
    if pct_val >= 80:
        return _c("31", s)   # red
    if pct_val >= 50:
        return _c("33", s)   # yellow
    return _c("32", s)        # green


class ColorFormatter(logging.Formatter):
    """
    Console log formatter that applies ANSI colors by level and message content.
    The plain Formatter is used for file handlers so log files stay clean.
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not ENABLED:
            return msg

        if record.levelno >= logging.ERROR:
            return _c("31", msg)
        if record.levelno >= logging.WARNING:
            return _c("33", msg)

        # INFO: highlight specific content
        text = record.getMessage()
        if "DONE —" in text or "COMPLETE:" in text:
            return _c("32", msg)
        if "=====" in text or "==========" in text:
            return _c("1", msg)    # bold
        if any(text.startswith(p) for p in (
            "[", "Detecting", "Analyzing", "Extracting", "Linking",
            "Fast-scaling", "Upscaling", "Encoding", "Querying",
        )):
            # Stage step lines get a subtle cyan tint on the bracket/verb
            pass

        return msg
