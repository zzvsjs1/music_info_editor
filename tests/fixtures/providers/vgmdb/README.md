# VGMdb fixture notes

These compact fixtures model the public VGMdb HTML observed on 2026-09-05 at
`https://vgmdb.net/search?q=Final+Fantasy+V&type=album` and
`https://vgmdb.net/album/4019`.

The search shape uses album rows with `rel="rel_<id>"`, `/album/<id>` links,
and language-tagged `.albumtitle` spans. The detail shape uses
`#album_infobit_large` for release facts, `#collapse_credits` for role credits,
plus `#tlnav` and `#tracklist` language views containing `table.role` disc
tracklists. Automated
access can return either HTTP 403 or a Cloudflare challenge document, represented
by `cloudflare_challenge.html` without challenge tokens.

All values are shortened or invented. The fixtures contain no credentials,
cookies, personal data, challenge tokens, or artwork.
