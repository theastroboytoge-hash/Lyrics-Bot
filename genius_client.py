"""
Search songs and fetch lyrics using the Genius API + Genius website.

- Song search uses the official Genius API (/search), which indexes song
  titles, artist names AND lyrics content, so it works well even when the
  user sends a partial lyric snippet.
- Genius's API does not return lyrics text directly (their terms of
  service), so we fetch the public song page and scrape the lyrics
  container, which is the standard, widely used approach.
"""

import re
import requests
from bs4 import BeautifulSoup

GENIUS_API_BASE = "https://api.genius.com"
_HEADERS_SCRAPE = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def search_songs(query: str, access_token: str, max_results: int = 30) -> list[dict]:
    """Search Genius for songs matching a title, artist, or lyric snippet.

    Returns a list of dicts: {id, title, artist, url}
    """
    if not access_token:
        raise RuntimeError("GENIUS_ACCESS_TOKEN is not configured.")

    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(
        f"{GENIUS_API_BASE}/search",
        headers=headers,
        params={"q": query},
        timeout=15,
    )
    resp.raise_for_status()
    hits = resp.json().get("response", {}).get("hits", [])

    results = []
    seen_ids = set()
    for hit in hits:
        result = hit.get("result", {})
        song_id = result.get("id")
        title = result.get("title")
        url = result.get("url")
        artist = (result.get("primary_artist") or {}).get("name")

        if not song_id or not title or not url or song_id in seen_ids:
            continue

        seen_ids.add(song_id)
        results.append(
            {
                "id": song_id,
                "title": title,
                "artist": artist or "Unknown Artist",
                "url": url,
            }
        )
        if len(results) >= max_results:
            break

    return results


def get_lyrics(genius_url: str) -> str | None:
    """Scrape the full lyrics text from a Genius song page.

    Returns None if the lyrics could not be found/parsed.
    """
    try:
        resp = requests.get(genius_url, headers=_HEADERS_SCRAPE, timeout=15)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.select("div[data-lyrics-container='true']")
    if not containers:
        return None

    parts = []
    for container in containers:
        for br in container.find_all("br"):
            br.replace_with("\n")
        # Drop annotation/ad elements that sometimes sit inside the container
        for tag in container.select("div[data-exclude-from-selection='true']"):
            tag.decompose()
        parts.append(container.get_text().strip())

    lyrics = "\n".join(p for p in parts if p).strip()
    lyrics = re.sub(r"\n{3,}", "\n\n", lyrics)
    return lyrics or None
