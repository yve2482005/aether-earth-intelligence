"""Logging setup for AETHER backend components.

Plain text lines by default. Set AETHER_LOG_JSON=1 for one-JSON-object-per-
record output (consumed by the API request-logging middleware); the level is
still driven by AETHER_LOG_LEVEL.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

_configured = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(name: str = "aether", level: str | None = None) -> logging.Logger:
    global _configured
    lvl = (level or os.environ.get("AETHER_LOG_LEVEL", "INFO")).upper()
    logger = logging.getLogger(name)
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        if os.environ.get("AETHER_LOG_JSON", "0") == "1":
            handler.setFormatter(_JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter(
                "%(asctime)sZ %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S"))
        logging.Formatter.converter = __import__("time").gmtime
        logger.addHandler(handler)
        logger.setLevel(getattr(logging, lvl, logging.INFO))
        _configured = True
    return logger
