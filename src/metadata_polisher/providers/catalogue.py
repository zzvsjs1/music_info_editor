"""Local descriptions of the two implemented adapters, with no discovery."""


def provider_label(provider_id: str | None, *, fallback: str = "Unavailable provider") -> str:
    if provider_id is None:
        return "None — local editing only"

    return {"musicbrainz_direct": "MusicBrainz Direct", "vgmdb": "VGMdb"}.get(
        provider_id, fallback,
    )


def provider_summary(provider_id: str | None) -> str:
    if provider_id == "musicbrainz_direct":
        return (
            "Find album and track titles, artists, dates and track composer credits in MusicBrainz. "
            "You can edit genres manually. No account is needed."
        )

    if provider_id == "vgmdb":
        return (
            "Find soundtrack albums and multilingual track titles on VGMdb. "
            "Album composer credits are available for reference. You can enter track composers and genres manually."
        )

    if provider_id is None:
        return (
            "Online lookup is disabled. Local scanning, manual editing and "
            "applying reviewed changes remain available."
        )

    return "The saved provider is unavailable. Choose another provider to search online."
