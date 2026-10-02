"""Typed preferences awaiting validation and explicit publication by Settings."""

from dataclasses import dataclass, replace

from metadata_polisher.infrastructure.settings import AppSettings


@dataclass(frozen=True, slots=True)
class SettingsDraft:
    """Keep field types fixed even while their values fail domain validation.

    The QML map is only a presentation boundary. Python validators and settings
    construction use these attributes without inferring types from old values.
    """

    rename_enabled: bool
    template: str
    track_digits: int
    disc_digits: int
    preferred_language: str
    provider_id: str | None
    network_mode: str
    proxy_host: str
    proxy_port: int
    backup_enabled: bool
    backup_directory: str
    reports_enabled: bool
    reports_directory: str
    detailed_tracing: bool

    @classmethod
    def from_settings(cls, settings: AppSettings) -> SettingsDraft:
        return cls(
            rename_enabled=settings.rename.enabled,
            template=settings.rename.template,
            track_digits=settings.rename.minimum_track_digits,
            disc_digits=settings.rename.minimum_disc_digits,
            preferred_language=settings.matching.preferred_language,
            provider_id=settings.providers.selected_provider_id,
            network_mode=settings.network.mode,
            proxy_host=settings.network.proxy_host,
            proxy_port=settings.network.proxy_port,
            backup_enabled=settings.backup.enabled,
            backup_directory=settings.backup.directory,
            reports_enabled=settings.reports.enabled,
            reports_directory=settings.reports.directory,
            detailed_tracing=settings.diagnostics.detailed_tracing,
        )

    def to_qml(self) -> dict[str, str | int | bool | None]:
        """Export a detached map with the established frontend field names."""
        return {
            "renameEnabled": self.rename_enabled,
            "template": self.template,
            "trackDigits": self.track_digits,
            "discDigits": self.disc_digits,
            "preferredLanguage": self.preferred_language,
            "providerId": self.provider_id,
            "networkMode": self.network_mode,
            "proxyHost": self.proxy_host,
            "proxyPort": self.proxy_port,
            "backupEnabled": self.backup_enabled,
            "backupDirectory": self.backup_directory,
            "reportsEnabled": self.reports_enabled,
            "reportsDirectory": self.reports_directory,
            "detailedTracing": self.detailed_tracing,
        }

    def with_field(self, name: str, value: object) -> SettingsDraft | None:
        """Accept only the declared Qt value type; leave invalid drafts intact.

        Integer fields deliberately reject bool, despite Python's subclass
        relationship. Domain constraints such as port ranges remain the
        authoritative validators' responsibility, allowing inline error feedback.
        """
        if name == "providerId" and (value is None or isinstance(value, str)):
            return replace(self, provider_id=value)

        if type(value) is bool:
            match name:
                case "renameEnabled":
                    return replace(self, rename_enabled=value)
                case "backupEnabled":
                    return replace(self, backup_enabled=value)
                case "reportsEnabled":
                    return replace(self, reports_enabled=value)
                case "detailedTracing":
                    return replace(self, detailed_tracing=value)

        if type(value) is int:
            match name:
                case "trackDigits":
                    return replace(self, track_digits=value)
                case "discDigits":
                    return replace(self, disc_digits=value)
                case "proxyPort":
                    return replace(self, proxy_port=value)

        if type(value) is str:
            match name:
                case "template":
                    return replace(self, template=value)
                case "preferredLanguage":
                    return replace(self, preferred_language=value)
                case "networkMode":
                    return replace(self, network_mode=value)
                case "proxyHost":
                    return replace(self, proxy_host=value)
                case "backupDirectory":
                    return replace(self, backup_directory=value)
                case "reportsDirectory":
                    return replace(self, reports_directory=value)

        return None
