#!/usr/bin/env python3
"""Media Hub — unified notification, cleanup, Telegram bot, and automation.

- Webhooks from Radarr, Sonarr, Prowlarr, Jellyseerr, Jellyfin
- Adaptive polling for download progress and status changes
- Telegram bot with inline keyboards for torrent selection
- Auto-search for continuing series every 6 hours
- Jellyfin deletion cascading to Radarr/Sonarr
"""

import json
import os
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# Config
RADARR_URL = os.environ.get("RADARR_URL", "http://gluetun:7878/radarr")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989/sonarr")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
PROWLARR_URL = os.environ.get("PROWLARR_URL", "http://gluetun:9696/prowlarr")
PROWLARR_KEY = os.environ.get("PROWLARR_KEY", "")
JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "http://jellyseerr:5055")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096/jellyfin")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = int(os.environ.get("PORT", "9999"))
AUTO_SEARCH_HOURS = int(os.environ.get("AUTO_SEARCH_HOURS", "6"))

# ── Helpers ──────────────────────────────────────────────────────────────

def log(msg):
    print(f"[hub] {msg}", flush=True)


def api_get(url, api_key="", timeout=15):
    headers = {"X-Api-Key": api_key} if api_key else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"API GET {url}: {e}")
        return None


def api_post(url, api_key, data=None):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"API POST {url}: {e}")
        return None


def api_delete(url, api_key):
    req = urllib.request.Request(url, method="DELETE", headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception as e:
        log(f"API DELETE {url}: {e}")
        return None


def fmt_size(b):
    if not b: return "?"
    gb = b / (1024**3)
    return f"{gb:.1f}GB" if gb >= 1 else f"{b / (1024**2):.0f}MB"


# ── Telegram API ─────────────────────────────────────────────────────────

def tg(method, data=None):
    if not TELEGRAM_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    if data:
        body = json.dumps(data).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"TG {method}: {e}")
        return None


MAIN_KEYBOARD = {"keyboard": [
    ["🔍 Поиск", "📊 Статус", "📋 Список"],
], "resize_keyboard": True, "one_time_keyboard": False}


def send_telegram(text, reply_markup=None):
    log(f"TG: {text[:80]}")
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    elif any(u.get("waiting_query") for u in state.user_state.values()):
        data["reply_markup"] = {"keyboard": [["❌ Отмена"]], "resize_keyboard": True}
    else:
        data["reply_markup"] = MAIN_KEYBOARD
    tg("sendMessage", data)


