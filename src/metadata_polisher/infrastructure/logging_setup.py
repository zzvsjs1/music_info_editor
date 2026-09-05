"""Human-readable rotating application logging confined to the portable folder."""

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "metadata-polisher.log"
DEFAULT_MAX_LOG_BYTES = 1_048_576
DEFAULT_BACKUP_COUNT = 3

_SENSITIVE_KEY = (
    r"(?:proxy[_-]?authori[sz]ation|authori[sz]ation(?:[_-]?code)?|(?:set[_-]?)?cookie|"
    r"api[_-]?key|credentials?|[a-z0-9_-]*(?:token|password|passwd|secret)[a-z0-9_-]*)"
)
_SENSITIVE_NAME = re.compile(_SENSITIVE_KEY, re.IGNORECASE)
_QUOTED_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])"
    r"(?P<prefix>(?P<key_quote>['\"]?)(?P<key>" + _SENSITIVE_KEY + r")(?P=key_quote)\s*[:=]\s*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')"
)
_SENSITIVE_HEADER = re.compile(
    r"(?i)(?<![\w-])"
    r"(?P<key>proxy[_-]?authori[sz]ation|authori[sz]ation|(?:set[_-]?)?cookie)"
    r"(?P<separator>\s*[:=][ \t]*)"
    r"(?P<value>[^\r\n]*)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(?<![\w-])(?P<key>" + _SENSITIVE_KEY + r")"
    r"(?P<separator>\s*[:=]\s*)"
    r"(?P<value>\[REDACTED\]|[^\s,;&}\]\"']+)"
)
_URL_USERINFO = re.compile(
    r"(?i)(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@<>\"']+@"
)
# HTTPX/httpcore representations use Python string/bytes literals, not header
# assignment syntax. Match only the literal forms that carry known secrets;
# never evaluate a diagnostic string as Python or walk arbitrary client state.
_STRING_LITERAL = r"(?:[bB]?\"(?:\\.|[^\"\\])*\"|[bB]?'(?:\\.|[^'\\])*')"
_SENSITIVE_HEADER_TUPLE = re.compile(
    r"(?i)(?P<prefix>\(\s*b?(?P<quote>['\"])(?:" + _SENSITIVE_KEY + r")(?P=quote)\s*,\s*)"
    r"(?P<value>" + _STRING_LITERAL + r")"
)
_PROXY_AUTH_TUPLE = re.compile(
    r"(?i)(?<![\w-])(?P<prefix>auth\s*=\s*)\(\s*" + _STRING_LITERAL
    + r"\s*,\s*" + _STRING_LITERAL + r"\s*\)"
)
_SENSITIVE_XML_ELEMENT = re.compile(
    r"(?P<opening><(?P<key>" + _SENSITIVE_KEY + r")\b[^>]*>)"
    r".*?(?P<closing></(?P=key)\s*>)",
    re.IGNORECASE | re.DOTALL,
)


def _redact_json_value(value: object) -> object:
    """Redact JSON values without mistaking a string's content for log syntax."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _SENSITIVE_NAME.fullmatch(str(key)) else _redact_json_value(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]

    if isinstance(value, str):
        return redact_sensitive_text(value)

    return value


def redact_sensitive_text(text: str) -> str:
    """Replace common credential/header assignments before text reaches disk."""
    # The final formatter receives JSON after strings have been escaped. Treat a
    # complete document as structured data so a Cookie header inside a message
    # cannot consume its closing quote and the remaining safe trace fields.
    for start_match in re.finditer(r"(?<!\S)[\[{]", text):
        start = start_match.start()
        prefix = text[:start]

        if _SENSITIVE_HEADER.search(prefix.rsplit("\n", 1)[-1]):
            # A raw header may itself contain JSON. It remains one sensitive
            # header value rather than becoming a separate diagnostic document.
            continue

        try:
            document, consumed = json.JSONDecoder().raw_decode(text[start:])
        except json.JSONDecodeError:
            continue

        redacted_document = _redact_json_value(document)
        encoded = (
            text[start:start + consumed]
            if redacted_document == document
            else json.dumps(redacted_document, ensure_ascii=False)
        )
        return redact_sensitive_text(prefix) + encoded + redact_sensitive_text(text[start + consumed:])

    def replace_quoted(match: re.Match[str]) -> str:
        # Preserve the original string delimiters so structured trace messages
        # stay valid JSON after the formatter's final redaction pass.
        quote = match.group("value")[0]
        return f"{match.group('prefix')}{quote}[REDACTED]{quote}"

    def replace(match: re.Match[str]) -> str:
        return f"{match.group('key')}{match.group('separator')}[REDACTED]"

    def replace_header_tuple(match: re.Match[str]) -> str:
        value = match.group("value")
        literal_prefix = value[:2] if value[0] in "bB" else value[0]
        return f"{match.group('prefix')}{literal_prefix}[REDACTED]{literal_prefix[-1]}"

    # A proxy username belongs to its RAM-only credentials too. Remove complete
    # URL userinfo and both members of HTTPX's auth tuple, including usernames
    # left visible by HTTPX's own password-masked Proxy representation.
    userinfo_redacted = _URL_USERINFO.sub(r"\g<scheme>[REDACTED]@", text)
    auth_redacted = _PROXY_AUTH_TUPLE.sub(r"\g<prefix>[REDACTED]", userinfo_redacted)
    tuples_redacted = _SENSITIVE_HEADER_TUPLE.sub(replace_header_tuple, auth_redacted)
    xml_redacted = _SENSITIVE_XML_ELEMENT.sub(r"\g<opening>[REDACTED]\g<closing>", tuples_redacted)
    quoted_redacted = _QUOTED_SENSITIVE_ASSIGNMENT.sub(replace_quoted, xml_redacted)

    # Cookie headers contain several semicolon-separated credentials and may
    # also carry private attributes. Their complete line is one sensitive value;
    # other assignments stop at query/value separators to retain safe context.
    headers_redacted = _SENSITIVE_HEADER.sub(replace, quoted_redacted)
    return _SENSITIVE_ASSIGNMENT.sub(replace, headers_redacted)


class RedactingFormatter(logging.Formatter):
    """Redact the complete formatted record, including rendered exception text."""

    # Redact after interpolation and exception formatting, because sensitive
    # values may arrive through logging arguments or an exception traceback.
    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))


def configure_logging(
    logs_dir: Path,
    *,
    logger_name: str = "metadata_polisher",
    detailed_tracing: bool = False,
) -> logging.Logger:
    """Configure and return the application logger for one explicit log directory."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG if detailed_tracing else logging.INFO)
    logger.propagate = False

    # Reconfiguration can happen in tests or after changing diagnostic settings.
    # Remove only rotating file handlers owned by this configuration boundary.
    for existing_handler in tuple(logger.handlers):
        if isinstance(existing_handler, RotatingFileHandler):
            logger.removeHandler(existing_handler)
            existing_handler.close()

    handler = RotatingFileHandler(
        logs_dir / LOG_FILE_NAME,
        maxBytes=DEFAULT_MAX_LOG_BYTES,
        backupCount=DEFAULT_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG if detailed_tracing else logging.INFO)
    handler.setFormatter(
        RedactingFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)

    return logger
