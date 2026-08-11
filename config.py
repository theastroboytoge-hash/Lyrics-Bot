import os

# --- Required ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN")

# --- Genre sources (at least one recommended, both is best) ---
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
LASTFM_API_KEY = os.environ.get("LASTFM_API_KEY")

# --- Deployment (leave WEBHOOK_URL empty to run in polling mode, e.g. on Termux) ---
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
PORT = int(os.environ.get("PORT", 8080))

# --- Behaviour ---
RESULTS_PER_PAGE = 5
MAX_RESULTS = 30
GENRE_LIMIT = 5
LYRICS_CHUNK_SIZE = 3500