def edit_message(message_id, text, reply_markup=None):
    data = {"chat_id": TELEGRAM_CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    tg("editMessageText", data)


def answer_callback(callback_id, text=""):
    tg("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


# ── State ────────────────────────────────────────────────────────────────

class State:
    def __init__(self):
        self.movies = {}
        self.series = {}
        self.queue_items = {}
        self.notified_progress = {}
        self.initialized = False
        self.pending_episodes = {}
        self.pending_lock = threading.Lock()
        self.last_auto_search = 0
        # Telegram interactive state
        self.search_results = {}  # callback_prefix -> [releases]
        self.user_state = {}     # chat_id -> {action, ...}

    def update_movies(self, movies):
        for m in movies:
            mid, title, status = m["id"], m.get("title", "?"), m.get("status", "?")
            has_file, monitored = m.get("hasFile", False), m.get("monitored", False)
            if not monitored:
                continue
            prev = self.movies.get(mid)

            # First load — record state silently, no notifications
            if not self.initialized:
                ns = "done" if has_file else ("nf" if status == "released" else f"w_{status}" if status in ("announced", "inCinemas") else None)
                self.movies[mid] = {"title": title, "status": status, "hasFile": has_file, "ns": ns}
                continue

            if prev:
                n = prev.get("ns")
                # Only notify on STATE CHANGES (not on every poll)
                if status == "released" and not has_file and prev.get("status") != "released":
                    send_telegram(f"❌ <b>Не найдено:</b> {title}\nНет подходящих релизов")
                    self.movies[mid] = {"title": title, "status": status, "hasFile": has_file, "ns": "nf"}
                    continue
                if status in ("announced", "inCinemas") and n != f"w_{status}" and prev.get("status") != status:
                    label = "Ожидаем выхода" if status == "announced" else "В кинотеатрах"
                    send_telegram(f"⏳ <b>{label}:</b> {title} ({m.get('year', '')})")
                    self.movies[mid] = {"title": title, "status": status, "hasFile": has_file, "ns": f"w_{status}"}
                    continue
                if has_file and not prev.get("hasFile"):
                    send_telegram(f"✅ <b>Готово:</b> {title}\nДоступно в Jellyfin")
                    self.movies[mid] = {"title": title, "status": status, "hasFile": has_file, "ns": "done"}
                    continue
            self.movies[mid] = {"title": title, "status": status, "hasFile": has_file, "ns": self.movies.get(mid, {}).get("ns")}

    def update_series(self, series_list):
        for s in series_list:
            self.series[s["id"]] = {
                "title": s.get("title", "?"), "status": s.get("status", "?"),
                "files": s.get("statistics", {}).get("episodeFileCount", 0),
                "total": s.get("statistics", {}).get("totalEpisodeCount", 0),
            }

    def update_queue(self, items):
        current = set()
        for item in items:
            dl_id = item.get("downloadId", "")
            if not dl_id:
                continue
            current.add(dl_id)
            title, size, left = item.get("title", "?"), item.get("size", 0), item.get("sizeleft", 0)
            pct = int(((size - left) / size) * 100) if size > 0 else 0
            ms = self.notified_progress.get(dl_id, set())
            if not self.initialized:
                if pct >= 50: ms.add(50)
            else:
                if pct >= 50 and 50 not in ms:
                    send_telegram(f"📊 <b>Прогресс:</b> {title[:50]}\n{pct}% ({fmt_size(size - left)}/{fmt_size(size)})")
                    ms.add(50)
            self.notified_progress[dl_id] = ms
            self.queue_items[dl_id] = {"title": title, "pct": pct}
        # Detect completed downloads and trigger rescan
        finished = [k for k in self.queue_items if k not in current]
        if finished and self.initialized:
            for k in finished:
                log(f"Download finished: {self.queue_items[k]['title'][:50]}")
            trigger_rescan()
        for k in [k for k in self.notified_progress if k not in current]: del self.notified_progress[k]
        for k in [k for k in self.queue_items if k not in current]: del self.queue_items[k]


state = State()

# ── Prowlarr Search ─────────────────────────────────────────────────────

def prowlarr_search(query):
    """Search all indexers via Prowlarr. Returns list of releases."""
    q = urllib.parse.quote(query)
    results = api_get(f"{PROWLARR_URL}/api/v1/search?query={q}&type=search", PROWLARR_KEY, timeout=30)
    if not results:
        return []
    # Sort: RuTracker first, then by seeders
    def sort_key(r):
        idx = r.get("indexer", "")
        is_rutracker = 1 if "rutracker" in idx.lower() else 0
        return (-is_rutracker, -(r.get("seeders", 0) or 0))
    results.sort(key=sort_key)
    return results


def send_search_results(query, results, callback_prefix):
    """Send search results with inline keyboard buttons."""
    if not results:
        send_telegram(f"❌ <b>Не найдено:</b> {query}")
        return
    # Filter only with seeders, limit to 15
    filtered = [r for r in results if (r.get("seeders") or 0) > 0][:15]
    if not filtered:
        filtered = results[:5]

    lines = [f"🔍 <b>{query}</b>\n"]
    buttons = []
    state.search_results[callback_prefix] = filtered

    for i, r in enumerate(filtered):
        idx = r.get("indexer", "?")
        title = r.get("title", "?")
        size = fmt_size(r.get("size", 0))
        seeders = r.get("seeders", 0)
        icon = "🟢" if seeders >= 10 else "🟡" if seeders >= 3 else "🔴"
        # Short title for button, full for message
        btn_title = title[:45] + "…" if len(title) > 45 else title
        lines.append(f"{icon} <b>{i+1}.</b> {size} 🌱{seeders} <i>{idx}</i>\n{title}\n")
        buttons.append([{"text": f"{i+1}. {size} 🌱{seeders} — {btn_title}", "callback_data": f"{callback_prefix}:{i}"}])

    buttons.append([{"text": "❌ Отмена", "callback_data": f"{callback_prefix}:cancel"}])
    send_telegram("\n".join(lines), {"inline_keyboard": buttons})


def classify_release(release):
    """Determine if release is movie, series, or sport based on categories."""
    cats = release.get("categories", [])
    cat_names = " ".join(c.get("name", "") for c in cats)
    if "Movie" in cat_names:
        return "movie"
    elif "TV" in cat_names:
        return "series"
    elif "Sport" in cat_names:
        return "sport"
    return "unknown"


def push_to_radarr(release, movie_id=None):
    """Push release to Radarr for proper import pipeline."""
    payload = {
        "title": release.get("title", ""),
        "downloadUrl": release.get("downloadUrl", ""),
        "protocol": "torrent",
        "publishDate": release.get("publishDate", "2026-01-01T00:00:00Z"),
    }
    if movie_id:
        payload["movieId"] = movie_id
    indexer = release.get("indexer", "")
    if indexer:
        payload["indexer"] = indexer
    result = api_post(f"{RADARR_URL}/api/v3/release/push", RADARR_KEY, [payload])
    if result:
        log(f"Pushed to Radarr: {release.get('title','?')[:50]}")
        return True
    log(f"Radarr push failed")
    return False


def push_to_sonarr(release, series_id=None):
    """Push release to Sonarr for proper import pipeline."""
    payload = {
        "title": release.get("title", ""),
        "downloadUrl": release.get("downloadUrl", ""),
        "protocol": "torrent",
        "publishDate": release.get("publishDate", "2026-01-01T00:00:00Z"),
    }
    if series_id:
        payload["seriesId"] = series_id
    indexer = release.get("indexer", "")
    if indexer:
        payload["indexer"] = indexer
    result = api_post(f"{SONARR_URL}/api/v3/release/push", SONARR_KEY, [payload])
    if result:
        log(f"Pushed to Sonarr: {release.get('title','?')[:50]}")
        return True
    log(f"Sonarr push failed")
    return False


def push_to_prowlarr(release):
    """Grab directly via Prowlarr (for sports and fallback)."""
    result = api_post(f"{PROWLARR_URL}/api/v1/search", PROWLARR_KEY, {
        "guid": release.get("guid", ""),
        "indexerId": release.get("indexerId", 0),
    })
    if result:
        log(f"Grabbed via Prowlarr: {release.get('title','?')[:50]}")
        return True
    log(f"Prowlarr grab failed")
    return False


def _clean_search_term(title):
    """Extract clean search term from release title."""
    search = title.split("[")[0].split("(")[0].strip()
    search = re.sub(r'S\d+E?\d*', '', search, flags=re.IGNORECASE)
    search = re.sub(r'\b(1080p|720p|480p|2160p|WEB|BD|HEVC|AVC|AAC|DUAL|DDP|H\.?\d+|WEB-DL|WEB-DLRip|BluRay|REMUX)\b', '', search, flags=re.IGNORECASE)
    search = re.sub(r'[-._]', ' ', search).strip()
    search = re.sub(r'\s+', ' ', search).strip()
    return search


def ensure_in_sonarr(release):
    """Ensure series exists in Sonarr. Add via lookup if missing. Returns series ID or None."""
    title = release.get("title", "")
    tvdb_id = release.get("tvdbId", 0)

    # Check existing series by tvdbId
    existing = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
    if existing and tvdb_id:
        for s in existing:
            if s.get("tvdbId") == tvdb_id:
                return s["id"]

    # Lookup by title
    search = _clean_search_term(title)
    if not search:
        return None

    q = urllib.parse.quote(search)
    results = api_get(f"{SONARR_URL}/api/v3/series/lookup?term={q}", SONARR_KEY, timeout=30)
    if not results:
        log(f"Sonarr lookup found nothing for: {search}")
        return None

    match = results[0]
    if tvdb_id:
        for r in results:
            if r.get("tvdbId") == tvdb_id:
                match = r
                break

    # Check not already added by tvdbId of the match
    if existing:
        for s in existing:
            if s.get("tvdbId") == match.get("tvdbId"):
                return s["id"]

    # Add to Sonarr
    payload = {
        "tvdbId": match["tvdbId"],
        "title": match.get("title", search),
        "titleSlug": match.get("titleSlug", ""),
        "images": match.get("images", []),
        "seasons": match.get("seasons", []),
        "qualityProfileId": 1,
        "languageProfileId": 1,
        "rootFolderPath": "/tv",
        "monitored": True,
        "seasonFolder": True,
        "seriesType": match.get("seriesType", "standard"),
        "addOptions": {"searchForMissingEpisodes": False},
    }
    added = api_post(f"{SONARR_URL}/api/v3/series", SONARR_KEY, payload)
    if added and isinstance(added, dict) and added.get("id"):
        log(f"Added to Sonarr: {match.get('title')} (tvdb:{match['tvdbId']})")
        time.sleep(3)  # Let Sonarr index the new series before push
        return added["id"]
    log(f"Failed to add to Sonarr: {match.get('title')}")
    return None


def ensure_in_radarr(release):
    """Ensure movie exists in Radarr. Add via lookup if missing. Returns movie ID or None."""
    title = release.get("title", "")
    tmdb_id = release.get("tmdbId", 0)

    existing = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
    if existing and tmdb_id:
        for m in existing:
            if m.get("tmdbId") == tmdb_id:
                return m["id"]

    search = _clean_search_term(title)
    if not search:
        return None

    q = urllib.parse.quote(search)
    results = api_get(f"{RADARR_URL}/api/v3/movie/lookup?term={q}", RADARR_KEY, timeout=30)
    if not results:
        log(f"Radarr lookup found nothing for: {search}")
        return None

    match = results[0]
    if tmdb_id:
        for r in results:
            if r.get("tmdbId") == tmdb_id:
                match = r
                break

    if existing:
        for m in existing:
            if m.get("tmdbId") == match.get("tmdbId"):
                return m["id"]

    payload = {
        "tmdbId": match["tmdbId"],
        "title": match.get("title", search),
        "titleSlug": match.get("titleSlug", ""),
        "images": match.get("images", []),
        "qualityProfileId": 1,
        "rootFolderPath": "/movies",
        "monitored": True,
        "addOptions": {"searchForMovie": False},
    }
    for key in ("year", "imdbId", "originalLanguage"):
        if match.get(key):
            payload[key] = match[key]

    added = api_post(f"{RADARR_URL}/api/v3/movie", RADARR_KEY, payload)
    if added and isinstance(added, dict) and added.get("id"):
        log(f"Added to Radarr: {match.get('title')} (tmdb:{match['tmdbId']})")
        time.sleep(3)
        return added["id"]
    log(f"Failed to add to Radarr: {match.get('title')}")
    return None


# ── Telegram Bot Commands ────────────────────────────────────────────────

def handle_bot_message(msg):
    """Handle incoming Telegram text messages."""
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if chat_id != TELEGRAM_CHAT_ID:
        return
    text = msg.get("text", "").strip()
    if not text:
        return

    # Commands
    if text in ("/start", "/help"):
        send_telegram("🎬 <b>Media Hub Bot</b>\n\nИспользуй кнопки внизу или просто напиши название для поиска.")
        return

    if text in ("🔍 Поиск", "/search"):
        send_telegram("Что ищем?", {"inline_keyboard": [
            [{"text": "🎬 Фильм", "callback_data": "type:movie"},
             {"text": "📺 Сериал", "callback_data": "type:series"},
             {"text": "⚽ Матч", "callback_data": "type:sport"}],
        ]})
        return

    if text == "❌ Отмена":
        state.user_state.pop(chat_id, None)
        send_telegram("👌 Отменено")
        return

    if text in ("📊 Статус", "/status"):
        show_status()
        return

    if text in ("📋 Список", "/list"):
        show_list()
        return

    # If user is in search mode, treat text as search query
    user = state.user_state.get(chat_id, {})
    search_type = user.get("waiting_query")
    if search_type:
        state.user_state.pop(chat_id, None)
        threading.Thread(target=do_search, args=(text, search_type), daemon=True).start()
        return

    # Default: search everything
    threading.Thread(target=do_search, args=(text, "all"), daemon=True).start()


def handle_callback(callback):
    """Handle inline keyboard button presses."""
    cb_id = callback.get("id", "")
    data = callback.get("data", "")
    msg = callback.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id != TELEGRAM_CHAT_ID:
        answer_callback(cb_id)
        return

    # Search type selected
    if data.startswith("type:"):
        search_type = data.split(":")[1]
        labels = {"movie": "фильма", "series": "сериала", "sport": "матча"}
        answer_callback(cb_id)
        label = labels.get(search_type, "")
        send_telegram(f"Введите название {label}:", {"keyboard": [["❌ Отмена"]], "resize_keyboard": True})
        state.user_state[chat_id] = {"waiting_query": search_type}
        return

    # Release selection from search results
    if ":" in data:
        prefix, action = data.rsplit(":", 1)
        if prefix in state.search_results:
            if action == "cancel":
                answer_callback(cb_id, "Отменено")
                edit_message(msg.get("message_id"), "❌ Отменено")
                state.search_results.pop(prefix, None)
                state.user_state.pop(chat_id, None)
                send_telegram("👌")  # Triggers main keyboard
                return
            try:
                idx = int(action)
                releases = state.search_results.get(prefix, [])
                if 0 <= idx < len(releases):
                    rel = releases[idx]
                    answer_callback(cb_id, "Отправлено на скачивание!")
                    # Remove search results and clear user state
                    state.search_results.pop(prefix, None)
                    state.user_state.pop(chat_id, None)
                    # Edit original message to show selection (no duplicate notification)
                    edit_message(msg.get("message_id"),
                        f"✅ Выбрано: {rel.get('title','?')[:60]}\n{fmt_size(rel.get('size',0))} • {rel.get('indexer','?')}")
                    threading.Thread(target=do_grab_silent, args=(rel,), daemon=True).start()
                    return
            except (ValueError, IndexError):
                pass

    answer_callback(cb_id)


def do_search(query, search_type):
    """Perform search in background thread."""
    results = prowlarr_search(query)

    # Filter by type
    if search_type == "movie":
        results = [r for r in results if any(
            c.get("name", "").startswith("Movies") for c in r.get("categories", [])
        )]
    elif search_type == "series":
        results = [r for r in results if any(
            c.get("name", "").startswith("TV") for c in r.get("categories", [])
        )]
    elif search_type == "sport":
        results = [r for r in results if any(
            "Sport" in c.get("name", "") for c in r.get("categories", [])
        )]
    # "all" — no filter

    prefix = f"sr_{int(time.time())}"
    send_search_results(query, results, prefix)


def do_grab_silent(release):
    """Route release through Sonarr/Radarr for proper import, or Prowlarr for sports."""
    rtype = classify_release(release)
    title = release.get("title", "?")[:60]

    if rtype == "movie":
        mid = ensure_in_radarr(release)
        if mid and push_to_radarr(release, movie_id=mid):
            return
        log(f"Radarr route failed for {title}, falling back to Prowlarr")
        push_to_prowlarr(release)
    elif rtype == "series":
        sid = ensure_in_sonarr(release)
        if sid and push_to_sonarr(release, series_id=sid):
            return
        log(f"Sonarr route failed for {title}, falling back to Prowlarr")
        push_to_prowlarr(release)
    else:
        # Sport or unknown — use Prowlarr directly
        push_to_prowlarr(release)


def _jellyfin_token():
    """Authenticate with Jellyfin and return a valid token."""
    jf_user = os.environ.get("JELLYFIN_USER", "admin")
    jf_pass = os.environ.get("JELLYFIN_PASSWORD", "")
    data = json.dumps({"Username": jf_user, "Pw": jf_pass}).encode()
    req = urllib.request.Request(f"{JELLYFIN_URL}/Users/AuthenticateByName",
        data=data,
        headers={"Content-Type": "application/json",
                 "X-Emby-Authorization": 'MediaBrowser Client="media-hub", Device="hub", DeviceId="hub1", Version="1.0.0"'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read()).get("AccessToken")


def trigger_rescan():
    """Tell Sonarr/Radarr to rescan downloads and Jellyfin to refresh library."""
    api_post(f"{SONARR_URL}/api/v3/command", SONARR_KEY, {"name": "DownloadedEpisodesScan"})
    api_post(f"{RADARR_URL}/api/v3/command", RADARR_KEY, {"name": "DownloadedMoviesScan"})
    try:
        token = _jellyfin_token()
        req = urllib.request.Request(f"{JELLYFIN_URL}/Library/Refresh", method="POST",
            headers={"Authorization": f'MediaBrowser Token="{token}"'})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Jellyfin refresh failed: {e}")
    log("Rescan triggered: Sonarr + Radarr + Jellyfin")


def show_status():
    lines = ["📊 <b>Статус</b>\n"]

    # qBittorrent — single source of truth
    torrents = []
    try:
        qb_url = "http://qbittorrent:8080/api/v2"
        login_data = urllib.parse.urlencode({"username": "admin", "password": os.environ.get("QB_PASSWORD", "")}).encode()
        req = urllib.request.Request(f"{qb_url}/auth/login", data=login_data)
        req.add_header("Referer", qb_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            cookie = resp.headers.get("Set-Cookie", "")
        req2 = urllib.request.Request(f"{qb_url}/torrents/info")
        req2.add_header("Cookie", cookie)
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            torrents = json.loads(resp2.read())
    except Exception as e:
        log(f"QB status error: {e}")
        lines.append("qBittorrent недоступен")
        send_telegram("\n".join(lines))
        return

    if not torrents:
        lines.append("Нет активных торрентов")
        send_telegram("\n".join(lines))
        return

    downloading, moving, seeding, other = [], [], [], []

    for t in torrents:
        name = t.get("name", "?")[:50]
        pct = int(t.get("progress", 0) * 100)
        st = t.get("state", "")
        speed_dl = t.get("dlspeed", 0) / (1024 * 1024)
        size = fmt_size(t.get("total_size", 0))
        uploaded = fmt_size(t.get("uploaded", 0))
        ratio = t.get("ratio", 0)

        if st in ("downloading", "forcedDL", "stalledDL", "metaDL", "allocating"):
            downloading.append(f"  ⬇️ {name}\n     {pct}% • {speed_dl:.1f} MB/s • {size}")
        elif st == "moving":
            moving.append(f"  📦 {name}\n     Перемещение...")
        elif st in ("uploading", "forcedUP", "stalledUP", "queuedUP"):
            seeding.append(f"  🌱 {name}\n     {uploaded} отдано • {ratio:.1f}x")
        elif st in ("checkingDL", "checkingUP", "checkingResumeData"):
            other.append(f"  🔍 {name}\n     Проверка...")
        elif st in ("pausedDL", "pausedUP"):
            other.append(f"  ⏸ {name}\n     Пауза ({pct}%)")
        elif st == "queuedDL":
            other.append(f"  🕐 {name}\n     В очереди")

    if downloading:
        lines.append(f"<b>⬇️ Загрузка ({len(downloading)}):</b>")
        lines.extend(downloading)
    if moving:
        lines.append(f"\n<b>📦 Перемещение ({len(moving)}):</b>")
        lines.extend(moving)
    if seeding:
        lines.append(f"\n<b>🌱 Раздача ({len(seeding)}):</b>")
        lines.extend(seeding)
    if other:
        lines.append(f"\n<b>⏳ Прочее ({len(other)}):</b>")
        lines.extend(other)

    total_size = sum(t.get("total_size", 0) for t in torrents)
    total_uploaded = sum(t.get("uploaded", 0) for t in torrents)
    lines.append(f"\n💾 Торренты: {fmt_size(total_size)} • Отдано: {fmt_size(total_uploaded)}")

    send_telegram("\n".join(lines))


def show_list():
    lines = ["📋 <b>Мониторинг</b>\n"]

    # Fresh data from APIs
    movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY) or []
    series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY) or []

    done_movies, waiting_movies, searching_movies = [], [], []
    done_series, active_series = [], []

    for m in movies:
        if not m.get("monitored"):
            continue
        title = m.get("title", "?")
        year = m.get("year", "")
        label = f"{title} ({year})" if year else title
        if m.get("hasFile"):
            done_movies.append(label)
        elif m.get("status") in ("announced", "inCinemas"):
            st = "анонсирован" if m["status"] == "announced" else "в кинотеатрах"
            waiting_movies.append(f"{label} — {st}")
        elif m.get("status") == "released":
            searching_movies.append(label)

    for s in series:
        if not s.get("monitored"):
            continue
        title = s.get("title", "?")
        # Count only monitored seasons (skip Season 0 specials)
        monitored_total = 0
        monitored_files = 0
        for season in s.get("seasons", []):
            if season.get("monitored") and season.get("seasonNumber", 0) > 0:
                ss = season.get("statistics", {})
                monitored_total += ss.get("totalEpisodeCount", 0)
                monitored_files += ss.get("episodeFileCount", 0)

        if monitored_total == 0:
            continue

        if monitored_files >= monitored_total and s.get("status") == "continuing":
            active_series.append(f"{title} ({monitored_files}/{monitored_total} серий) — ждём новых")
        elif monitored_files >= monitored_total:
            done_series.append(f"{title} ({monitored_files} серий)")
        else:
            active_series.append(f"{title} ({monitored_files}/{monitored_total} серий)")

    has_any = False

    if waiting_movies:
        has_any = True
        lines.append("<b>⏳ Ожидаем выхода:</b>")
        for m in waiting_movies:
            lines.append(f"  • {m}")

    if searching_movies:
        has_any = True
        lines.append("\n<b>🔍 Ищем релизы:</b>")
        for m in searching_movies:
            lines.append(f"  • {m}")

    if active_series:
        has_any = True
        lines.append("\n<b>📺 Сериалы:</b>")
        for s in active_series:
            lines.append(f"  • {s}")

    if done_movies or done_series:
        has_any = True
        lines.append("\n<b>✅ Скачано:</b>")
        for m in done_movies:
            lines.append(f"  🎬 {m}")
        for s in done_series:
            lines.append(f"  📺 {s}")

    if not has_any:
        lines.append("Пусто — добавьте что-нибудь через Поиск")

    send_telegram("\n".join(lines))


# ── Webhook handlers ─────────────────────────────────────────────────────

def handle_radarr_webhook(data):
    event = data.get("eventType", "")
    movie = data.get("movie", {})
    title = movie.get("title", "?")
    release = data.get("release", {})

    if event == "Grab":
        send_telegram(f"⬇️ <b>Качаем фильм:</b> {title}\n{release.get('quality','?')} • {fmt_size(release.get('size',0))} • {release.get('indexer','?')}")
    elif event == "Download":
        if data.get("isUpgrade"):
            send_telegram(f"⬆️ <b>Обновлено:</b> {title}")
        else:
            send_telegram(f"✅ <b>Фильм готов:</b> {title}\nДоступно в Jellyfin")
    elif event == "MovieAdded":
        log(f"Movie added: {title} (no auto-search — user already selected torrent)")
    elif event == "MovieDelete":
        send_telegram(f"🗑 <b>Удалён:</b> {title}")
    elif event == "Health":
        send_telegram(f"⚠️ <b>Radarr:</b> {data.get('message','?')}")


def flush_episodes(title):
    with state.pending_lock:
        info = state.pending_episodes.pop(title, None)
    if not info or not info["episodes"]:
        return
    eps = sorted(set(info["episodes"]))
    if len(eps) == 1:
        send_telegram(f"✅ <b>Серия готова:</b> {title} ({eps[0]})\nДоступно в Jellyfin")
    else:
        send_telegram(f"✅ <b>Сериал готов:</b> {title}\nСкачано {len(eps)} серий ({eps[0]}–{eps[-1]}). Доступно в Jellyfin")


def handle_sonarr_webhook(data):
    event = data.get("eventType", "")
    series = data.get("series", {})
    title = series.get("title", "?")
    episodes = data.get("episodes", [])
    release = data.get("release", {})

    if event == "Grab":
        ep = ""
        if episodes:
            nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes[:3]]
            ep = f" ({', '.join(nums)})"
        send_telegram(f"⬇️ <b>Качаем:</b> {title}{ep}\n{release.get('quality','?')} • {fmt_size(release.get('size',0))} • {release.get('indexer','?')}")
    elif event == "Download":
        if data.get("isUpgrade"):
            return
        ep_nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes]
        with state.pending_lock:
            if title not in state.pending_episodes:
                state.pending_episodes[title] = {"episodes": []}
            state.pending_episodes[title]["episodes"].extend(ep_nums)
            existing = state.pending_episodes[title].get("timer")
            if existing:
                existing.cancel()
            timer = threading.Timer(120, flush_episodes, args=[title])
            timer.daemon = True
            timer.start()
            state.pending_episodes[title]["timer"] = timer
    elif event == "SeriesAdd":
        log(f"Series added: {title} (no auto-search — user already selected torrent)")
    elif event == "SeriesDelete":
        send_telegram(f"🗑 <b>Удалён:</b> {title}")


