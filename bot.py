import logging
from html import escape

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from genius_client import get_lyrics, search_songs
from genre_client import get_genres

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("music_bot")


WELCOME_TEXT = (
    "👋 <b>Welcome to Lyrics &amp; Genre Finder Bot!</b>\n\n"
    "Send me a <b>song name</b>, an <b>artist name</b>, or even a snippet of "
    "<b>lyrics</b>, and I'll search the web to find matching songs.\n\n"
    "I'll show you up to 5 results at a time, with buttons to browse more.\n\n"
    "Tap any result to get:\n"
    "📝 The full lyrics (tap the text to copy it)\n"
    "🎧 At least 5 genres (tap to copy)\n\n"
    "Give it a try — send me a song right now! 🎵"
)


def build_results_keyboard(results: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * config.RESULTS_PER_PAGE
    end = start + config.RESULTS_PER_PAGE
    page_results = results[start:end]
    total_pages = (len(results) - 1) // config.RESULTS_PER_PAGE + 1

    rows = []
    for i, song in enumerate(page_results, start=start):
        label = f"{song['title']} — {song['artist']}"
        if len(label) > 60:
            label = label[:57] + "..."
        rows.append([InlineKeyboardButton(label, callback_data=f"s|{i}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"p|{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
    if end < len(results):
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"p|{page + 1}"))
    rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


def chunk_text(text: str, max_len: int) -> list[str]:
    """Split text into chunks under max_len, preferring to break on newlines."""
    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            # A single line longer than max_len must be hard-split.
            if len(line) > max_len:
                for i in range(0, len(line), max_len):
                    chunks.append(line[i : i + max_len])
                current = ""
            else:
                current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT, parse_mode=ParseMode.HTML)


async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    try:
        results = search_songs(query, config.GENIUS_ACCESS_TOKEN, config.MAX_RESULTS)
    except requests.RequestException:
        logger.exception("Search request failed")
        await update.message.reply_text(
            "⚠️ The search service is temporarily unavailable. Please try again in a moment."
        )
        return

    if not results:
        await update.message.reply_text(
            "😕 No results found. Try a different song name, artist, or lyric snippet."
        )
        return

    context.user_data["results"] = results
    context.user_data["page"] = 0

    await update.message.reply_text(
        f"🔎 Results for: <b>{escape(query)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=build_results_keyboard(results, 0),
    )


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data == "noop":
        return

    results = context.user_data.get("results")
    if not results:
        await query.edit_message_text("⚠️ This search has expired. Please send the song name again.")
        return

    if data.startswith("p|"):
        page = int(data.split("|", 1)[1])
        context.user_data["page"] = page
        await query.edit_message_reply_markup(reply_markup=build_results_keyboard(results, page))
        return

    if data.startswith("s|"):
        index = int(data.split("|", 1)[1])
        if index >= len(results):
            return
        await send_song_details(update, context, results[index])
        return


async def send_song_details(update: Update, context: ContextTypes.DEFAULT_TYPE, song: dict) -> None:
    chat = update.effective_chat
    await chat.send_action(ChatAction.TYPING)

    # --- Lyrics ---
    try:
        lyrics = get_lyrics(song["url"])
    except Exception:
        logger.exception("Lyrics fetch failed")
        lyrics = None

    header = f"🎵 <b>{escape(song['title'])}</b> — {escape(song['artist'])}"
    await chat.send_message(header, parse_mode=ParseMode.HTML)

    if lyrics:
        for chunk in chunk_text(lyrics, config.LYRICS_CHUNK_SIZE):
            await chat.send_message(f"<code>{escape(chunk)}</code>", parse_mode=ParseMode.HTML)
    else:
        await chat.send_message(f"😕 Lyrics for this song were not found.")

    # --- Genres ---
    await chat.send_action(ChatAction.TYPING)
    try:
        genres = get_genres(
            song["artist"],
            song["title"],
            config.SPOTIFY_CLIENT_ID,
            config.SPOTIFY_CLIENT_SECRET,
            config.LASTFM_API_KEY,
            limit=config.GENRE_LIMIT,
        )
    except Exception:
        logger.exception("Genre fetch failed")
        genres = []

    if genres:
        genre_lines = "\n".join(f"• {g}" for g in genres)
        await chat.send_message(
            f"🎧 <b>Genres:</b>\n<code>{escape(genre_lines)}</code>", parse_mode=ParseMode.HTML
        )
    else:
        await chat.send_message("😕 Genre was not found.")


def main() -> None:
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN environment variable is required.")
    if not config.GENIUS_ACCESS_TOKEN:
        raise SystemExit("GENIUS_ACCESS_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(config.TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))

    if config.WEBHOOK_URL:
        logger.info("Starting in webhook mode on port %s", config.PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.TELEGRAM_BOT_TOKEN,
            webhook_url=f"{config.WEBHOOK_URL.rstrip('/')}/{config.TELEGRAM_BOT_TOKEN}",
        )
    else:
        logger.info("Starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
