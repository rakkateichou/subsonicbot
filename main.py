import hashlib
import os
import random
import requests
import sqlite3
import base64
import io
import sys
import logging
import threading
from dotenv import load_dotenv
from telebot import TeleBot, types
from flask import Flask, request, abort, Response, send_file
from mutagen.id3 import ID3, TIT2, TPE1, APIC, Encoding

# -------------------------------------------------------------
# ENVIRONMENT & LOGGING SETUP
# -------------------------------------------------------------
load_dotenv()

IS_DEBUG_MODE = os.getenv("BOT_DEBUG", "false").lower() in ("true", "1", "t", "yes", "y")

logging.basicConfig(
    level=logging.INFO if IS_DEBUG_MODE else logging.WARNING,
    format='%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("subsonicbot")
logger.setLevel(logging.DEBUG if IS_DEBUG_MODE else logging.WARNING)

if not IS_DEBUG_MODE:
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    print("Bot is starting up in SILENT mode. Set environment variable BOT_DEBUG=True in your .env to enable verbose logs.")
else:
    logger.info("Verbose/Debug logs are ENABLED.")

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

SILENT_MP3_BYTES = None

def prefetch_silent_mp3():
    global SILENT_MP3_BYTES
    try:
        logger.info("Prefetching silent MP3 placeholder from remote...")
        raw_bytes = requests.get("https://github.com/anars/blank-audio/raw/master/250-milliseconds-of-silence.mp3", timeout=10).content
        
        # Inject custom 'Loading...' ID3 tags so Telegram's audio player shows 'Loading...'
        # while the bot downloads the actual song in the background.
        bio = io.BytesIO(raw_bytes)
        try:
            audio = ID3(bio)
        except Exception:
            audio = ID3()
        audio.add(TIT2(encoding=3, text=["Loading..."]))
        audio.add(TPE1(encoding=3, text=["Loading..."]))
        
        out_bio = io.BytesIO(raw_bytes)
        audio.save(out_bio)
        SILENT_MP3_BYTES = out_bio.getvalue()
        logger.info("Silent MP3 with 'Loading...' ID3 tags cached successfully.")
    except Exception as e:
        logger.warning(f"Failed to prefetch silent MP3 on startup: {e}. Will retry on-demand.")

threading.Thread(target=prefetch_silent_mp3, name="PrefetchThread", daemon=True).start()

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is not set! The bot cannot start.")
    raise ValueError("BOT_TOKEN environment variable is not set")
else:
    logger.info("BOT_TOKEN loaded successfully.")

if not WEBHOOK_URL:
    logger.warning("WEBHOOK_URL environment variable is not set. Webhooks require a public HTTPS URL.")
else:
    logger.info(f"WEBHOOK_URL loaded: {WEBHOOK_URL}")

bot = TeleBot(BOT_TOKEN)

BOT_USERNAME = "submusbot"
try:
    bot_info = bot.get_me()
    BOT_USERNAME = bot_info.username
    logger.info(f"Bot info loaded. Username: @{BOT_USERNAME}")
except Exception as get_me_err:
    logger.warning(f"Failed to fetch bot username dynamically: {get_me_err}. Defaulting to 'submusbot'")

try:
    bot.set_my_commands([
        types.BotCommand("start", "Start the bot and get a welcome message"),
        types.BotCommand("login", "Connect your Subsonic/Navidrome account"),
        types.BotCommand("account", "View your connected account details"),
        types.BotCommand("logout", "Disconnect your Subsonic/Navidrome account"),
        types.BotCommand("help", "Show the list of supported commands")
    ])
    logger.info("Bot commands registered successfully with Telegram.")
except Exception as set_cmd_err:
    logger.warning(f"Failed to set bot commands in Telegram: {set_cmd_err}")

# -------------------------------------------------------------
# DATABASE INITIALIZATION
# -------------------------------------------------------------
DATABASE_PATH = os.getenv("DATABASE_PATH", "users.db")

# Ensure the database parent directory exists
db_dir = os.path.dirname(DATABASE_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

# Automatically migrate legacy users.db from local directory to dynamic DATABASE_PATH
if DATABASE_PATH != "users.db" and not os.path.exists(DATABASE_PATH) and os.path.exists("users.db"):
    try:
        import shutil
        shutil.copy2("users.db", DATABASE_PATH)
        logger.info(f"Automatically migrated existing 'users.db' database to '{DATABASE_PATH}'.")
    except Exception as e:
        logger.error(f"Failed to automatically migrate 'users.db' to '{DATABASE_PATH}': {e}")

def init_db():
    logger.info("Initializing SQLite database connection and schemas...")
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    server_url TEXT,
                    username TEXT,
                    password TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS file_cache (
                    track_id TEXT PRIMARY KEY,
                    file_id TEXT,
                    title TEXT,
                    artist TEXT,
                    duration INTEGER
                )
            """)
        logger.info("SQLite database tables verified/created successfully.")
    except Exception as e:
        logger.exception(f"Critical error initializing SQLite database: {e}")
        raise e

init_db()

# -------------------------------------------------------------
# COMMAND HANDLERS
# -------------------------------------------------------------
@bot.message_handler(commands=['start'])
def start_cmd(message):
    user_id = message.from_user.id
    logger.info(f"[COMMAND] /start received from user_id: {user_id} (chat_id: {message.chat.id})")
    welcome_text = (
        "Welcome to the Subsonic/Navidrome Bot! 🎵\n\n"
        "Connect your server to share what you are listening to in any chat via inline queries!\n\n"
        "Here is the list of supported commands:\n"
        "/start - Start the bot and get a welcome message\n"
        "/login - Connect your Subsonic/Navidrome account\n"
        "/account - View your connected account details\n"
        "/logout - Disconnect your Subsonic/Navidrome account\n"
        "/help - Show this list of supported commands"
    )
    bot.reply_to(message, welcome_text)

@bot.message_handler(commands=['help'])
def help_cmd(message):
    user_id = message.from_user.id
    logger.info(f"[COMMAND] /help received from user_id: {user_id} (chat_id: {message.chat.id})")
    help_text = (
        "Here is the list of supported commands:\n"
        "/start - Start the bot and get a welcome message\n"
        "/login - Connect your Subsonic/Navidrome account\n"
        "/account - View your connected account details\n"
        "/logout - Disconnect your Subsonic/Navidrome account\n"
        "/help - Show this list of supported commands"
    )
    bot.reply_to(message, help_text)

@bot.message_handler(commands=['account'])
def account_cmd(message):
    user_id = message.from_user.id
    logger.info(f"[COMMAND] /account received from user_id: {user_id} (chat_id: {message.chat.id})")
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute("SELECT server_url, username FROM users WHERE user_id = ?", (user_id,)).fetchone()
        
        if row:
            server_url = row[0]
            username = row[1]
            account_text = (
                "👤 *Connected Account Details:*\n"
                f"• *Server URL:* {server_url}\n"
                f"• *Username:* {username}\n\n"
                f"You can now use the bot in any chat by typing: `@{BOT_USERNAME}`"
            )
            bot.reply_to(message, account_text, parse_mode="Markdown")
        else:
            bot.reply_to(
                message, 
                "You don't have a connected Subsonic/Navidrome account yet.\n"
                "Please use /login to connect your account."
            )
    except Exception as db_err:
        logger.exception(f"[COMMAND ERROR] Failed to query account for user_id {user_id}: {db_err}")
        bot.reply_to(message, "An internal database error occurred. Please try again.")

@bot.message_handler(commands=['logout'])
def logout_cmd(message):
    user_id = message.from_user.id
    logger.info(f"[COMMAND] /logout received from user_id: {user_id} (chat_id: {message.chat.id})")
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            cursor = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            rows_deleted = cursor.rowcount
        
        if rows_deleted > 0:
            bot.reply_to(
                message, 
                "🚪 *Successfully logged out!*\n\nYour server URL, username, and password have been removed from our database.\n\n"
                "If you want to use the bot again, you can reconnect using /login."
            )
            logger.info(f"[LOGOUT SUCCESS] Cleared credentials for user_id {user_id}.")
        else:
            bot.reply_to(
                message, 
                "You are not currently logged in.\n"
                "Use /login to connect your Subsonic/Navidrome account."
            )
    except Exception as db_err:
        logger.exception(f"[COMMAND ERROR] Failed to delete account for user_id {user_id}: {db_err}")
        bot.reply_to(message, "An internal database error occurred. Please try again.")

def check_cancel(message):
    text = message.text.strip().lower() if message.text else ""
    if text in ("/cancel", "cancel", "❌ cancel"):
        logger.info(f"[LOGIN] Login process cancelled by user {message.from_user.id}")
        bot.clear_step_handler_by_chat_id(chat_id=message.chat.id)
        bot.reply_to(message, "❌ Login process cancelled.", reply_markup=types.ReplyKeyboardRemove())
        return True
    return False

@bot.message_handler(commands=['login'])
def login_cmd(message):
    user_id = message.from_user.id
    logger.info(f"[COMMAND] /login initiated by user_id: {user_id} (chat_id: {message.chat.id})")
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Cancel"))
    
    msg = bot.reply_to(
        message, 
        "Please send your Subsonic/Navidrome server base URL (e.g., http://yourserver:4533):",
        reply_markup=markup
    )
    bot.register_next_step_handler(msg, process_url)

def process_url(message):
    if check_cancel(message):
        return
    user_id = message.from_user.id
    server_url = message.text.strip().rstrip('/')
    logger.info(f"[LOGIN STEP 1] process_url for user_id {user_id}: Input url='{server_url}'")
    
    if not server_url.startswith('http'):
        logger.warning(f"[LOGIN STEP 1 FAILED] Invalid URL scheme from user_id {user_id}: '{server_url}'")
        bot.reply_to(
            message, 
            "Invalid URL. Please start again with /login and provide a URL starting with http:// or https://",
            reply_markup=types.ReplyKeyboardRemove()
        )
        return
        
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Cancel"))
    
    msg = bot.reply_to(message, "Great. Now send your username:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_username, server_url)

def process_username(message, server_url):
    if check_cancel(message):
        return
    user_id = message.from_user.id
    username = message.text.strip()
    logger.info(f"[LOGIN STEP 2] process_username for user_id {user_id}: Username='{username}', Server='{server_url}'")
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("❌ Cancel"))
    
    msg = bot.reply_to(message, "Got it. Finally, send your password:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_password, server_url, username)

def process_password(message, server_url, username):
    if check_cancel(message):
        return
    user_id = message.from_user.id
    password = message.text.strip()
    
    # Securely delete the password message IMMEDIATELY for privacy
    try:
        bot.delete_message(message.chat.id, message.message_id)
        logger.info("[LOGIN] Password message deleted immediately for security.")
    except Exception as delete_err:
        logger.warning(f"[LOGIN WARNING] Failed to delete password message immediately: {delete_err}")
        
    # Inform the user that we are testing the connection
    status_msg = bot.send_message(
        message.chat.id, 
        "🔄 Testing connection to your Subsonic server...", 
        reply_markup=types.ReplyKeyboardRemove()
    )
    
    def send_final_message(text):
        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
            logger.debug("[LOGIN] Deleted temporary status message.")
        except Exception as delete_err:
            logger.warning(f"[LOGIN WARNING] Failed to delete temporary status message: {delete_err}")
        
        bot.send_message(
            message.chat.id,
            text,
            parse_mode="Markdown"
        )

    rest_url = server_url if server_url.endswith('/rest') else server_url + "/rest"
    auth = get_auth_params(username, password)
    ping_url = f"{rest_url}/ping?{auth}"
    
    logger.info(f"[LOGIN TEST] Testing Subsonic credentials for user {username} on {rest_url}...")
    
    try:
        req = requests.get(ping_url, timeout=8)
        logger.debug(f"[LOGIN TEST] Subsonic test ping HTTP status: {req.status_code}")
        
        if req.status_code != 200:
            send_final_message(
                f"❌ *Connection Failed!*\n\nYour server returned HTTP status `{req.status_code}` instead of `200`.\n\nPlease check your server URL and try logging in again with /login."
            )
            return
            
        try:
            res = req.json()
        except Exception:
            send_final_message(
                "❌ *Authentication Failed!*\n\nServer responded, but it did not return valid Subsonic JSON. Please verify that your URL points to a Subsonic/Navidrome server API.\n\nPlease try again with /login."
            )
            return
            
        response_data = res.get("subsonic-response", {})
        status = response_data.get("status")
        
        if status == "failed":
            error_data = response_data.get("error", {})
            error_code = error_data.get("code", "Unknown")
            error_msg = error_data.get("message", "Wrong username or password.")
            send_final_message(
                f"❌ *Authentication Failed!* (Error Code {error_code})\n\n{error_msg}\n\nPlease check your credentials and try again with /login."
            )
            return
            
        elif status == "ok":
            logger.info(f"[LOGIN SUCCESS] Subsonic test connection successful for user_id {user_id}.")
        else:
            send_final_message(
                f"❌ *Connection Warning!*\n\nReceived unknown response status: `{status}`.\n\nPlease try again with /login."
            )
            return
            
    except requests.exceptions.RequestException as req_err:
        logger.warning(f"[LOGIN TEST FAILED] Network connection error: {req_err}")
        send_final_message(
            f"❌ *Connection Error!*\n\nCould not reach the server.\n\n*Details:* `{str(req_err)}`\n\nPlease check that your server URL is correct, online, and publicly accessible, then try again with /login."
        )
        return
        
    # If the connection test succeeds, save the credentials!
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO users (user_id, server_url, username, password)
                VALUES (?, ?, ?, ?)
            """, (user_id, server_url, username, password))
        logger.info(f"[LOGIN DATABASE] Saved verified credentials successfully for user_id {user_id}.")
        
        send_final_message(
            f"✅ *Successfully logged in!*\n\nYour Subsonic/Navidrome credentials are verified and securely saved.\n\nYou can now share your played music in any chat by typing: `@{BOT_USERNAME}`"
        )
    except Exception as db_err:
        logger.exception(f"[LOGIN DATABASE ERROR] Failed to save credentials to database for user_id {user_id}: {db_err}")
        send_final_message(
            "❌ *Database Error!*\n\nYour credentials were verified but could not be saved to our internal database. Please try again with /login."
        )

def get_auth_params(username, password):
    """Generates a secure dynamic MD5 authentication string for the Subsonic API."""
    salt = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=6))
    token = hashlib.md5((password + salt).encode()).hexdigest()
    return f"u={username}&t={token}&s={salt}&v=1.16.1&c=telegram-bot&f=json"

