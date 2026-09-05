"""Typed JSON settings with explicit leaf-level validation and defaults."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import cast

SETTINGS_SCHEMA_VERSION = 1
DEFAULT_RENAME_TEMPLATE = "[%discnumber%.]%tracknumber%. %title%"


@dataclass(frozen=True)
class GeneralSettings:
    """General desktop state that is safe to retain between sessions."""

    last_root_folder: str = ""


@dataclass(frozen=True)
class RenameSettings:
    """Filename preview and number-rendering preferences."""

    enabled: bool = True
    template: str = DEFAULT_RENAME_TEMPLATE
    minimum_track_digits: int = 2
    minimum_disc_digits: int = 1


@dataclass(frozen=True)
class MatchingSettings:
    """User preference used when ranking provider language variants."""

    preferred_language: str = "auto"


@dataclass(frozen=True)
class ProvidersSettings:
    """One explicit catalogue choice; None keeps all provider access disabled."""

    selected_provider_id: str | None = "musicbrainz_direct"

    def __post_init__(self) -> None:
        # Unknown strings deliberately survive loading and saving. A future or
        # removed adapter is a visible configuration problem, never a fallback.
        if self.selected_provider_id is not None and not isinstance(self.selected_provider_id, str):
            raise TypeError("selected_provider_id must be a string or None")


@dataclass(frozen=True)
class NetworkSettings:
    """Only non-secret route preferences are eligible for JSON persistence."""

    mode: str = "direct"
    proxy_host: str = ""
    proxy_port: int = 8080

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str) or not isinstance(self.proxy_host, str):
            raise TypeError("Network mode and proxy host must be strings")

        if type(self.proxy_port) is not int:
            raise TypeError("Proxy port must be an integer")


@dataclass(frozen=True)
class BackupSettings:
    """Optional permanent-backup policy."""

    enabled: bool = False
    directory: str = ""


@dataclass(frozen=True)
class ReportsSettings:
    """Optional processing-report policy."""

    enabled: bool = False
    directory: str = ""


@dataclass(frozen=True)
class DiagnosticsSettings:
    """Diagnostic verbosity retained between runs."""

    detailed_tracing: bool = False


@dataclass(frozen=True)
class UiSettings:
    """Versioned logical geometry and column preferences, without Qt storage."""

    version: int = 1
    geometry: tuple[int, ...] = ()
    maximised: bool = False
    horizontal_sizes: tuple[int, ...] = ()
    vertical_sizes: tuple[int, ...] = ()
    column_widths: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    hidden_columns: Mapping[str, tuple[int, ...]] = field(default_factory=dict)
    dialog_sizes: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("column_widths", "hidden_columns", "dialog_sizes"):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))


@dataclass(frozen=True)
class AppSettings:
    """Complete typed settings document consumed outside the JSON boundary."""

    schema_version: int = SETTINGS_SCHEMA_VERSION
    general: GeneralSettings = field(default_factory=GeneralSettings)
    rename: RenameSettings = field(default_factory=RenameSettings)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    providers: ProvidersSettings = field(default_factory=ProvidersSettings)
    network: NetworkSettings = field(default_factory=NetworkSettings)
    backup: BackupSettings = field(default_factory=BackupSettings)
    reports: ReportsSettings = field(default_factory=ReportsSettings)
    diagnostics: DiagnosticsSettings = field(default_factory=DiagnosticsSettings)
    ui: UiSettings = field(default_factory=UiSettings)
    external_tools: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != SETTINGS_SCHEMA_VERSION:
            raise ValueError(f"Unsupported settings schema_version: {self.schema_version}")

        # Copying prevents a caller from mutating session settings through a dictionary
        # retained after construction, while preserving natural mapping access.
        object.__setattr__(self, "external_tools", MappingProxyType(dict(self.external_tools)))


@dataclass(frozen=True)
class SettingsLoadResult:
    """Loaded session settings plus recoverable validation information."""

    settings: AppSettings
    warnings: tuple[str, ...] = ()
    error: str | None = None


_MISSING = object()


def _invalid_warning(path: str) -> str:
    return f"Invalid value for '{path}'; using the default."


def _read_section(
    document: Mapping[str, object],
    key: str,
    warnings: list[str],
    *,
    warning_path: str | None = None,
) -> Mapping[str, object]:
    value = document.get(key, _MISSING)

    if value is _MISSING:
        return {}

    if not isinstance(value, dict):
        warnings.append(_invalid_warning(warning_path or key))
        return {}

    return cast(dict[str, object], value)


def _read_bool(
    section: Mapping[str, object],
    key: str,
    default: bool,
    path: str,
    warnings: list[str],
) -> bool:
    value = section.get(key, _MISSING)

    if value is _MISSING:
        return default

    if type(value) is bool:
        return value

    warnings.append(_invalid_warning(path))
    return default


def _read_string(
    section: Mapping[str, object],
    key: str,
    default: str,
    path: str,
    warnings: list[str],
    *,
    allow_empty: bool = True,
) -> str:
    value = section.get(key, _MISSING)

    if value is _MISSING:
        return default

    if type(value) is str and (allow_empty or bool(value)):
        return value

    warnings.append(_invalid_warning(path))
    return default


def _read_non_negative_int(
    section: Mapping[str, object],
    key: str,
    default: int,
    path: str,
    warnings: list[str],
) -> int:
    value = section.get(key, _MISSING)

    if value is _MISSING:
        return default

    # bool is a subclass of int in Python, so an exact type check is required for
    # predictable validation of JSON values such as `true`.
    if type(value) is int and value >= 0:
        return value

    warnings.append(_invalid_warning(path))
    return default


def _read_positive_int(
    section: Mapping[str, object],
    key: str,
    default: int,
    path: str,
    warnings: list[str],
) -> int:
    value = section.get(key, _MISSING)

    if value is _MISSING:
        return default

    if type(value) is int and value > 0:
        return value

    warnings.append(_invalid_warning(path))
    return default


def _parse_providers(
    section: Mapping[str, object],
    warnings: list[str],
) -> ProvidersSettings:
    # The explicit selection takes precedence over legacy enabled flags. Even
    # an unknown saved ID must survive rather than silently choosing a provider.
    if "selected_provider_id" in section:
        selected = section["selected_provider_id"]

        if selected is not None and not isinstance(selected, str):
            warnings.append("Invalid selected provider value; online lookup is disabled until Settings is corrected.")
            return ProvidersSettings(None)

        if selected not in {None, "musicbrainz_direct", "vgmdb"}:
            warnings.append("The stored selected provider is unavailable. Choose an implemented provider in Settings.")

        return ProvidersSettings(selected)

    if not any(key in section for key in ("musicbrainz", "vgmdb")):
        return ProvidersSettings()

    enabled: dict[str, bool] = {}

    for key, default_priority in (("musicbrainz", 100), ("vgmdb", 90)):
        path = f"providers.{key}"
        legacy = _read_section(section, key, warnings, warning_path=path)
        enabled[key] = _read_bool(legacy, "enabled", True, f"{path}.enabled", warnings)
        _read_non_negative_int(legacy, "priority", default_priority, f"{path}.priority", warnings)

    # MusicBrainz takes precedence by the migration policy. VGMdb is the only
    # other implemented legacy adapter, so its priority cannot create a tie.
    selected = "musicbrainz_direct" if enabled["musicbrainz"] else "vgmdb" if enabled["vgmdb"] else None
    label = "MusicBrainz Direct" if selected == "musicbrainz_direct" else "VGMdb" if selected else "local editing only"
    warnings.append(f"Legacy provider settings migrated to {label}. Only the selected provider will be contacted.")
    return ProvidersSettings(selected)


def _parse_network(document: Mapping[str, object], warnings: list[str]) -> NetworkSettings:
    if "network" in document and not isinstance(document["network"], dict):
        warnings.append("Invalid network settings; online access is blocked until the route is corrected.")
        return NetworkSettings(mode="invalid")

    raw = _read_section(document, "network", warnings)
    mode = raw.get("mode", "direct")
    host = raw.get("proxy_host", "")
    port = raw.get("proxy_port", 8080)

    # Invalid persisted routing must never turn a requested proxy into direct
    # access. Preserve valid strings/numbers for correction; block other shapes.
    if not isinstance(mode, str) or not isinstance(host, str) or type(port) is not int:
        warnings.append("Invalid network settings; online access is blocked until the route is corrected.")
        return NetworkSettings(mode="invalid")

    if mode not in {"direct", "manual_proxy"}:
        warnings.append("The stored network mode is unavailable. Correct the route in Settings.")

    return NetworkSettings(mode, host, port)


def _parse_external_tools(document: Mapping[str, object], warnings: list[str]) -> Mapping[str, str]:
    value = document.get("external_tools", _MISSING)

    if value is _MISSING:
        return {}

    if not isinstance(value, dict):
        warnings.append(_invalid_warning("external_tools"))
        return {}

    tools: dict[str, str] = {}

    for name, configured_path in cast(dict[str, object], value).items():
        if type(configured_path) is str:
            tools[name] = configured_path
        else:
            warnings.append(_invalid_warning(f"external_tools.{name}"))

    return tools


def _parse_ui(document: Mapping[str, object], warnings: list[str]) -> UiSettings:
    section = _read_section(document, "ui", warnings)

    if section.get("version", 1) != 1:
        warnings.append(_invalid_warning("ui.version"))
        return UiSettings()

    def numbers(value: object, key: str, *, length: int | None = None, minimum: int = 0) -> tuple[int, ...]:
        if value is None:
            return ()

        if (isinstance(value, list) and (length is None or len(value) in {0, length})
                and len(value) <= 32 and all(type(item) is int and minimum <= item <= 100000 for item in value)):
            return tuple(value)

        warnings.append(_invalid_warning(f"ui.{key}"))
        return ()

    def mapping(key: str, *, length: int | None = None) -> Mapping[str, tuple[int, ...]]:
        raw = _read_section(section, key, warnings, warning_path=f"ui.{key}")
        return {name: numbers(value, f"{key}.{name}", length=length) for name, value in raw.items()}

    return UiSettings(
        geometry=numbers(section.get("geometry"), "geometry", length=4, minimum=-100000),
        maximised=_read_bool(section, "maximised", False, "ui.maximised", warnings),
        horizontal_sizes=numbers(section.get("horizontal_sizes"), "horizontal_sizes", length=2),
        vertical_sizes=numbers(section.get("vertical_sizes"), "vertical_sizes", length=2),
        column_widths=mapping("column_widths"), hidden_columns=mapping("hidden_columns"),
        dialog_sizes=mapping("dialog_sizes", length=2),
    )


def _parse_settings(document: Mapping[str, object]) -> tuple[AppSettings, tuple[str, ...]]:
    warnings: list[str] = []
    defaults = AppSettings()
    general = _read_section(document, "general", warnings)
    rename = _read_section(document, "rename", warnings)
    matching = _read_section(document, "matching", warnings)
    providers = _read_section(document, "providers", warnings)
    backup = _read_section(document, "backup", warnings)
    reports = _read_section(document, "reports", warnings)
    diagnostics = _read_section(document, "diagnostics", warnings)

    # Validate leaf values independently so one damaged preference does not
    # discard unrelated valid settings. Route/provider parsing stays conservative.
    settings = AppSettings(
        general=GeneralSettings(
            last_root_folder=_read_string(
                general,
                "last_root_folder",
                defaults.general.last_root_folder,
                "general.last_root_folder",
                warnings,
            )
        ),
        rename=RenameSettings(
            enabled=_read_bool(rename, "enabled", defaults.rename.enabled, "rename.enabled", warnings),
            template=_read_string(rename, "template", defaults.rename.template, "rename.template", warnings),
            minimum_track_digits=_read_positive_int(
                rename,
                "minimum_track_digits",
                defaults.rename.minimum_track_digits,
                "rename.minimum_track_digits",
                warnings,
            ),
            minimum_disc_digits=_read_positive_int(
                rename,
                "minimum_disc_digits",
                defaults.rename.minimum_disc_digits,
                "rename.minimum_disc_digits",
                warnings,
            ),
        ),
        matching=MatchingSettings(
            preferred_language=_read_string(
                matching,
                "preferred_language",
                defaults.matching.preferred_language,
                "matching.preferred_language",
                warnings,
                allow_empty=False,
            )
        ),
        providers=_parse_providers(providers, warnings),
        network=_parse_network(document, warnings),
        backup=BackupSettings(
            enabled=_read_bool(backup, "enabled", defaults.backup.enabled, "backup.enabled", warnings),
            directory=_read_string(backup, "directory", defaults.backup.directory, "backup.directory", warnings),
        ),
        reports=ReportsSettings(
            enabled=_read_bool(reports, "enabled", defaults.reports.enabled, "reports.enabled", warnings),
            directory=_read_string(reports, "directory", defaults.reports.directory, "reports.directory", warnings),
        ),
        diagnostics=DiagnosticsSettings(
            detailed_tracing=_read_bool(
                diagnostics,
                "detailed_tracing",
                defaults.diagnostics.detailed_tracing,
                "diagnostics.detailed_tracing",
                warnings,
            )
        ),
        external_tools=_parse_external_tools(document, warnings),
        ui=_parse_ui(document, warnings),
    )

    return settings, tuple(warnings)


def _load_error(message: str) -> SettingsLoadResult:
    return SettingsLoadResult(settings=AppSettings(), warnings=(message,), error=message)


def load_settings(path: Path) -> SettingsLoadResult:
    """Load settings without ever rewriting a missing, invalid, or corrupt source."""
    if not path.exists():
        return SettingsLoadResult(settings=AppSettings())

    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return _load_error(f"Could not read settings: {error}")

    try:
        raw_document: object = json.loads(contents)
    except json.JSONDecodeError as error:
        return _load_error(f"Settings JSON is corrupt: {error.msg}")

    if not isinstance(raw_document, dict):
        return _load_error("Settings JSON root must be an object.")

    document = cast(dict[str, object], raw_document)
    schema_version = document.get("schema_version", _MISSING)

    if type(schema_version) is not int:
        return _load_error("Settings schema_version is required and must be an integer.")

    if schema_version != SETTINGS_SCHEMA_VERSION:
        return _load_error(f"Unsupported settings schema_version: {schema_version}.")

    settings, warnings = _parse_settings(document)
    return SettingsLoadResult(settings=settings, warnings=warnings)


def save_settings(path: Path, settings: AppSettings) -> None:
    """Serialise typed settings to the explicitly supplied application path."""
    # Serialise an explicit allowlist. Session credentials and new runtime-only
    # fields must not enter settings merely because a dataclass later gains them.
    document = {
        "schema_version": settings.schema_version,
        "general": {"last_root_folder": settings.general.last_root_folder},
        "rename": {
            "enabled": settings.rename.enabled,
            "template": settings.rename.template,
            "minimum_track_digits": settings.rename.minimum_track_digits,
            "minimum_disc_digits": settings.rename.minimum_disc_digits,
        },
        "matching": {"preferred_language": settings.matching.preferred_language},
        "providers": {
            "selected_provider_id": settings.providers.selected_provider_id,
        },
        "network": {
            "mode": settings.network.mode,
            "proxy_host": settings.network.proxy_host,
            "proxy_port": settings.network.proxy_port,
        },
        "backup": {
            "enabled": settings.backup.enabled,
            "directory": settings.backup.directory,
        },
        "reports": {
            "enabled": settings.reports.enabled,
            "directory": settings.reports.directory,
        },
        "diagnostics": {"detailed_tracing": settings.diagnostics.detailed_tracing},
        "external_tools": dict(settings.external_tools),
        "ui": {
            "version": settings.ui.version,
            "geometry": settings.ui.geometry,
            "maximised": settings.ui.maximised,
            "horizontal_sizes": settings.ui.horizontal_sizes,
            "vertical_sizes": settings.ui.vertical_sizes,
            "column_widths": dict(settings.ui.column_widths),
            "hidden_columns": dict(settings.ui.hidden_columns),
            "dialog_sizes": dict(settings.ui.dialog_sizes),
        },
    }
    encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    path.write_text(encoded, encoding="utf-8")
