"""Detailed trace output is opt-in, deterministic, redacted and non-disruptive."""

import json
import logging

from metadata_polisher.infrastructure.diagnostic_trace import DetailedTraceSink


class RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def recording_logger():
    logger = logging.Logger("trace-test", logging.DEBUG)
    handler = RecordingHandler()
    logger.addHandler(handler)
    return logger, handler


def test_detailed_trace_is_disabled_by_default_and_does_not_inspect_details():
    # A hostile representation makes even an accidental inspection observable,
    # proving that disabled tracing returns before walking supplied details.
    class Uninspectable:
        def __repr__(self):
            raise AssertionError("Disabled diagnostics must not inspect details")

    logger, handler = recording_logger()
    DetailedTraceSink(logger).record("LOOKUP-0001", "provider_search", Uninspectable())

    assert handler.records == []


def test_enabled_trace_writes_structured_debug_json_with_redacted_nested_details():
    logger, handler = recording_logger()
    sink = DetailedTraceSink(logger, enabled=True)
    sink.record("LOOKUP-0001", "candidate_scored", {
        "title": "序曲",
        "Authorization": "Bearer top-secret",
        "evidence": [{"score": 87.5, "reason": "TITLE_MATCH", "access_token": "nested-secret"}],
    })

    assert len(handler.records) == 1
    record = handler.records[0]
    assert record.levelno == logging.DEBUG
    text = record.getMessage()
    assert "top-secret" not in text
    assert "nested-secret" not in text
    document = json.loads(text)
    assert document["operation_id"] == "LOOKUP-0001"
    assert document["event"] == "candidate_scored"
    assert document["details"]["title"] == "序曲"
    assert document["details"]["Authorization"] == "[REDACTED]"
    assert document["details"]["evidence"] == [{
        "score": 87.5, "reason": "TITLE_MATCH", "access_token": "[REDACTED]",
    }]


def test_equivalent_trace_details_produce_identical_json_despite_dictionary_insertion_order():
    logger, handler = recording_logger()
    sink = DetailedTraceSink(logger, enabled=True)
    sink.record("LOOKUP-0001", "ranking", {"z": 2, "nested": {"b": 2, "a": 1}, "a": "safe"})
    sink.record("LOOKUP-0001", "ranking", {"a": "safe", "nested": {"a": 1, "b": 2}, "z": 2})

    assert handler.records[0].getMessage() == handler.records[1].getMessage()


def test_trace_logging_failure_does_not_escape_into_the_processing_operation():
    # Simulate an unavailable log destination at the handler boundary; a trace
    # failure must never become a metadata-operation failure.
    class FailedHandler(logging.Handler):
        def emit(self, record):
            raise OSError("The logging destination is unavailable")

    logger = logging.Logger("failed-trace-test", logging.DEBUG)
    logger.addHandler(FailedHandler())

    DetailedTraceSink(logger, enabled=True).record("APPLY-0001", "preflight_complete", {"files": 3})


def test_trace_sanitising_failure_does_not_escape_into_the_processing_operation(monkeypatch):
    logger, handler = recording_logger()

    def failed_sanitiser(_value):
        raise ValueError("Unexpected diagnostic value")

    monkeypatch.setattr(
        "metadata_polisher.infrastructure.diagnostic_trace.sanitise_diagnostic_data", failed_sanitiser
    )
    DetailedTraceSink(logger, enabled=True).record("APPLY-0001", "preflight_complete", {"files": 3})

    assert handler.records == []