# -------------------------------------------------------------
# INLINE QUERY HANDLER
# -------------------------------------------------------------
@bot.inline_handler(lambda query: True)
def query_text(inline_query):
    user_id = inline_query.from_user.id
    query_str = inline_query.query
    logger.info(f"[INLINE QUERY] Received inline query '{query_str}' from user_id: {user_id} (query_id: {inline_query.id})")
    
    try:
        logger.debug(f"[INLINE QUERY] Querying SQLite for user credentials...")
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute("SELECT server_url, username, password FROM users WHERE user_id = ?", (user_id,)).fetchone()
            
        if not row:
            logger.warning(f"[INLINE QUERY] User {user_id} not logged in database. Sending login entry...")
            login_help_article = types.InlineQueryResultArticle(
                id="login_help",
                title="🔑 Not logged in!",
                description="Click here to learn how to connect your Navidrome/Subsonic server.",
                input_message_content=types.InputTextMessageContent(
                    message_text=(
                        "To use this bot, you need to connect it to your Navidrome/Subsonic server first.\n\n"
                        f"1. Start a direct chat with me: @{BOT_USERNAME}\n"
                        "2. Send the `/login` command.\n"
                        "3. Follow the simple steps to set up your server, username, and password.\n\n"
                        "After that, you'll be able to instantly search and share your music in any chat!"
                    )
                )
            )
            bot.answer_inline_query(
                inline_query.id, 
                [login_help_article], 
                cache_time=0, 
                switch_pm_text="🔑 Login to Subsonic First", 
                switch_pm_parameter="login"
            )
            logger.info(f"[INLINE QUERY] Answered inline query {inline_query.id} with PM login button and helpful entry.")
            return
            
        server_url = row[0].rstrip('/')
        username = row[1]
        password = row[2]
        
        if server_url.endswith('/rest'):
            rest_url = server_url
        else:
            rest_url = server_url + "/rest"
            
        logger.debug(f"[INLINE QUERY] Credentials found. Subsonic Endpoint: {rest_url}, Username: {username}")
        
        # Fetch current history from Subsonic API
        auth = get_auth_params(username, password)
        now_playing_endpoint = f"{rest_url}/getNowPlaying?{auth}"
        logger.info(f"[INLINE QUERY] Fetching Subsonic getNowPlaying history for user {user_id} from {rest_url}...")
        
        try:
            req = requests.get(now_playing_endpoint, timeout=10)
            logger.debug(f"[INLINE QUERY] getNowPlaying HTTP status: {req.status_code}")
            if req.status_code != 200:
                logger.error(f"[INLINE QUERY ERROR] Subsonic server returned HTTP status {req.status_code} instead of 200. Check if your connected server URL is correct!")
                return
            response = req.json()
        except Exception as api_err:
            logger.exception(f"[INLINE QUERY ERROR] Failed to fetch/parse getNowPlaying from Subsonic: {api_err}")
            return
            
        now_playing_list = response.get('subsonic-response', {}).get('nowPlaying', {}).get('entry', [])
        if not isinstance(now_playing_list, list):
            now_playing_list = [now_playing_list] if now_playing_list else []
            
        logger.info(f"[INLINE QUERY] getNowPlaying parsed. Found {len(now_playing_list)} active song(s).")
        results = []
        
        # Build the Now Playing cards (limit to top 5)
        for idx, track in enumerate(now_playing_list[:5]):
            track_id = track.get('id')
            title = track.get('title', 'Unknown Title')
            artist = track.get('artist', 'Unknown Artist')
            duration = int(track.get('duration')) if track.get('duration') else None
            
            if not track_id:
                logger.warning(f"[INLINE QUERY WARNING] Track item is missing an 'id'. Skipping. Track data: {track}")
                continue
                
            if WEBHOOK_URL:
                placeholder_url = f"{WEBHOOK_URL.rstrip('/')}/_.mp3?t={track_id}"
            else:
                placeholder_url = f"https://es3n1n.eu/empty.mp3?t={track_id}"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text="⚡ Downloading...", callback_data=f"dl_{track_id}"))

            item = types.InlineQueryResultAudio(
                id=f"np_{track_id}",
                audio_url=placeholder_url,
                title=title,
                performer=artist,
                audio_duration=duration,
                reply_markup=markup
            )
            results.append(item)
            logger.info(f"[INLINE QUERY] Added NP Result Card: '{title}' by '{artist}' (result_id: np_{track_id}, duration: {duration})")

        # Fetch recently playing songs using Navidrome's private /api/song endpoint
        limit_history = 4 if not now_playing_list else 3
        recently_played_list = []
        try:
            logger.info(f"[INLINE QUERY] Authenticating to Navidrome private API at {server_url}/auth/login...")
            auth_req = requests.post(
                f"{server_url}/auth/login", 
                json={'username': username, 'password': password}, 
                timeout=5
            )
            logger.debug(f"[INLINE QUERY] Navidrome login response status: {auth_req.status_code}")
            
            if auth_req.status_code == 200:
                auth_resp = auth_req.json()
                jwt_token = auth_resp.get('token')
                if jwt_token:
                    logger.debug(f"[INLINE QUERY] JWT login token secured. Retrieving {limit_history} recently played songs...")
                    recent_url = f"{server_url}/api/song?_end={limit_history}&_order=DESC&_sort=play_date&_start=0&recently_played=true"
                    recent_req = requests.get(
                        recent_url, 
                        headers={'x-nd-authorization': f'Bearer {jwt_token}'}, 
                        timeout=5
                    )
                    logger.debug(f"[INLINE QUERY] Navidrome recent songs HTTP status: {recent_req.status_code}")
                    recently_played_list = recent_req.json()
                    if not isinstance(recently_played_list, list):
                        recently_played_list = [recently_played_list] if recently_played_list else []
                    logger.info(f"[INLINE QUERY] Navidrome private API successfully returned {len(recently_played_list)} recent tracks.")
                else:
                    logger.warning(f"[INLINE QUERY WARNING] Login response did not contain a 'token'. Response: {auth_resp}")
            else:
                logger.warning(f"[INLINE QUERY WARNING] Navidrome private API login failed. Status code: {auth_req.status_code}. Response: {auth_req.text[:200]}")
        except Exception as navi_err:
            logger.exception(f"[INLINE QUERY ERROR] Failed to fetch Navidrome private API history: {navi_err}")

        # Build the History cards (limit dynamically based on now playing list)
        for idx, track in enumerate(recently_played_list[:limit_history]):
            track_id = track.get('id')
            title = track.get('title', 'Unknown Title')
            artist = track.get('artist', 'Unknown Artist')
            duration = int(track.get('duration')) if track.get('duration') else None
            
            if not track_id:
                logger.warning(f"[INLINE QUERY WARNING] History track item is missing an 'id'. Skipping. Track data: {track}")
                continue
                
            # Skip if we already added it in the "Now Playing" list to avoid duplicate cards
            if any(track_id == np_track.get('id') for np_track in now_playing_list):
                logger.debug(f"[INLINE QUERY] Skipping duplicate history card for track_id: {track_id} ('{title}')")
                continue
                
            if WEBHOOK_URL:
                placeholder_url = f"{WEBHOOK_URL.rstrip('/')}/_.mp3?t={track_id}"
            else:
                placeholder_url = f"https://es3n1n.eu/empty.mp3?t={track_id}"
            
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton(text="⚡ Downloading...", callback_data=f"dl_{track_id}"))

            item = types.InlineQueryResultAudio(
                id=f"hist_{track_id}",
                audio_url=placeholder_url,
                title=title,
                performer=artist,
                audio_duration=duration,
                reply_markup=markup
            )
            results.append(item)
            logger.info(f"[INLINE QUERY] Added History Result Card: '{title}' by '{artist}' (result_id: hist_{track_id}, duration: {duration})")
            
        # Send responses back to Telegram
        logger.info(f"[INLINE QUERY] Answering inline query {inline_query.id} with {len(results)} total card options.")
        ans_ok = bot.answer_inline_query(inline_query.id, results, cache_time=0)
        logger.info(f"[INLINE QUERY SUCCESS] answer_inline_query API response for query {inline_query.id}: {ans_ok}")
    except Exception as e:
        logger.exception(f"[INLINE QUERY FATAL ERROR] Exception processing inline query for user {user_id}: {e}")

