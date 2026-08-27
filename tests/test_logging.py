from __future__ import annotations

import json
import logging

from bigbot.logging_config import JsonFormatter, configure_logging


def test_json_formatter_includes_structured_context() -> None:
    record = logging.LogRecord(
        "bigbot.test",
        logging.INFO,
        __file__,
        1,
        "posted %s",
        ("item",),
        None,
    )
    record.event = "item_posted"
    record.feed_id = 7
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "posted item"
    assert payload["event"] == "item_posted"
    assert payload["feed_id"] == 7
    assert "timestamp" in payload


def test_configure_logging_sets_levels() -> None:
    configure_logging("DEBUG")
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("discord").level == logging.INFO
