# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
import logging
import os
import sys
from datetime import datetime, timezone

from config import settings as _cfg


# Try to import JSON logger, fall back to standard if not available
try:
    from pythonjsonlogger import jsonlogger
    JSON_LOGGER_AVAILABLE = True
except ImportError:
    JSON_LOGGER_AVAILABLE = False


class StructuredJsonFormatter(jsonlogger.JsonFormatter if JSON_LOGGER_AVAILABLE else logging.Formatter):
    """Custom JSON formatter with consistent field names for Loki/Grafana."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)

        # Use ISO 8601 timestamp
        log_record['timestamp'] = datetime.now(timezone.utc).isoformat()
        log_record['level'] = record.levelname
        log_record['logger'] = record.name

        log_record.pop('levelname', None)
        log_record.pop('name', None)

        # Add source location for errors
        if record.levelno >= logging.ERROR:
            log_record['source'] = {
                'file': record.filename,
                'line': record.lineno,
                'function': record.funcName
            }


def setup_logging(service_name: str) -> logging.Logger:
    """The service's logger, its level from LOG_LEVEL (DEBUG, INFO, WARNING,
    ERROR, default INFO) and its format from LOG_FORMAT ('json' or 'text', the
    default)."""
    try:
        log_level = _cfg.logging.level.upper()
        log_format = _cfg.logging.format.lower()
    except Exception:
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        log_format = os.getenv('LOG_FORMAT', 'text').lower()

    logger = logging.getLogger(service_name)
    logger.setLevel(getattr(logging, log_level))
    logger.handlers = []

    # Choose formatter based on LOG_FORMAT
    if log_format == 'json' and JSON_LOGGER_AVAILABLE:
        formatter = StructuredJsonFormatter(
            '%(timestamp)s %(level)s %(name)s %(message)s'
        )
    else:
        # Fall back to text format
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        if log_format == 'json' and not JSON_LOGGER_AVAILABLE:
            print("Warning: LOG_FORMAT=json requested but python-json-logger not installed. Using text format.",
                  file=sys.stderr)

    # One handler, on the root logger, for these loggers and for the modules
    # that use logging.getLogger(__name__) (a handler per logger as well would
    # print every line twice). Configured once per process.
    root = logging.getLogger()
    if not getattr(root, '_nbody_configured', False):
        root_handler = logging.StreamHandler(sys.stdout)
        root_handler.setFormatter(formatter)
        root.addHandler(root_handler)
        root.setLevel(getattr(logging, log_level))
        # Third-party libraries at INFO would log every connection (pika logs
        # about a dozen lines per open/close. The health checks open one)
        for noisy in ('pika', 'botocore', 'boto3', 'urllib3', 'aiobotocore'):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        root._nbody_configured = True

    logger.info(f"Logging initialized for {service_name} at level {log_level}")

    return logger