# -------------------------------------------------------------
# BACKGROUND WORKER - PROCESS SELECTED TRACK
# -------------------------------------------------------------
def process_chosen_track(user_id, track_id, inline_message_id):
    logger.info(f"[BACKGROUND WORKER] Started for user_id={user_id}, track_id={track_id}, inline_message_id={inline_message_id}")
    try:
        # 1. Fetch user credentials from SQLite database
        logger.debug(f"[BACKGROUND WORKER] Fetching credentials for user_id: {user_id}...")
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute("SELECT server_url, username, password FROM users WHERE user_id = ?", (user_id,)).fetchone()
            
        if not row:
            logger.error(f"[BACKGROUND WORKER ERROR] No credentials found in database for user_id {user_id}! Aborting.")
            return
            
        server_url, username, password = row
        server_url = server_url.rstrip('/')
        rest_url = server_url if server_url.endswith('/rest') else server_url + "/rest"
        auth = get_auth_params(username, password)
        logger.info(f"[BACKGROUND WORKER] Server endpoint resolved: {rest_url} (user: {username})")
        
        # 2. Check if this track is already cached in file_cache
        logger.debug(f"[BACKGROUND WORKER] Querying file_cache for track_id: {track_id}...")
        with sqlite3.connect(DATABASE_PATH) as conn:
            cache_row = conn.execute("SELECT file_id, title, artist, duration FROM file_cache WHERE track_id = ?", (track_id,)).fetchone()
            
        if cache_row:
            file_id, title, artist, duration = cache_row
            logger.info(f"[BACKGROUND WORKER] CACHE HIT for track {track_id}! Cached file_id: {file_id}. Title='{title}', Artist='{artist}'")
            
            # Update the inline message using the cached file_id instantly!
            logger.info(f"[BACKGROUND WORKER] Editing inline message {inline_message_id} with cached file_id...")
            res = bot.edit_message_media(
                media=types.InputMediaAudio(
                    media=file_id,
                    title=title,
                    performer=artist,
                    duration=duration
                ),
                inline_message_id=inline_message_id,
                reply_markup=None
            )
            logger.info(f"[BACKGROUND WORKER SUCCESS] edit_message_media completed for cache hit. Result: {res}")
            return
            
        logger.info(f"[BACKGROUND WORKER] CACHE MISS for track {track_id}. Fetching fresh metadata, stream, and artwork...")
        
        # 3. Fetch track metadata from Subsonic
        metadata_url = f"{rest_url}/getSong?id={track_id}&{auth}"
        logger.info(f"[BACKGROUND WORKER] Fetching track metadata from: {metadata_url}")
        
        try:
            meta_req = requests.get(metadata_url, timeout=10)
            logger.debug(f"[BACKGROUND WORKER] getSong HTTP status: {meta_req.status_code}")
            meta_resp = meta_req.json()
        except Exception as e:
            logger.exception(f"[BACKGROUND WORKER ERROR] Failed to fetch track metadata from {metadata_url}: {e}")
            return
            
        song = meta_resp.get('subsonic-response', {}).get('song', {})
        if not song:
            logger.error(f"[BACKGROUND WORKER ERROR] Song metadata was empty/missing in API response! Response: {meta_resp}")
            return
            
        title = song.get('title', 'Unknown')
        artist = song.get('artist', 'Unknown')
        duration = int(song.get('duration')) if song.get('duration') else None
        cover_id = song.get('coverArt')
        
        logger.info(f"[BACKGROUND WORKER] Metadata parsed: Title='{title}', Artist='{artist}', Duration={duration}, CoverArt ID={cover_id}")
        
        # 4. Fetch cover art bytes
        cover_bytes = None
        if cover_id:
            cover_url = f"{rest_url}/getCoverArt?id={cover_id}&{auth}"
            logger.info(f"[BACKGROUND WORKER] Fetching cover art image from: {cover_url}")
            try:
                cover_resp = requests.get(cover_url, timeout=15)
                logger.debug(f"[BACKGROUND WORKER] getCoverArt HTTP status: {cover_resp.status_code}")
                if cover_resp.status_code == 200:
                    cover_bytes = cover_resp.content
                    logger.info(f"[BACKGROUND WORKER] Downloaded cover art image successfully. Size: {len(cover_bytes)} bytes")
                else:
                    logger.warning(f"[BACKGROUND WORKER WARNING] Server returned HTTP {cover_resp.status_code} for cover art.")
            except Exception as cover_err:
                logger.exception(f"[BACKGROUND WORKER ERROR] Exception downloading cover art: {cover_err}")
        else:
            logger.info("[BACKGROUND WORKER] No cover art ID listed. Album artwork embedding will be skipped.")
                
        # 5. Fetch actual audio data
        stream_url = f"{rest_url}/stream?id={track_id}&{auth}&format=mp3&maxBitrate=320"
        logger.info(f"[BACKGROUND WORKER] Downloading raw audio stream from: {stream_url}")
        
        try:
            s_req = requests.get(stream_url, timeout=45)
            logger.debug(f"[BACKGROUND WORKER] Stream download HTTP status: {s_req.status_code}")
            if s_req.status_code != 200:
                logger.error(f"[BACKGROUND WORKER ERROR] Failed to download audio stream. HTTP Status: {s_req.status_code}, Response: {s_req.text[:200]}")
                return
            audio_data = s_req.content
            logger.info(f"[BACKGROUND WORKER] Downloaded raw audio successfully. Size: {len(audio_data)} bytes")
        except Exception as stream_err:
            logger.exception(f"[BACKGROUND WORKER ERROR] Exception downloading audio stream: {stream_err}")
            return
            
        # 6. Apply ID3 tags in memory using Mutagen
        logger.info("[BACKGROUND WORKER] Applying ID3 tags and APIC front cover to MP3 stream in-memory...")
        buf = io.BytesIO(audio_data)
        try:
            tags = ID3(buf)
            logger.debug("[BACKGROUND WORKER] Existing ID3 tags parsed successfully.")
        except Exception as id3_parse_err:
            logger.warning(f"[BACKGROUND WORKER WARNING] Could not parse existing ID3 tags (normal if file has none): {id3_parse_err}. Initializing empty ID3 tags.")
            tags = ID3()
            
        try:
            tags.add(TIT2(encoding=Encoding.UTF8, text=title))
            tags.add(TPE1(encoding=Encoding.UTF8, text=artist))
            if cover_bytes:
                mime_type = "image/jpeg" if cover_bytes.startswith(b'\xff\xd8') else "image/png"
                tags.add(APIC(
                    encoding=Encoding.UTF8,
                    mime=mime_type,
                    type=3,  # 3 = Front Cover
                    desc='Cover',
                    data=cover_bytes
                ))
                logger.info(f"[BACKGROUND WORKER] Embedded APIC cover frame. Mime-type guessed: {mime_type}")
            else:
                logger.info("[BACKGROUND WORKER] Skipping APIC cover frame embed (no cover art bytes).")
                
            buf.seek(0)
            tags.save(buf, padding=lambda x: 0)
            modified_data = buf.getvalue()
            logger.info(f"[BACKGROUND WORKER] ID3 metadata saved. New file size: {len(modified_data)} bytes (delta: {len(modified_data) - len(audio_data)} bytes)")
        except Exception as id3_save_err:
            logger.exception(f"[BACKGROUND WORKER ERROR] Failed to modify ID3 tags in-memory: {id3_save_err}. Defaulting to unmodified stream.")
            modified_data = audio_data
            
        # 7. Upload the file to Telegram
        audio_file = io.BytesIO(modified_data)
        audio_file.name = f"{artist} - {title}.mp3"
        
        thumb_file = None
        if cover_bytes:
            thumb_file = io.BytesIO(cover_bytes)
            thumb_file.name = "cover.jpg"
            
        logger.info(f"[BACKGROUND WORKER] Uploading audio file to user's private message thread (user_id/chat_id={user_id}) via send_audio...")
        try:
            sent_msg = bot.send_audio(
                chat_id=user_id,
                audio=audio_file,
                thumb=thumb_file,
                title=title,
                performer=artist,
                duration=duration,
                caption=f"📥 \"{title}\" has been downloaded successfully!",
                disable_notification=True,
                timeout=60
            )
            file_id = sent_msg.audio.file_id
            logger.info(f"[BACKGROUND WORKER SUCCESS] Upload completed! Assigned file_id: {file_id}")
        except Exception as upload_err:
            logger.exception(
                f"[BACKGROUND WORKER ERROR] bot.send_audio failed! "
                f"CRITICAL: This usually happens if the user {user_id} has never started a direct message conversation with this bot! "
                f"The user MUST open a PM with the bot and click /start so the bot can upload cached assets to them. "
                f"Error details: {upload_err}"
            )
            return
            
        # Delete the uploaded message from user private chat to avoid clutter
        logger.info(f"[BACKGROUND WORKER] Deleting temporary helper upload message (message_id={sent_msg.message_id}) from user's PM thread...")
        try:
            bot.delete_message(chat_id=user_id, message_id=sent_msg.message_id)
            logger.info("[BACKGROUND WORKER] Helper message deleted successfully.")
        except Exception as delete_err:
            logger.warning(f"[BACKGROUND WORKER WARNING] Failed to delete helper message from user's PM: {delete_err}")
            
        # 8. Cache the file_id in SQLite
        logger.info(f"[BACKGROUND WORKER] Caching file_id '{file_id}' in SQLite file_cache table...")
        try:
            with sqlite3.connect(DATABASE_PATH) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO file_cache (track_id, file_id, title, artist, duration) VALUES (?, ?, ?, ?, ?)",
                    (track_id, file_id, title, artist, duration)
                )
            logger.info(f"[BACKGROUND WORKER] file_id successfully cached.")
        except Exception as db_err:
            logger.exception(f"[BACKGROUND WORKER ERROR] Failed to cache file_id in SQLite: {db_err}")
            
        # 9. Edit the inline message with the uploaded file_id!
        logger.info(f"[BACKGROUND WORKER] Swapping inline placeholder message (inline_message_id={inline_message_id}) with the official Telegram file_id media...")
        try:
            res = bot.edit_message_media(
                media=types.InputMediaAudio(
                    media=file_id,
                    title=title,
                    performer=artist,
                    duration=duration
                ),
                inline_message_id=inline_message_id,
                reply_markup=None
            )
            logger.info(f"[BACKGROUND WORKER SUCCESS] Successfully processed, cached, uploaded and updated track {track_id}! Response: {res}")
        except Exception as edit_err:
            logger.exception(f"[BACKGROUND WORKER ERROR] edit_message_media failed during final inline message update: {edit_err}")
            
    except Exception as fatal_err:
        logger.exception(f"[BACKGROUND WORKER FATAL ERROR] Uncaught exception in process_chosen_track: {fatal_err}")