def _qb_delete_torrents(search_name):
    """Delete torrents from qBittorrent whose name contains search_name."""
    try:
        qb_url = "http://qbittorrent:8080/api/v2"
        login_data = urllib.parse.urlencode({"username": "admin", "password": os.environ.get("QB_PASSWORD", "")}).encode()
        req = urllib.request.Request(f"{qb_url}/auth/login", data=login_data)
        req.add_header("Referer", qb_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            cookie = resp.headers.get("Set-Cookie", "")
        req2 = urllib.request.Request(f"{qb_url}/torrents/info")
        req2.add_header("Cookie", cookie)
        with urllib.request.urlopen(req2, timeout=5) as resp2:
            torrents = json.loads(resp2.read())
        search_lower = search_name.lower()
        hashes = [t["hash"] for t in torrents if search_lower in t.get("name", "").lower()]
        if hashes:
            data = urllib.parse.urlencode({"hashes": "|".join(hashes), "deleteFiles": "true"}).encode()
            req3 = urllib.request.Request(f"{qb_url}/torrents/delete", data=data)
            req3.add_header("Cookie", cookie)
            urllib.request.urlopen(req3, timeout=10)
            log(f"Deleted {len(hashes)} torrents from qBit for: {search_name}")
    except Exception as e:
        log(f"qBit delete error: {e}")


def handle_jellyfin_webhook(data):
    if data.get("NotificationType") != "ItemDeleted":
        return
    item_type = data.get("ItemType", "")
    tmdb = data.get("Provider_tmdb", "") or data.get("Item", {}).get("ProviderIds", {}).get("Tmdb", "")
    tvdb = data.get("Provider_tvdb", "") or data.get("Item", {}).get("ProviderIds", {}).get("Tvdb", "")
    name = data.get("Name", "?")
    if item_type == "Movie" and tmdb:
        try:
            tid = int(tmdb)
            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies:
                for m in movies:
                    if m.get("tmdbId") == tid:
                        api_delete(f"{RADARR_URL}/api/v3/movie/{m['id']}?deleteFiles=true", RADARR_KEY)
                        log(f"Deleted from Radarr: {name}")
                        _qb_delete_torrents(m.get("title", name))
                        send_telegram(f"🗑 <b>Удалено:</b> {name}\nRadarr + торренты очищены")
                        return
        except ValueError: pass
    elif item_type in ("Series", "Season", "Episode") and tvdb:
        try:
            tid = int(tvdb)
            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series:
                for s in series:
                    if s.get("tvdbId") == tid:
                        api_delete(f"{SONARR_URL}/api/v3/series/{s['id']}?deleteFiles=true", SONARR_KEY)
                        log(f"Deleted from Sonarr: {name}")
                        _qb_delete_torrents(s.get("title", name))
                        send_telegram(f"🗑 <b>Удалено:</b> {name}\nSonarr + торренты очищены")
                        return
        except ValueError: pass


def handle_prowlarr_webhook(data):
    # Prowlarr grabs already notified via Sonarr/Radarr webhooks — log only
    if data.get("eventType") == "Grab":
        rel = data.get("release", {})
        log(f"Prowlarr grab (silent): {rel.get('releaseTitle','?')[:50]}")


def handle_jellyseerr_webhook(data):
    ntype = data.get("notification_type", "")
    subject = data.get("subject", "")
    icon = "🎬" if data.get("media_type") == "movie" else "📺"
    msgs = {
        "MEDIA_PENDING": f"📋 <b>Запрос:</b> {subject}\n{icon} Ожидает одобрения",
        "MEDIA_APPROVED": f"👍 <b>Одобрено:</b> {subject}",
        "MEDIA_AUTO_APPROVED": f"👍 <b>Одобрено:</b> {subject}",
        "MEDIA_AVAILABLE": f"✅ <b>Доступно:</b> {subject}\n{icon} Можно смотреть",
        "MEDIA_FAILED": f"❌ <b>Ошибка:</b> {subject}",
        "MEDIA_DECLINED": f"🚫 <b>Отклонено:</b> {subject}",
    }
    if ntype in msgs:
        send_telegram(msgs[ntype])


# ── HTTP Server ──────────────────────────────────────────────────────────

class HubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers(); self.wfile.write(b"OK")
        try: data = json.loads(body)
        except: return
        h = {"/radarr": handle_radarr_webhook, "/sonarr": handle_sonarr_webhook, "/jellyfin": handle_jellyfin_webhook,
             "/webhook": handle_jellyfin_webhook, "/prowlarr": handle_prowlarr_webhook, "/jellyseerr": handle_jellyseerr_webhook}
        if self.path in h:
            log(f"Webhook {self.path}: {data.get('eventType', data.get('NotificationType', data.get('notification_type', '?')))}")
            h[self.path](data)

    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps({"service": "media-hub", "movies": len(state.movies), "series": len(state.series), "queue": len(state.queue_items)}).encode())

    def log_message(self, *a): pass


