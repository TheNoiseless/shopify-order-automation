import json
import logging
from datetime import datetime, timezone
from typing import Any


LOGGER_NAME = "shopify_order_automation"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", record.getMessage()),
            "status": getattr(record, "status", "unknown"),
        }
        for field in ("order_id", "error_type"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def configure_logging() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not any(getattr(handler, "shopify_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.shopify_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger,
    event: str,
    status: str,
    *,
    order_id: int | None = None,
    error: BaseException | None = None,
) -> None:
    extra = {
        "event": event,
        "status": status,
        "order_id": order_id,
        "error_type": type(error).__name__ if error else None,
    }
    level = logging.ERROR if error else logging.INFO
    logger.log(
        level,
        event,
        extra=extra,
        exc_info=error is not None,
    )