# -------------------------------------------------------------
# CHOSEN INLINE RESULT HANDLER
# -------------------------------------------------------------
@bot.chosen_inline_handler(lambda chosen_inline_result: True)
def chosen_inline_result_handler(chosen_inline_result):
    inline_message_id = chosen_inline_result.inline_message_id
    result_id = chosen_inline_result.result_id
    user_id = chosen_inline_result.from_user.id
    query_text = chosen_inline_result.query
    
    logger.info(f"[CHOSEN RESULT EVENT] Click detected! User {user_id} clicked result_id '{result_id}' (inline_message_id: {inline_message_id}, query: '{query_text}')")
    logger.info("[CHOSEN RESULT EVENT] If you see this log, inline feedback is enabled in BotFather! Proceeding...")
    
    if not inline_message_id:
        logger.error("[CHOSEN RESULT EVENT ERROR] No inline_message_id provided in chosen inline update! Cannot swap media. Returning.")
        return
        
    if not (result_id.startswith("np_") or result_id.startswith("hist_")):
        logger.warning(f"[CHOSEN RESULT EVENT] Result ID '{result_id}' did not start with 'np_' or 'hist_'. Ignoring event.")
        return
        
    track_id = result_id.split("_", 1)[1]
    logger.info(f"[CHOSEN RESULT EVENT] Extracted track_id: '{track_id}'. Spawning background worker thread...")
    
    # Process chosen track in a background thread so we don't block the main event webhook loop
    try:
        t = threading.Thread(
            target=process_chosen_track, 
            args=(user_id, track_id, inline_message_id), 
            name=f"Worker-{track_id}",
            daemon=True
        )
        t.start()
        logger.info(f"[CHOSEN RESULT EVENT] Worker thread '{t.name}' successfully spawned.")
    except Exception as spawn_err:
        logger.exception(f"[CHOSEN RESULT EVENT ERROR] Failed to spawn background worker thread: {spawn_err}")

