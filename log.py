"""
Logging module - Pure console output (stateless functional style)
- LOG_LEVEL controls level (debug/info/warning/error/critical, default info)
- ENABLE_LOG=0 completely silences logs (no output printed)
- log.debug/info/warning/error/critical(message) records logs
- set_log_level dynamically adjusts level, get_log_level reads current level
"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace

# Log level definitions
LOG_LEVELS = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

# Cached configuration (read once upon module load)
_cached_log_level: int = LOG_LEVELS["info"]
_log_enabled: bool = True


def _refresh_config():
    """Refresh cached configuration from environment variables."""
    global _cached_log_level, _log_enabled
    level = os.getenv("LOG_LEVEL", "info").lower()
    _cached_log_level = LOG_LEVELS.get(level, LOG_LEVELS["info"])
    _log_enabled = os.getenv("ENABLE_LOG", "1").strip().lower() not in ("0", "false", "no", "off")


_refresh_config()


def _log(level: str, message: str):
    # Fast short-circuit: return immediately when logging is disabled, zero overhead
    if not _log_enabled:
        return

    level_val = LOG_LEVELS.get(level.lower())
    if level_val is None:
        return

    if level_val < _cached_log_level:
        return

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level.upper()}] {message}"

    if level in ("error", "critical"):
        print(entry, file=sys.stderr, flush=True)
    else:
        print(entry, flush=True)


def set_log_level(level: str):
    """Dynamically set log level."""
    global _cached_log_level
    level = level.lower()
    if level not in LOG_LEVELS:
        return False
    _cached_log_level = LOG_LEVELS[level]
    return True


def get_log_level() -> str:
    """Get current log level name."""
    current = _cached_log_level
    for name, value in LOG_LEVELS.items():
        if value == current:
            return name
    return "info"


# Logging interface: log.debug/info/warning/error/critical(message)
log = SimpleNamespace(
    debug=lambda message: _log("debug", message),
    info=lambda message: _log("info", message),
    warning=lambda message: _log("warning", message),
    error=lambda message: _log("error", message),
    critical=lambda message: _log("critical", message),
)


def is_enabled(level: str = "info") -> bool:
    """Check if the specified log level is currently enabled.

    For expensive debug logs on hot paths (such as per-chunk raw content),
    call is_enabled("debug") before formatting strings:
        if is_enabled("debug"):
            log.debug(f"expensive {data}")
    """
    if not _log_enabled:
        return False
    return LOG_LEVELS.get(level, LOG_LEVELS["info"]) >= _cached_log_level


# Exported public interface
__all__ = ["log", "set_log_level", "get_log_level", "LOG_LEVELS", "is_enabled"]
