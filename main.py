"""
Telegram Lyrics & Genre Finder Bot
-----------------------------------
- Runs as a web service (webhook mode) so it works on Render's free tier.
- 100% free data sources: iTunes Search API, lrclib.net, lyrics.ovh,
  Deezer API, MusicBrainz, and (optionally) Last.fm.
- No paid APIs required. Last.fm key is optional but recommended
  (free to get) since it greatly improves genre coverage.
"""

import os
import re
import html
import logging
from typing import List, Dict, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY", "")  # optional, free key
PORT = int(os.environ.get("PORT", "10000"))
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")  # auto-set by Render

RESULTS_PER_PAGE = 5
MAX_RESULTS = 25
HTTP_TIMEOUT = 8
# Keep well under Telegram's 4096 char limit to leave room for <pre> tags
# and HTML-escaping expansion (e.g. "&" -> "&amp;").
SAFE_CHUNK_LEN = 3500

# In-memory per-chat search state: chat_id -> {"query", "results", "page"}
search_cache: Dict[int, Dict] = {}

CUSTOM_UA = "LyricsGenreTelegramBot/1.0 (+https://render.com; contact: example@example.com)"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def clean_query(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def split_text(text: str, max_len: int = SAFE_CHUNK_LEN) -> List[str]:
    """Split long text into Telegram-safe chunks, breaking on line/word
    boundaries when possible."""
    text = text.strip()
    if len(text) <= max_len:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        parts.append(remaining)
    return parts


# --------------------------------------------------------------------------
# Data sources: search
# --------------------------------------------------------------------------

def search_songs(query: str) -> List[Dict]:
    """Search for songs using the free iTunes Search API (no key needed)."""
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={
                "term": query,
                "entity": "song",
                "limit": MAX_RESULTS,
                "media": "music",
            },
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("iTunes search failed: %s", e)
        return []

    seen = set()
    results = []
    for item in data.get("results", []):
        track = item.get("trackName")
        artist = item.get("artistName")
        if not track or not artist:
            continue
        key = (track.lower(), artist.lower())
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "track": track,
                "artist": artist,
                "genre": item.get("primaryGenreName"),
            }
        )
    return results


# --------------------------------------------------------------------------
# Data sources: lyrics
# --------------------------------------------------------------------------

def get_lyrics(artist: str, track: str) -> Optional[str]:
    """Try multiple free lyrics sources in order until one succeeds."""

    # 1) lrclib.net - free, no API key, large + up-to-date database
    try:
        resp = requests.get(
            "https://lrclib.net/api/search",
            params={"track_name": track, "artist_name": artist},
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": CUSTOM_UA},
        )
        if resp.ok:
            for item in resp.json():
                lyrics = item.get("plainLyrics")
                if lyrics and lyrics.strip():
                    return lyrics.strip()
    except Exception as e:
        logger.warning("lrclib lookup failed: %s", e)

    # 2) lyrics.ovh - free, no API key (fallback)
    try:
        url = f"https://api.lyrics.ovh/v1/{requests.utils.quote(artist)}/{requests.utils.quote(track)}"
        resp = requests.get(url, timeout=HTTP_TIMEOUT)
        if resp.ok:
            data = resp.json()
            lyrics = data.get("lyrics")
            if lyrics and lyrics.strip():
                return lyrics.strip()
    except Exception as e:
        logger.warning("lyrics.ovh lookup failed: %s", e)

    return None


# --------------------------------------------------------------------------
# Data sources: genres
# --------------------------------------------------------------------------

def get_genres(artist: str, track: str, itunes_genre: Optional[str] = None) -> List[str]:
    """Aggregate genres/tags from several free sources to maximize coverage."""
    genres: List[str] = []

    def add(g: Optional[str]):
        if not g:
            return
        g = g.strip()
        if not g:
            return
        for existing in genres:
            if existing.lower() == g.lower():
                return
        genres.append(g[0].upper() + g[1:] if len(g) > 1 else g.upper())

    add(itunes_genre)

    # Deezer - free, no API key
    try:
        resp = requests.get(
            "https://api.deezer.com/search",
            params={"q": f'artist:"{artist}" track:"{track}"'},
            timeout=HTTP_TIMEOUT,
        )
        if resp.ok:
            items = resp.json().get("data", [])
            if items:
                album_id = items[0].get("album", {}).get("id")
                if album_id:
                    resp2 = requests.get(
                        f"https://api.deezer.com/album/{album_id}", timeout=HTTP_TIMEOUT
                    )
                    if resp2.ok:
                        for g in resp2.json().get("genres", {}).get("data", []):
                            add(g.get("name"))
    except Exception as e:
        logger.warning("Deezer genre lookup failed: %s", e)

    # Last.fm - free API key (optional but recommended)
    if LASTFM_API_KEY:
        try:
            resp = requests.get(
                "https://ws.audioscrobbler.com/2.0/",
                params={
                    "method": "track.gettoptags",
                    "artist": artist,
                    "track": track,
                    "api_key": LASTFM_API_KEY,
                    "format": "json",
                },
                timeout=HTTP_TIMEOUT,
            )
            if resp.ok:
                tags = resp.json().get("toptags", {}).get("tag", [])
                for t in tags[:10]:
                    add(t.get("name"))
        except Exception as e:
            logger.warning("Last.fm track.gettoptags failed: %s", e)

        if len(genres) < 5:
            try:
                resp = requests.get(
                    "https://ws.audioscrobbler.com/2.0/",
                    params={
                        "method": "artist.gettoptags",
                        "artist": artist,
                        "api_key": LASTFM_API_KEY,
                        "format": "json",
                    },
                    timeout=HTTP_TIMEOUT,
                )
                if resp.ok:
                    tags = resp.json().get("toptags", {}).get("tag", [])
                    for t in tags[:10]:
                        add(t.get("name"))
            except Exception as e:
                logger.warning("Last.fm artist.gettoptags failed: %s", e)

    # MusicBrainz - free, no API key (requires a descriptive User-Agent)
    if len(genres) < 5:
        try:
            resp = requests.get(
                "https://musicbrainz.org/ws/2/recording",
                params={
                    "query": f'recording:"{track}" AND artist:"{artist}"',
                    "fmt": "json",
                    "limit": 1,
                },
                headers={"User-Agent": CUSTOM_UA},
                timeout=HTTP_TIMEOUT,
            )
            if resp.ok:
                recs = resp.json().get("recordings", [])
                if recs:
                    mbid = recs[0].get("id")
                    if mbid:
                        resp2 = requests.get(
                            f"https://musicbrainz.org/ws/2/recording/{mbid}",
                            params={"fmt": "json", "inc": "genres+tags"},
                            headers={"User-Agent": CUSTOM_UA},
                            timeout=HTTP_TIMEOUT,
                        )
                        if resp2.ok:
                            rdata = resp2.json()
                            for g in rdata.get("genres", []):
                                add(g.get("name"))
                            for t in rdata.get("tags", []):
                                add(t.get("name"))
        except Exception as e:
            logger.warning("MusicBrainz genre lookup failed: %s", e)

    return genres