# -------------------------------------------------------------
# LEGACY FALLBACK STREAM PROXY (FLASK)
# -------------------------------------------------------------
app = Flask(__name__)

@app.route('/_.mp3')
def serve_silent_mp3():
    global SILENT_MP3_BYTES
    if not SILENT_MP3_BYTES:
        try:
            logger.info("[SILENT MP3] Fetching silent MP3 placeholder from remote on-demand...")
            raw_bytes = requests.get("https://github.com/anars/blank-audio/raw/master/250-milliseconds-of-silence.mp3", timeout=10).content
            
            # Inject custom 'Loading...' ID3 tags so Telegram's audio player shows 'Loading...'
            bio = io.BytesIO(raw_bytes)
            try:
                audio = ID3(bio)
            except Exception:
                audio = ID3()
            audio.add(TIT2(encoding=3, text=["Loading..."]))
            audio.add(TPE1(encoding=3, text=["Loading..."]))
            
            out_bio = io.BytesIO(raw_bytes)
            audio.save(out_bio)
            SILENT_MP3_BYTES = out_bio.getvalue()
            logger.info("[SILENT MP3] Silent MP3 with 'Loading...' ID3 tags cached in-memory.")
        except Exception as e:
            logger.error(f"[SILENT MP3 ERROR] Failed to fetch silent MP3: {e}")
            return Response(b'', mimetype='audio/mpeg')
    return Response(SILENT_MP3_BYTES, mimetype='audio/mpeg')

