import os
import logging
import uuid
import time
import textwrap
import tempfile
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut
import lyricsgenius
import requests
from bs4 import BeautifulSoup
from mutagen import File as MutagenFile
import httpx

# -------------------- تنظیمات پایه --------------------
logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("TOKEN")
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
ENV = os.environ.get("ENV", "development")
PORT = int(os.environ.get("PORT", 8443))

if not TOKEN:
    raise ValueError("TOKEN environment variable is required!")
if not GENIUS_TOKEN:
    raise ValueError("GENIUS_TOKEN environment variable is required!")

WEBHOOK_URL = None
if ENV == "production":
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        WEBHOOK_URL = f"{render_url}/{TOKEN}"
    else:
        logging.warning("RENDER_EXTERNAL_URL not set; webhook will not be used.")

# تنظیم Genius با timeout بالاتر
genius = lyricsgenius.Genius(
    GENIUS_TOKEN,
    timeout=15,
    retries=3,
    sleep_time=1
)
genius.verbose = False

# کش
CACHE_TTL = 1800
search_cache = {}

def clean_old_cache():
    now = time.time()
    to_delete = [k for k, v in search_cache.items() if now - v['timestamp'] > CACHE_TTL]
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

# -------------------- دریافت متن و ژانر با retry --------------------
async def fetch_lyrics_and_genres(title, artist, genius_url=None, retries=2):
    lyrics = None
    genres = []

    for attempt in range(retries + 1):
        try:
            # ۱. Genius
            song = genius.search_song(title, artist)
            if song and song.lyrics:
                lyrics = clean_lyrics(song.lyrics)
                break
        except Exception as e:
            logging.warning(f"Genius attempt {attempt+1} failed: {e}")
            if attempt == retries:
                logging.error("Genius failed after retries")
            await asyncio.sleep(1)

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

    # ۳. Lyrics.ovh
    if not lyrics:
        try:
            resp = requests.get(f"https://api.lyrics.ovh/v1/{artist}/{title}", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if 'lyrics' in data:
                    lyrics = clean_lyrics(data['lyrics'])
        except Exception as e:
            logging.error(f"Lyrics.ovh error: {e}")

    # ۴. Scrape
    if not lyrics and genius_url:
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            page = requests.get(genius_url, headers=headers, timeout=10)
            soup = BeautifulSoup(page.text, 'html.parser')
            lyrics_div = soup.find('div', class_='Lyrics__Container') or \
                         soup.find('div', class_='lyrics') or \
                         soup.find('div', {'data-lyrics-container': True})
            if lyrics_div:
                text = lyrics_div.get_text(separator='\n')
                lyrics = clean_lyrics(text)
        except Exception as e:
            logging.error(f"Scrape error: {e}")

    # ۵. Last.fm Genres
    if LASTFM_API_KEY:
        try:
            url = f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptags&artist={artist}&api_key={LASTFM_API_KEY}&format=json"
            resp = requests.get(url, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                tags = data.get('toptags', {}).get('tag', [])
                if tags:
                    genres = [tag['name'] for tag in tags[:10]]
        except Exception as e:
            logging.error(f"Last.fm error: {e}")

    return lyrics, genres[:5] if genres else []

# -------------------- بقیه توابع (تغییرات جزئی) --------------------
async def send_results_page(update_or_query, search_id, page):
    clean_old_cache()
    data = search_cache.get(search_id)
    if not data:
        text = "⏳ Search session expired. Please search again."
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(text, parse_mode=None)
        else:
            await update_or_query.edit_message_text(text, parse_mode=None)
        return

    # ... (بقیه کد send_results_page بدون تغییر)
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
        await update_or_query.message.reply_text(text, reply_markup=reply_markup, parse_mode=None)
    else:
        await update_or_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=None)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    clean_old_cache()
    if not query_text:
        return

    try:
        search_results = genius.search(query_text, per_page=10, page=1)
        hits = []
        if 'sections' in search_results and len(search_results['sections']) > 0:
            hits = search_results['sections'][0].get('hits', [])
        if not hits and 'hits' in search_results:
            hits = search_results['hits']

        if not hits:
            await update.message.reply_text("😕 No results found. Try a different query.", parse_mode=None)
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
        logging.error(e, exc_info=True)
        await update.message.reply_text("⚠️ An error occurred. Please try again later.", parse_mode=None)

# هندلرها (بدون تغییر اساسی)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Welcome to Lyrics & Genre Bot!\n\n"
        "Send me a song name, artist name, or any part of the lyrics.\n"
        "OR send me an audio file (MP3, M4A, etc.) and I'll extract its metadata and search for you!\n"
        "Use the inline buttons to navigate through results.\n\n"
        "Let's go!",
        parse_mode=None
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    if query:
        await perform_search(update, context, query)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (کد قبلی handle_audio بدون تغییر)
    audio = update.message.audio
    if not audio:
        document = update.message.document
        if document and document.mime_type and document.mime_type.startswith('audio/'):
            file_obj = document
            file_name = document.file_name
        else:
            await update.message.reply_text("❌ Please send a valid audio file.", parse_mode=None)
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

        await update.message.reply_text(f"🎵 Extracted: {title} by {artist}", parse_mode=None)
        query_text = f"{title} {artist}"
        await perform_search(update, context, query_text)

    except Exception as e:
        logging.error(f"Audio metadata extraction error: {e}")
        await update.message.reply_text("⚠️ Could not read audio metadata. Trying with file name...", parse_mode=None)
        title = os.path.splitext(file_name)[0]
        await perform_search(update, context, title)

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
            await query.edit_message_text("⏳ Search session expired. Please search again.", parse_mode=None)
            return

        song = next((s for s in cache['songs'] if str(s['id']) == song_id), None)
        if not song:
            await query.edit_message_text("❌ Song not found. Please search again.", parse_mode=None)
            return

        await query.edit_message_text(f"⏳ Fetching lyrics and genres for {song['title']} by {song['artist']}...", parse_mode=None)

        lyrics, genres = await fetch_lyrics_and_genres(song['title'], song['artist'], song['url'])

        if not lyrics and not genres:
            await query.edit_message_text("❌ No lyrics and no genres found for this song.", parse_mode=None)
            return

        chat_id = query.message.chat_id

        response_text = ""
        if lyrics:
            response_text += f"📝 Lyrics:\n{lyrics}"
        else:
            response_text += "📝 Lyrics not found."

        if genres:
            genre_lines = "\n".join([f"• {g}" for g in genres[:5]])
            response_text += f"\n\n🎶 Genres:\n{genre_lines}"

        if len(response_text) > 4096:
            chunks = split_text(response_text, 4000)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await query.edit_message_text(chunk, parse_mode=None)
                else:
                    await context.bot.send_message(chat_id=chat_id, text=chunk, parse_mode=None)
        else:
            await query.edit_message_text(response_text, parse_mode=None)

    elif parts[0] == 'page':
        search_id = parts[1]
        page = int(parts[2])
        await send_results_page(query, search_id, page)

    elif data == 'ignore':
        pass

# -------------------- اجرای اصلی --------------------
async def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & \
                                           filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    application.add_handler(MessageHandler(filters.Document.AUDIO, handle_audio))
    application.add_handler(CallbackQueryHandler(handle_callback))

    if ENV == "production" and WEBHOOK_URL:
        await application.bot.set_webhook(WEBHOOK_URL)
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
        )
    else:
        await application.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
