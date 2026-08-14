import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
from datetime import UTC, datetime

from src.shared.runtime_paths import runtime_root


_STANDARD_LOG_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}

_SECRET_PATTERNS = [
    re.compile(r"(?i)(bot[_-]?token|api[_-]?hash|password|access[_-]?token|secret|authorization)\s*[:=]\s*['\"]?([^'\"\s&]+)"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram Bot token pattern
]


def redact_secrets(text: str) -> str:
    if not text:
        return text
    result = text
    for pattern in _SECRET_PATTERNS:
        if pattern.groups == 2:
            result = pattern.sub(r"\1=***REDACTED***", result)
        else:
            result = pattern.sub(r"***REDACTED_TOKEN***", result)
    return result


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_secrets(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_secrets(str(v)) if isinstance(v, str) else v for v in record.args)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "pid": os.getpid(),
            "message": redact_secrets(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_KEYS and not key.startswith("_"):
                payload[key] = redact_secrets(str(value)) if isinstance(value, str) else value
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


class StandardFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        formatted = super().format(record)
        return redact_secrets(formatted)


def app_log_path() -> Path:
    log_dir = runtime_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "app.log"


def configure_logging(level: str = "INFO", log_format: str = "plain", *, component: str | None = None, enable_file: bool = True) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level.upper())

    fmt_str = f"%(asctime)s [%(process)d] %(levelname)s [{component or '%(name)s'}]: %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    stream_handler = logging.StreamHandler()
    stream_handler.addFilter(SecretFilter())
    if log_format.lower() == "json":
        stream_handler.setFormatter(JsonFormatter())
    else:
        stream_handler.setFormatter(StandardFormatter(fmt_str, datefmt=date_fmt))
    root.addHandler(stream_handler)

    if enable_file:
        try:
            file_path = app_log_path()
            file_handler = RotatingFileHandler(
                file_path,
                maxBytes=5 * 1024 * 1024,  # 5 MB
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.addFilter(SecretFilter())
            if log_format.lower() == "json":
                file_handler.setFormatter(JsonFormatter())
            else:
                file_handler.setFormatter(StandardFormatter(fmt_str, datefmt=date_fmt))
            root.addHandler(file_handler)
        except Exception:
            pass