@app.route('/stream/<track_id>.mp3')
def stream_proxy(track_id):
    """
    Proxies an audio stream from Navidrome and dynamically prepends
    an ID3v2 metadata block containing the album cover so Telegram natively renders it.
    """
    user_id = request.args.get('uid')
    logger.info(f"[STREAM PROXY] HTTP GET request received for track_id: {track_id}, user_id: {user_id}")
    
    if not user_id:
        logger.warning("[STREAM PROXY ERROR] Missing user_id parameter in request.")
        abort(400)
        
    try:
        with sqlite3.connect(DATABASE_PATH) as conn:
            row = conn.execute("SELECT server_url, username, password FROM users WHERE user_id = ?", (user_id,)).fetchone()
            
        if not row:
            logger.warning(f"[STREAM PROXY ERROR] User {user_id} credentials not found in database.")
            abort(404)
            
        server_url, username, password = row
        server_url = server_url.rstrip('/')
        rest_url = server_url if server_url.endswith('/rest') else server_url + "/rest"
        auth = get_auth_params(username, password)
        logger.info(f"[STREAM PROXY] Resolved credentials for user {username}. Streaming from server: {rest_url}")
        
        # 1. Fetch metadata and cover from Navidrome
        meta_url = f"{rest_url}/getSong?id={track_id}&{auth}"
        logger.debug(f"[STREAM PROXY] Fetching song metadata: {meta_url}")
        meta_resp = requests.get(meta_url, timeout=10).json()
        song = meta_resp.get('subsonic-response', {}).get('song', {})
        
        title = song.get('title', 'Unknown')
        artist = song.get('artist', 'Unknown')
        cover_id = song.get('coverArt')
        logger.debug(f"[STREAM PROXY] Metadata parsed: Title='{title}', Artist='{artist}', Cover ID={cover_id}")
        
        cover_bytes = None
        if cover_id:
            cover_url = f"{rest_url}/getCoverArt?id={cover_id}&{auth}"
            logger.debug(f"[STREAM PROXY] Fetching cover art: {cover_url}")
            cover_resp = requests.get(cover_url, timeout=10)
            if cover_resp.status_code == 200:
                cover_bytes = cover_resp.content
                logger.debug(f"[STREAM PROXY] Cover art loaded. Size: {len(cover_bytes)} bytes")
                
        # 2. Request actual audio stream from Navidrome
        stream_url = f"{rest_url}/stream?id={track_id}&{auth}&format=mp3&maxBitrate=320"
        logger.info(f"[STREAM PROXY] Pulling audio stream from Navidrome: {stream_url}")
        s_req = requests.get(stream_url, timeout=30)
        if s_req.status_code != 200:
            logger.error(f"[STREAM PROXY ERROR] Navidrome stream request failed with HTTP {s_req.status_code}")
            abort(s_req.status_code)
            
        # 3. Modify ID3 tags in memory using Mutagen
        logger.debug("[STREAM PROXY] Formatting and embedding ID3 tags in-memory...")
        buf = io.BytesIO(s_req.content)
        try:
            tags = ID3(buf)
        except Exception:
            tags = ID3()
            
        tags.add(TIT2(encoding=Encoding.UTF8, text=title))
        tags.add(TPE1(encoding=Encoding.UTF8, text=artist))
        if cover_bytes:
            mime_type = "image/jpeg" if cover_bytes.startswith(b'\xff\xd8') else "image/png"
            tags.add(APIC(
                encoding=Encoding.UTF8,
                mime=mime_type,
                type=3,
                desc='Cover',
                data=cover_bytes
            ))
            
        buf.seek(0)
        tags.save(buf, padding=lambda x: 0)
        buf.seek(0)
        
        logger.info(f"[STREAM PROXY SUCCESS] Returning media stream for track_id: {track_id}")
        return send_file(
            buf,
            mimetype='audio/mpeg',
            as_attachment=True,
            download_name=f"{track_id}.mp3"
        )
    except Exception as e:
        logger.exception(f"[STREAM PROXY FATAL ERROR] streaming error: {e}")
        abort(500)

