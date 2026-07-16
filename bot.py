import os
import logging
import uuid
import time
import textwrap
import tempfile
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import lyricsgenius
import requests
from bs4 import BeautifulSoup
from mutagen import File as MutagenFile

# -------------------- تنظیمات پایه --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

TOKEN = os.environ.get("TOKEN")
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
ENV = os.environ.get("ENV", "development")
PORT = int(os.environ.get("PORT", 8443))

if not TOKEN or not GENIUS_TOKEN:
    raise ValueError("TOKEN and GENIUS_TOKEN environment variables are required!")

WEBHOOK_URL = None
if ENV == "production":
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        WEBHOOK_URL = f"{render_url}/{TOKEN}"

# Genius setup
genius = lyricsgenius.Genius(
    GENIUS_TOKEN,
    timeout=20,
    retries=3,
    sleep_time=2,
    remove_section_headers=True
)
genius.verbose = False

CACHE_TTL = 1800
search_cache = {}

def clean_old_cache():
    now = time.time()
    to_delete = [k for k, v in search_cache.items() if now - v.get('timestamp', 0) > CACHE_TTL]
    for k in to_delete:
        del search_cache[k]

def generate_search_id():
    return str(uuid.uuid4())

def clean_lyrics(text):
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        lower = line.lower()
        if "lyrics" in lower and len(line) < 30:
            continue
        if "embed" in lower or "share" in lower:
            continue
        if "you might also like" in lower:
            continue
        cleaned.append(line.strip())
    return '\n'.join(cleaned).strip()

def split_text(text, max_len=4000):
    paragraphs = text.split('\n')
    chunks = []
    current_chunk = ""
    for para in paragraphs:
        if len(para) > max_len:
            wrapped = textwrap.fill(para, width=max_len, break_long_words=False)
            for line in wrapped.split('\n'):
                if len(current_chunk) + len(line) + 1 > max_len:
                    chunks.append(current_chunk)
                    current_chunk = line
                else:
                    current_chunk += ('\n' + line) if current_chunk else line
        else:
            if len(current_chunk) + len(para) + 1 > max_len:
                chunks.append(current_chunk)
                current_chunk = para
            else:
                current_chunk += ('\n' + para) if current_chunk else para
    if current_chunk:
        chunks.append(current_chunk)
    return chunks

# -------------------- دریافت lyrics و genres --------------------
async def fetch_lyrics_and_genres(title, artist, genius_url=None):
    lyrics = None
    genres = []

    # Genius API
    try:
        song = genius.search_song(title, artist)
        if song and song.lyrics:
            lyrics = clean_lyrics(song.lyrics)
    except Exception as e:
        logging.error(f"Genius error: {e}")

    # Fallback Genius
    if not lyrics:
        try:
            results = genius.search(title + " " + artist, per_page=1)
            if results and 'sections' in results and len(results['sections']) > 0:
                hits = results['sections'][0].get('hits', [])
                if hits:
                    song_id = hits[0]['result']['id']
                    full_song = genius.song(song_id, get_lyrics=True)
                    if full_song and full_song.lyrics:
                        lyrics = clean_lyrics(full_song.lyrics)
        except Exception as e:
            logging.error(f"Genius fallback error: {e}")

    # Lyrics.ovh
    if not lyrics:
        try:
            resp = requests.get(f"https://api.lyrics.ovh/v1/{artist}/{title}", timeout=12)
            if resp.status_code == 200:
                data = resp.json()
                if 'lyrics' in data:
                    lyrics = clean_lyrics(data['lyrics'])
        except Exception as e:
            logging.error(f"Lyrics.ovh error: {e}")

    # Last.fm Genres
    if LASTFM_API_KEY:
        try:
            url = f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptags&artist={artist}&api_key={LASTFM_API_KEY}&format=json"
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                tags = data.get('toptags', {}).get('tag', [])
                if tags:
                    genres = [tag['name'] for tag in tags[:8]]
        except Exception:
            pass

    return lyrics, genres[:5] if genres else []

# -------------------- نمایش نتایج جستجو --------------------
async def send_results_page(update_or_query, search_id, page):
    clean_old_cache()
    data = search_cache.get(search_id)
    if not data:
        text = "⏳ Search session expired. Please search again."
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text)
        else:
            await update_or_query.edit_message_text(text)
        return

    songs = data['songs']
    total = data['total']
    page_size = 5
    start = page * page_size
    end = min(start + page_size, total)
    page_songs = songs[start:end]
    total_pages = (total + page_size - 1) // page_size

    keyboard = []
    for song in page_songs:
        button_text = f"{song['title']} - {song['artist']}"
        callback_data = f"select_{search_id}_{song['id']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{search_id}_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(f"Page {page+1}/{total_pages}", callback_data="ignore"))
    if end < total:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{search_id}_{page+1}"))
    keyboard.append(nav_buttons)

    reply_markup = InlineKeyboardMarkup(keyboard)
    text = "🔍 Search results:"

    if isinstance(update_or_query, Update) and update_or_query.message:
        await update_or_query.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update_or_query.edit_message_text(text, reply_markup=reply_markup)

