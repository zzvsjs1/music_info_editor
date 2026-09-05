"""Local descriptions of the two implemented adapters, with no discovery."""


def provider_label(provider_id: str | None) -> str:
    if provider_id is None:
        return "None — local editing only"

    return {"musicbrainz_direct": "MusicBrainz Direct", "vgmdb": "VGMdb"}.get(
        provider_id, "Unavailable provider",
    )


# These local descriptions support Settings without sending a request. A
# capability description is not evidence that the provider is reachable now.
def provider_summary(provider_id: str | None) -> str:
    if provider_id == "musicbrainz_direct":
        return (
            "Public catalogue search, release/track titles, artists, dates and supported track composer relationships. "
            "Genre proposals are not supplied. No account or API key is required. "
            "Requests identify the application with a contact and run at most once per second. "
            "Live access has not been tested in this dialogue."
        )

    if provider_id == "vgmdb":
        return (
            "Specialist public album pages and multilingual track titles. "
            "Album composer credits are shown as evidence; they cannot automatically populate track composers. "
            "Genre proposals and verified totals are not supplied. No account or API key is supported by this adapter. "
            "Access can be blocked by browser challenges or HTTP 403. "
            "Live access has not been tested in this dialogue."
        )

    if provider_id is None:
        return (
            "Online lookup is disabled. Local scanning, manual editing and "
            "applying reviewed changes remain available."
        )

    return "The stored provider is unavailable. Choose an implemented provider to enable online lookup."
