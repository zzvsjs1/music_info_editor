import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from metadata_polisher.infrastructure.logging_setup import (
    RedactingFormatter,
    configure_logging,
    redact_sensitive_text,
)


def test_redaction_removes_complete_basic_authorisation_credential() -> None:
    redacted = redact_sensitive_text("Authorization: Basic dXNlcjpwYXNzd29yZA==")

    assert redacted == "Authorization: [REDACTED]"
    assert "dXNlcjpwYXNzd29yZA" not in redacted


def test_configure_logging_creates_rotating_redacted_log_under_logs_dir(
    tmp_path: Path,
) -> None:
    logs_dir = tmp_path / "portable-app" / "logs"
    logger = configure_logging(logs_dir, logger_name="metadata_polisher.test.logging")

    logger.info(
        "Provider request Authorization: Bearer top-secret Cookie=session=cookie-secret"
    )

    rotating_handlers = [
        handler for handler in logger.handlers if isinstance(handler, RotatingFileHandler)
    ]
    assert len(rotating_handlers) == 1
    handler = rotating_handlers[0]
    # Read the real rotating log after flushing: inspecting only a LogRecord
    # would miss secrets introduced during interpolation or final formatting.
    handler.flush()

    log_file = Path(handler.baseFilename)
    assert log_file.parent == logs_dir
    assert handler.maxBytes > 0
    assert handler.backupCount > 0
    assert log_file.is_file()

    contents = log_file.read_text(encoding="utf-8")
    assert "Provider request" in contents
    assert "[REDACTED]" in contents
    assert "top-secret" not in contents
    assert "cookie-secret" not in contents


@pytest.mark.parametrize("header", ["Cookie", "Set-Cookie"])
def test_cookie_redaction_covers_the_entire_header_value_including_later_assignments(header):
    text = f"Request failed\n{header}: session=first-secret; second=other-secret; Path=/private\nStatus: 503"

    redacted = redact_sensitive_text(text)

    assert "first-secret" not in redacted
    assert "other-secret" not in redacted
    assert "/private" not in redacted
    assert "Request failed" in redacted
    assert "Status: 503" in redacted
    assert f"{header}: [REDACTED]" in redacted


@pytest.mark.parametrize("key", ["Authorization", "token", "password", "secret", "api_key"])
# Redaction must leave parseable JSON, not merely hide the sentinel string;
# diagnostic readers still need the safe fields surrounding a secret.
def test_quoted_json_credentials_are_redacted_without_destroying_safe_json_fields(key):
    encoded = json.dumps({key: "sensitive-value", "status": "retrying", "count": 2})

    redacted = redact_sensitive_text(encoded)

    assert "sensitive-value" not in redacted
    assert json.loads(redacted) == {key: "[REDACTED]", "status": "retrying", "count": 2}
    assert redact_sensitive_text(redacted) == redacted


def test_url_secret_assignments_are_redacted_while_safe_query_values_remain_visible():
    text = "Lookup failed for https://catalogue.example/search?token=secret-token&password=secret-password&page=2"

    redacted = redact_sensitive_text(text)

    assert "secret-token" not in redacted
    assert "secret-password" not in redacted
    assert "page=2" in redacted
    assert "Lookup failed" in redacted


def test_redacting_formatter_covers_exception_text_and_interpolated_arguments():
    error = ValueError('Cookie: a=first-secret; second=second-secret\n{"Authorization": "Bearer third-secret"}')
    record = logging.LogRecord(
        "provider", logging.ERROR, "provider.py", 1,
        "Lookup failed: password=%s", ("argument-secret",), (type(error), error, None),
    )

    redacted = RedactingFormatter("%(message)s").format(record)

    assert "Lookup failed" in redacted
    assert "ValueError" in redacted
    assert all(secret not in redacted for secret in (
        "first-secret", "second-secret", "third-secret", "argument-secret",
    ))


def test_formatter_retains_valid_trace_json_when_a_safe_string_contains_a_redacted_header():
    document = {"event": "provider_error", "details": {"message": "Cookie: [REDACTED]", "status": 503}}
    record = logging.LogRecord("trace", logging.DEBUG, "trace.py", 1, json.dumps(document), (), None)

    formatted = RedactingFormatter("%(levelname)s %(message)s").format(record)

    assert json.loads(formatted.removeprefix("DEBUG ")) == document


@pytest.mark.parametrize("userinfo", [
    "PROXY_USER_SENTINEL:PROXY_PASSWORD_SENTINEL", "USER%40SENTINEL:PASSWORD%2FSENTINEL",
])
def test_error_formatter_redacts_complete_proxy_url_userinfo(userinfo):
    error = ValueError(f"Could not connect to http://{userinfo}@127.0.0.1:8080/?page=2")
    record = logging.LogRecord("metadata_polisher", logging.ERROR, "network.py", 1,
                               "Operation failed: %s", (error,), None)

    formatted = RedactingFormatter("%(message)s").format(record)

    assert all(part not in formatted for part in userinfo.split(":"))
    assert "127.0.0.1:8080" in formatted
    assert "page=2" in formatted
    assert "Operation failed" in formatted


@pytest.mark.parametrize("header", ["Proxy-Authorization", "Authorization", "Cookie", "Set-Cookie"])
def test_raw_http_header_tuple_representation_does_not_export_credentials(header):
    text = (
        f"receive_response_headers.complete return_value=(b'HTTP/1.1', 200, [(b'{header}', b'HEADER_SENTINEL')])"
    )

    formatted = redact_sensitive_text(text)

    assert "HEADER_SENTINEL" not in formatted
    assert "200" in formatted
    assert header in formatted


def test_httpx_proxy_representation_does_not_export_session_username():
    text = "Proxy('http://127.0.0.1:8080', auth=('PROXY_USERNAME_SENTINEL', '********'))"

    formatted = redact_sensitive_text(text)

    assert "PROXY_USERNAME_SENTINEL" not in formatted
    assert "127.0.0.1:8080" in formatted


@pytest.mark.parametrize("tag", ["password", "access_token", "refresh_token", "authorization_code"])
def test_xml_error_body_redacts_secret_elements_without_losing_status(tag):
    text = f'<error><status>403</status><{tag} encoding="text">XML_SENTINEL</{tag}></error>'

    formatted = redact_sensitive_text(text)

    assert "XML_SENTINEL" not in formatted
    assert "<status>403</status>" in formatted


def test_detailed_application_logging_does_not_enable_httpx_or_httpcore_logs(tmp_path):
    logger = configure_logging(tmp_path, logger_name="metadata_polisher.test.network_separation",
                               detailed_tracing=True)
    handler = next(item for item in logger.handlers if isinstance(item, RotatingFileHandler))

    try:
        logger.debug("Safe semantic provider stage")
        logging.getLogger("httpx").info("Unfiltered request URL QUERY_SENTINEL")
        logging.getLogger("httpcore.http11").debug("Raw response header COOKIE_SENTINEL")
        handler.flush()
        contents = Path(handler.baseFilename).read_text(encoding="utf-8")

        assert "Safe semantic provider stage" in contents
        assert "QUERY_SENTINEL" not in contents
        assert "COOKIE_SENTINEL" not in contents
    finally:
        logger.removeHandler(handler)
        handler.close()
