"""
Look up genres for an artist/track using two free sources so that
coverage stays high even when one source has no data:

1. Spotify Web API (Client Credentials flow) - artist-level genres.
2. Last.fm API - community tags on the track, falling back to the
   artist, which behave like crowd-sourced genres and often fill gaps
   Spotify leaves (especially for less mainstream artists).
"""

import time
import requests

_spotify_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_spotify_token(client_id: str, client_secret: str) -> str | None:
    if not client_id or not client_secret:
        return None
    if _spotify_token_cache["token"] and time.time() < _spotify_token_cache["expires_at"]:
        return _spotify_token_cache["token"]

    try:
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret),
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return None

    data = resp.json()
    _spotify_token_cache["token"] = data.get("access_token")
    _spotify_token_cache["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return _spotify_token_cache["token"]


def get_genres_spotify(artist_name: str, client_id: str, client_secret: str, limit: int = 5) -> list[str]:
    token = _get_spotify_token(client_id, client_secret)
    if not token or not artist_name:
        return []

    try:
        resp = requests.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": artist_name, "type": "artist", "limit": 1},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException:
        return []

    items = resp.json().get("artists", {}).get("items", [])
    if not items:
        return []
    genres = items[0].get("genres", [])
    return [g.title() for g in genres[:limit]]


def get_genres_lastfm(artist_name: str, track_name: str, api_key: str, limit: int = 5) -> list[str]:
    if not api_key or not artist_name:
        return []

    def _fetch(method: str, extra_params: dict) -> list[dict]:
        try:
            resp = requests.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={"method": method, "api_key": api_key, "format": "json", **extra_params},
                timeout=15,
            )
            data = resp.json()
        except (requests.RequestException, ValueError):
            return []
        return data.get("toptags", {}).get("tag", []) or []

    tags = []
    if track_name:
        tags = _fetch("track.gettoptags", {"artist": artist_name, "track": track_name})
    if not tags:
        tags = _fetch("artist.gettoptags", {"artist": artist_name})

    names = []
    for t in tags:
        name = (t.get("name") or "").strip()
        # Last.fm tags sometimes contain non-genre folksonomy noise (e.g. "seen live")
        if name and not name.lower() in {"seen live", "favorites", "favourite"}:
            names.append(name.title())
        if len(names) >= limit:
            break
    return names


def get_genres(
    artist_name: str,
    track_name: str,
    spotify_client_id: str,
    spotify_client_secret: str,
    lastfm_api_key: str,
    limit: int = 5,
) -> list[str]:
    """Best-effort merge of genres from Spotify and Last.fm, deduplicated."""
    genres = get_genres_spotify(artist_name, spotify_client_id, spotify_client_secret, limit)

    if len(genres) < limit:
        for g in get_genres_lastfm(artist_name, track_name, lastfm_api_key, limit):
            if g not in genres:
                genres.append(g)
            if len(genres) >= limit:
                break

    return genres[:limit]