# -------------------- جستجو --------------------
async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    clean_old_cache()
    if not query_text:
        return

    try:
        search_results = genius.search(query_text, per_page=10)
        hits = []
        if 'sections' in search_results and len(search_results['sections']) > 0:
            hits = search_results['sections'][0].get('hits', [])
        if not hits and 'hits' in search_results:
            hits = search_results['hits']

        if not hits:
            await update.message.reply_text("😕 No results found.")
            return

        song_list = []
        for hit in hits[:10]:
            song = hit['result']
            song_list.append({
                'id': song['id'],
                'title': song['title'],
                'artist': song['primary_artist']['name'],
                'url': song['url']
            })

        search_id = generate_search_id()
        search_cache[search_id] = {
            'songs': song_list,
            'total': len(song_list),
            'timestamp': time.time()
        }
        await send_results_page(update, search_id, 0)

    except Exception as e:
        logging.error(f"Search error: {e}")
        await update.message.reply_text("⚠️ An error occurred. Please try again.")

# -------------------- هندلرها --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Welcome to Lyrics Bot!\n\n"
        "Send song name or audio file."
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text:
        await perform_search(update, context, update.message.text.strip())

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    audio = update.message.audio
    if not audio:
        document = update.message.document
        if document and document.mime_type and document.mime_type.startswith('audio/'):
            file_obj = document
            file_name = document.file_name
        else:
            await update.message.reply_text("❌ Please send a valid audio file.")
            return
    else:
        file_obj = audio
        file_name = audio.file_name or "audio.mp3"

    file_id = file_obj.file_id
    new_file = await context.bot.get_file(file_id)
    
    suffix = os.path.splitext(file_name)[1] or '.mp3'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
        temp_path = temp_file.name
        await new_file.download_to_drive(temp_path)

    try:
        meta = MutagenFile(temp_path)
        title = None
        artist = None

        if meta and hasattr(meta, 'tags'):
            if 'TIT2' in meta.tags:
                title = str(meta.tags['TIT2'])
            elif 'title' in meta.tags:
                title = str(meta.tags['title'])
            if 'TPE1' in meta.tags:
                artist = str(meta.tags['TPE1'])
            elif 'artist' in meta.tags:
                artist = str(meta.tags['artist'])
            if not artist and 'TPE2' in meta.tags:
                artist = str(meta.tags['TPE2'])

        if not title:
            title = os.path.splitext(file_name)[0]
        if not artist:
            artist = "Unknown Artist"

        await update.message.reply_text(f"🎵 Extracted: {title} by {artist}")
        await perform_search(update, context, f"{title} {artist}")

    except Exception as e:
        logging.error(f"Audio metadata error: {e}")
        await update.message.reply_text("⚠️ Metadata failed. Searching with filename...")
        await perform_search(update, context, os.path.splitext(file_name)[0])

    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split('_')

    if parts[0] == 'select':
        search_id = parts[1]
        song_id = parts[2]
        clean_old_cache()
        cache = search_cache.get(search_id)
        if not cache:
            await query.edit_message_text("⏳ Session expired.")
            return

        song = next((s for s in cache['songs'] if str(s['id']) == song_id), None)
        if not song:
            await query.edit_message_text("❌ Song not found.")
            return

        await query.edit_message_text(f"⏳ Fetching {song['title']}...")

        lyrics, genres = await fetch_lyrics_and_genres(song['title'], song['artist'], song['url'])

        response = f"📝 Lyrics:\n{lyrics}\n" if lyrics else "📝 Lyrics not found.\n"
        if genres:
            response += "\n🎶 Genres:\n" + "\n".join(f"• {g}" for g in genres)

        if len(response) > 4096:
            for chunk in split_text(response, 4000):
                await context.bot.send_message(query.message.chat_id, chunk)
            await query.edit_message_text("✅ Lyrics sent.")
        else:
            await query.edit_message_text(response)

    elif parts[0] == 'page':
        await send_results_page(query, parts[1], int(parts[2]))

# -------------------- اجرای اصلی --------------------
async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & \
                                        filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    application.add_handler(CallbackQueryHandler(handle_callback))

    if ENV == "production" and WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logging.info("Webhook activated")
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            drop_pending_updates=True
        )
    else:
        await application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped")
    except Exception as e:
        logging.error(f"Critical error: {e}")
