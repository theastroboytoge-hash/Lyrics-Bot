import os
import logging
import uuid
import time
import textwrap
import tempfile
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import lyricsgenius
import requests
from bs4 import BeautifulSoup
from mutagen import File as MutagenFile
import asyncio
import sys

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TOKEN = os.environ.get("TOKEN")
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")
ENV = os.environ.get("ENV", "development")
PORT = int(os.environ.get("PORT", 8443))

if not TOKEN or not GENIUS_TOKEN:
    raise ValueError("TOKEN and GENIUS_TOKEN are required!")

WEBHOOK_URL = None
if ENV == "production":
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        WEBHOOK_URL = f"{render_url}/{TOKEN}"

genius = lyricsgenius.Genius(GENIUS_TOKEN, timeout=20, retries=3, sleep_time=2)
genius.verbose = False

search_cache = {}
CACHE_TTL = 1800

def clean_old_cache():
    now = time.time()
    to_delete = [k for k, v in search_cache.items() if now - v.get('timestamp', 0) > CACHE_TTL]
    for k in to_delete:
        search_cache.pop(k, None)

def generate_search_id():
    return str(uuid.uuid4())

def clean_lyrics(text):
    lines = text.split('\n')
    cleaned = []
    for line in lines:
        lower = line.lower()
        if ("lyrics" in lower and len(line) < 30) or "embed" in lower or "share" in lower or "you might also like" in lower:
            continue
        cleaned.append(line.strip())
    return '\n'.join(cleaned).strip()

def split_text(text, max_len=4000):
    paragraphs = text.split('\n')
    chunks = []
    current = ""
    for para in paragraphs:
        if len(para) > max_len:
            for line in textwrap.fill(para, width=max_len).split('\n'):
                if len(current) + len(line) + 1 > max_len:
                    chunks.append(current)
                    current = line
                else:
                    current += ('\n' + line) if current else line
        else:
            if len(current) + len(para) + 1 > max_len:
                chunks.append(current)
                current = para
            else:
                current += ('\n' + para) if current else para
    if current:
        chunks.append(current)
    return chunks

async def fetch_lyrics_and_genres(title, artist):
    lyrics = None
    genres = []

    try:
        song = genius.search_song(title, artist)
        if song and song.lyrics:
            lyrics = clean_lyrics(song.lyrics)
    except Exception as e:
        logging.error(f"Genius error: {e}")

    if not lyrics:
        try:
            results = genius.search(title + " " + artist, per_page=1)
            if results and 'sections' in results and results['sections']:
                hits = results['sections'][0].get('hits', [])
                if hits:
                    full_song = genius.song(hits[0]['result']['id'], get_lyrics=True)
                    if full_song and full_song.lyrics:
                        lyrics = clean_lyrics(full_song.lyrics)
        except Exception as e:
            logging.error(f"Genius fallback: {e}")

    if not lyrics:
        try:
            resp = requests.get(f"https://api.lyrics.ovh/v1/{artist}/{title}", timeout=10)
            if resp.status_code == 200 and 'lyrics' in resp.json():
                lyrics = clean_lyrics(resp.json()['lyrics'])
        except Exception:
            pass

    if LASTFM_API_KEY:
        try:
            r = requests.get(f"http://ws.audioscrobbler.com/2.0/?method=artist.gettoptags&artist={artist}&api_key={LASTFM_API_KEY}&format=json", timeout=8)
            if r.status_code == 200:
                tags = r.json().get('toptags', {}).get('tag', [])
                genres = [t['name'] for t in tags[:8]]
        except Exception:
            pass

    return lyrics, genres[:5]