# -------------------------------------------------------------
# WEBHOOK ENDPOINT
# -------------------------------------------------------------
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        logger.debug(f"[WEBHOOK] Received update request headers: {dict(request.headers)}")
        logger.debug(f"[WEBHOOK] Raw incoming payload data: {json_string}")
        
        try:
            update = types.Update.de_json(json_string)
            logger.info(f"[WEBHOOK] Successfully parsed update object. Update ID: {update.update_id}")
            bot.process_new_updates([update])
            logger.debug(f"[WEBHOOK] Finished processing updates for update ID: {update.update_id}")
            return '', 200
        except Exception as process_err:
            logger.exception(f"[WEBHOOK ERROR] Failed to process update: {process_err}")
            return 'Internal Error', 500
    else:
        logger.warning(f"[WEBHOOK WARNING] Rejected webhook request with invalid content-type: {request.headers.get('content-type')}")
        abort(403)

# -------------------------------------------------------------
# MAIN STARTUP
# -------------------------------------------------------------
if __name__ == '__main__':
    logger.info("==================================================")
    logger.info("Initializing Subsonic/Navidrome Telegram Bot Startup")
    logger.info("==================================================")
    
    try:
        logger.info("Removing any existing webhooks from Telegram...")
        webhook_removed = bot.remove_webhook()
        logger.info(f"Telegram remove_webhook response: {webhook_removed}")
    except Exception as wh_err:
        logger.exception(f"Failed to remove webhook: {wh_err}")
        
    if WEBHOOK_URL:
        # We append the BOT_TOKEN to the URL to ensure it's a secret endpoint
        webhook_target = f"{WEBHOOK_URL.rstrip('/')}/{BOT_TOKEN}"
        logger.info(f"Setting webhook to secret URL: {WEBHOOK_URL.rstrip('/')}/<BOT_TOKEN>")
        try:
            webhook_set = bot.set_webhook(url=webhook_target)
            logger.info(f"Telegram set_webhook response: {webhook_set}")
        except Exception as wh_err:
            logger.exception(f"Failed to register webhook with Telegram: {wh_err}")
    else:
        logger.warning("WEBHOOK_URL is NOT configured. The bot will NOT receive any updates via Webhook!")
    
    # Render or other PaaS platforms route dynamic ports via the PORT environment variable
    port = int(os.environ.get("PORT", default=8080))
    logger.info(f"Starting Flask server on host 0.0.0.0, port {port}...")
    
    app.run(host='0.0.0.0', port=port)