# --------------------------------------------------------------------------
# Keyboard building
# --------------------------------------------------------------------------

def build_results_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    state = search_cache[chat_id]
    results = state["results"]
    page = state["page"]
    start = page * RESULTS_PER_PAGE
    end = start + RESULTS_PER_PAGE
    page_results = results[start:end]

    buttons = []
    for i, item in enumerate(page_results, start=start):
        label = f"{item['artist']} - {item['track']}"
        if len(label) > 60:
            label = label[:57] + "..."
        buttons.append([InlineKeyboardButton(label, callback_data=f"select|{i}")])

    total_pages = max(1, (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data="nav|prev"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data="nav|next"))
    buttons.append(nav_row)

    return InlineKeyboardMarkup(buttons)


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------

WELCOME_MESSAGE = (
    "👋 <b>Welcome to Lyrics &amp; Genre Finder Bot!</b>\n\n"
    "Here's how to use me:\n"
    "1️⃣ Send me a song name, an artist name, or even a snippet of lyrics.\n"
    "2️⃣ I'll search and show you up to 5 matching songs at a time. "
    "Use the ⬅️ / ➡️ buttons to browse more results.\n"
    "3️⃣ Tap a song to get its <b>full lyrics</b> and at least <b>5 genres</b>.\n\n"
    "💡 Lyrics and genres are sent as tap-to-copy blocks — just tap the text to copy it.\n\n"
    "Try it now — send me a song name! 🎵"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.HTML)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = clean_query(update.message.text)
    if not query:
        return
    chat_id = update.effective_chat.id

    searching_msg = await update.message.reply_text("🔎 Searching...")

    results = search_songs(query)

    if not results:
        await searching_msg.edit_text(
            "😕 No results found for your search.\n"
            "Try a different song name, artist, or lyrics snippet."
        )
        return

    search_cache[chat_id] = {"query": query, "results": results, "page": 0}
    keyboard = build_results_keyboard(chat_id)

    await searching_msg.edit_text(
        f"🔍 Results for: <b>{html.escape(query)}</b>\nTap a song to get lyrics &amp; genres:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    data = query.data

    if data == "noop":
        await query.answer()
        return

    if chat_id not in search_cache:
        await query.answer("This search has expired. Please search again.", show_alert=True)
        return

    if data.startswith("nav|"):
        direction = data.split("|", 1)[1]
        state = search_cache[chat_id]
        if direction == "next":
            state["page"] += 1
        elif direction == "prev":
            state["page"] = max(0, state["page"] - 1)
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=build_results_keyboard(chat_id))
        return

    if data.startswith("select|"):
        await query.answer("Fetching lyrics & genres...")
        index = int(data.split("|", 1)[1])
        state = search_cache[chat_id]
        results = state["results"]

        if index >= len(results):
            await context.bot.send_message(
                chat_id, "⚠️ This result is no longer available. Please search again."
            )
            return

        item = results[index]
        artist = item["artist"]
        track = item["track"]

        await context.bot.send_message(
            chat_id,
            f"🎵 <b>{html.escape(track)}</b>\n👤 {html.escape(artist)}",
            parse_mode=ParseMode.HTML,
        )

        lyrics = get_lyrics(artist, track)
        genres = get_genres(artist, track, item.get("genre"))

        # Lyrics
        if lyrics:
            for part in split_text(lyrics):
                escaped = html.escape(part)
                await context.bot.send_message(
                    chat_id, f"<pre>{escaped}</pre>", parse_mode=ParseMode.HTML
                )
        else:
            await context.bot.send_message(chat_id, "😕 Lyrics not found for this song.")

        # Genres
        if genres:
            genre_lines = "\n".join(f"• {g}" for g in genres[:15])
            escaped_genres = html.escape(genre_lines)
            await context.bot.send_message(
                chat_id,
                f"🎧 <b>Genres:</b>\n<pre>{escaped_genres}</pre>",
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(chat_id, "😕 Genres not found for this song.")
        return


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    application.add_handler(CallbackQueryHandler(handle_callback))

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL.rstrip('/')}/{BOT_TOKEN}"
        logger.info("Starting in webhook mode: %s", webhook_url)
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url,
        )
    else:
        logger.info("RENDER_EXTERNAL_URL not set - running in polling mode (local/dev use).")
        application.run_polling()


if __name__ == "__main__":
    main()