# ── Auto-search ──────────────────────────────────────────────────────────

def auto_search():
    series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
    if not series: return
    continuing = [s for s in series if s.get("status") == "continuing" and s.get("monitored")]
    if not continuing: return
    log(f"Auto-search: {len(continuing)} continuing series")
    for s in continuing:
        api_post(f"{SONARR_URL}/api/v3/command", SONARR_KEY, {"name": "SeriesSearch", "seriesId": s["id"]})
        log(f"Auto-search: {s.get('title', '?')}")
        time.sleep(5)


# ── Telegram polling ─────────────────────────────────────────────────────

def telegram_loop():
    log("Telegram bot started")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=5"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
            if not result or not result.get("ok"):
                time.sleep(2)
                continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    threading.Thread(target=handle_bot_message, args=(update["message"],), daemon=True).start()
                elif "callback_query" in update:
                    threading.Thread(target=handle_callback, args=(update["callback_query"],), daemon=True).start()
        except Exception as e:
            log(f"TG poll error: {e}")
            time.sleep(5)


# ── Main polling ─────────────────────────────────────────────────────────

def poll_loop():
    log("Poller started")
    time.sleep(30)
    while True:
        try:
            rq = api_get(f"{RADARR_URL}/api/v3/queue/details", RADARR_KEY)
            sq = api_get(f"{SONARR_URL}/api/v3/queue/details", SONARR_KEY)
            q = (rq or []) + (sq or [])
            state.update_queue(q) if q else state.update_queue([])
            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies: state.update_movies(movies)
            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series: state.update_series(series)
            if not state.initialized:
                state.initialized = True
                log(f"State: {len(state.movies)}m {len(state.series)}s {len(state.queue_items)}q")
            now = time.time()
            if now - state.last_auto_search > AUTO_SEARCH_HOURS * 3600:
                state.last_auto_search = now
                if state.initialized: auto_search()
            time.sleep(30 if q else 120)
        except Exception as e:
            log(f"Poll: {e}\n{traceback.format_exc()}")
            time.sleep(60)


def main():
    log("Media Hub v2 starting...")
    log(f"Radarr: {RADARR_URL} | Sonarr: {SONARR_URL} | Prowlarr: {PROWLARR_URL}")
    log(f"Telegram: {'configured' if TELEGRAM_TOKEN else 'NOT'} | Auto-search: {AUTO_SEARCH_HOURS}h")
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", PORT), HubHandler)
    log(f"Listening :{PORT} | /radarr /sonarr /prowlarr /jellyfin /jellyseerr")
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()

if __name__ == "__main__":
    main()