async def send_results_page(update_or_query, search_id, page):
    clean_old_cache()
    data = search_cache.get(search_id)
    if not data:
        msg = "⏳ Session expired."
        if isinstance(update_or_query, Update):
            await update_or_query.message.reply_text(msg)
        else:
            await update_or_query.edit_message_text(msg)
        return

    songs = data['songs']
    total = data['total']
    ps = 5
    start = page * ps
    end = min(start + ps, total)
    total_pages = (total + ps - 1) // ps

    keyboard = [[InlineKeyboardButton(f"{s['title']} - {s['artist']}", callback_data=f"select_{search_id}_{s['id']}")] for s in songs[start:end]]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{search_id}_{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="ignore"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{search_id}_{page+1}"))
    keyboard.append(nav)

    markup = InlineKeyboardMarkup(keyboard)
    if isinstance(update_or_query, Update):
        await update_or_query.message.reply_text("🔍 Results:", reply_markup=markup)
    else:
        await update_or_query.edit_message_text("🔍 Results:", reply_markup=markup)

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query_text: str):
    clean_old_cache()
    if not query_text:
        return
    try:
        res = genius.search(query_text, per_page=10)
        hits = res.get('sections', [{}])[0].get('hits', []) or res.get('hits', [])

        if not hits:
            await update.message.reply_text("😕 No results.")
            return

        songs = [{'id': h['result']['id'], 'title': h['result']['title'], 'artist': h['result']['primary_artist']['name'], 'url': h['result']['url']} for h in hits[:10]]

        sid = generate_search_id()
        search_cache[sid] = {'songs': songs, 'total': len(songs), 'timestamp': time.time()}
        await send_results_page(update, sid, 0)
    except Exception as e:
        logging.error(f"Search failed: {e}")
        await update.message.reply_text("⚠️ Error occurred.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎵 Lyrics Bot ready!\nSend song or audio.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await perform_search(update, context, update.message.text.strip())

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    obj = update.message.audio or update.message.document
    if not obj or (update.message.document and not update.message.document.mime_type.startswith('audio/')):
        await update.message.reply_text("❌ Send valid audio file.")
        return

    file_name = obj.file_name or "audio.mp3"
    new_file = await context.bot.get_file(obj.file_id)

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(file_name)[1] or '.mp3', delete=False) as f:
        path = f.name
        await new_file.download_to_drive(path)

    try:
        meta = MutagenFile(path)
        title = artist = None
        if meta and hasattr(meta, 'tags'):
            tags = meta.tags
            title = str(tags.get('TIT2') or tags.get('title') or os.path.splitext(file_name)[0])
            artist = str(tags.get('TPE1') or tags.get('artist') or tags.get('TPE2') or "Unknown Artist")

        await update.message.reply_text(f"🎵 {title} by {artist}")
        await perform_search(update, context, f"{title} {artist}")
    except Exception as e:
        logging.error(f"Metadata error: {e}")
        await perform_search(update, context, os.path.splitext(file_name)[0])
    finally:
        if os.path.exists(path):
            os.remove(path)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split('_')

    if parts[0] == 'select':
        sid, sid2 = parts[1], parts[2]
        cache = search_cache.get(sid)
        if not cache:
            await q.edit_message_text("Session expired.")
            return
        song = next((s for s in cache['songs'] if str(s['id']) == sid2), None)
        if not song:
            await q.edit_message_text("Song not found.")
            return

        await q.edit_message_text(f"⏳ Fetching {song['title']}...")
        lyrics, genres = await fetch_lyrics_and_genres(song['title'], song['artist'])

        text = f"📝 Lyrics for {song['title']}\n\n{lyrics or 'Not found.'}"
        if genres:
            text += "\n\n🎶 Genres:\n" + "\n".join(f"• {g}" for g in genres)

        if len(text) > 4096:
            for chunk in split_text(text):
                await context.bot.send_message(q.message.chat_id, chunk)
            await q.edit_message_text("✅ Sent.")
        else:
            await q.edit_message_text(text)

    elif parts[0] == 'page':
        await send_results_page(q, parts[1], int(parts[2]))

async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & \~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_callback))

    if WEBHOOK_URL:
        await app.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        await app.run_webhook(listen="0.0.0.0", port=PORT, url_path=TOKEN, drop_pending_updates=True)
    else:
        await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Fatal: {e}")
    finally:
        logging.info("Bot shutdown")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.stop()
        except:
            pass
