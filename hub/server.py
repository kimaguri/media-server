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
TEAMS_FILE = os.environ.get("TEAMS_FILE", "/data/teams.json")
SPORTSDB_API = "https://www.thesportsdb.com/api/v1/json/3"

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
    ["🔍 Поиск", "📊 Статус"],
    ["📋 Список", "⚽ Матчи"],
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
    """Determine if release is movie, series, or sport based on categories and title."""
    cats = release.get("categories", [])
    cat_ids = [c.get("id", 0) for c in cats]
    cat_names = " ".join(c.get("name", "") for c in cats)
    title = release.get("title", "")

    # Anime categories (5070=TV/Anime, 127720=Anime) → always series
    if any(cid in (5070, 127720) for cid in cat_ids):
        return "series"
    # Title has season/episode pattern → series (even if tagged as Sport)
    if re.search(r'S\d+E?\d*|Season\s*\d+', title, re.IGNORECASE):
        return "series"
    if "Movie" in cat_names:
        return "movie"
    if "TV" in cat_names:
        return "series"
    if "Sport" in cat_names:
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


def _detect_season(title):
    """Extract season number from release title. Returns int or None."""
    m = re.search(r'S(\d+)', title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r'Season\s*(\d+)', title, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def ensure_in_sonarr(release):
    """Ensure series exists in Sonarr. Add via lookup if missing. Returns series ID or None.
    Only monitors the season from the release title, not all seasons."""
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

    # Detect which season the user wants from the release title
    target_season = _detect_season(title)

    # Build seasons list — only monitor the target season (+ specials off)
    seasons = []
    for s in match.get("seasons", []):
        sn = s.get("seasonNumber", 0)
        if sn == 0:
            seasons.append({"seasonNumber": 0, "monitored": False})
        elif target_season and sn == target_season:
            seasons.append({"seasonNumber": sn, "monitored": True})
        elif not target_season:
            # No season detected in title — monitor all (fallback)
            seasons.append({"seasonNumber": sn, "monitored": True})
        else:
            seasons.append({"seasonNumber": sn, "monitored": False})

    # Add to Sonarr
    payload = {
        "tvdbId": match["tvdbId"],
        "title": match.get("title", search),
        "titleSlug": match.get("titleSlug", ""),
        "images": match.get("images", []),
        "seasons": seasons,
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
        season_info = f" (season {target_season})" if target_season else " (all seasons)"
        log(f"Added to Sonarr: {match.get('title')} (tvdb:{match['tvdbId']}){season_info}")
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
        send_telegram("🎬 <b>Media Hub Bot</b>\n\nИспользуй кнопки внизу для управления.\n🔍 Поиск — найти и скачать\n📊 Статус — что качается\n📋 Список — что мониторится")
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

    if text in ("⚽ Матчи", "/matches"):
        threading.Thread(target=show_matches, daemon=True).start()
        return

    # If user is in search mode, treat text as search query or team add
    user = state.user_state.get(chat_id, {})
    search_type = user.get("waiting_query")
    if search_type:
        state.user_state.pop(chat_id, None)
        if search_type == "add_team":
            send_telegram(f"🔍 Ищу команду: {text}...")
            threading.Thread(target=_add_team_via_search, args=(text,), daemon=True).start()
        else:
            threading.Thread(target=do_search, args=(text, search_type), daemon=True).start()
        return

    # No free-text search — always require category selection
    send_telegram("Сначала выбери категорию:", {"inline_keyboard": [
        [{"text": "🎬 Фильм", "callback_data": "type:movie"},
         {"text": "📺 Сериал", "callback_data": "type:series"},
         {"text": "⚽ Матч", "callback_data": "type:sport"}],
    ]})


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

    # Cancel all downloads
    if data == "cancel_all_dl":
        answer_callback(cb_id, "Останавливаю все загрузки...")
        edit_message(msg.get("message_id"), "🛑 Останавливаю все загрузки...")
        threading.Thread(target=do_cancel_all_downloads, daemon=True).start()
        return

    # Matches refresh
    if data == "matches:refresh":
        answer_callback(cb_id, "Обновляю...")
        threading.Thread(target=show_matches, daemon=True).start()
        return

    # Track/remove team
    if data.startswith("track_team:"):
        parts = data.split(":", 2)
        if len(parts) >= 3:
            action, value = parts[1], parts[2]
            if action == "skip":
                answer_callback(cb_id)
                edit_message(msg.get("message_id"), "👌")
            elif action == "add":
                answer_callback(cb_id, f"Ищу {value}...")
                threading.Thread(target=_add_team, args=(value, msg.get("message_id")), daemon=True).start()
            elif action == "remove":
                answer_callback(cb_id, "Удалено")
                threading.Thread(target=_remove_team, args=(value, msg.get("message_id")), daemon=True).start()
        return

    # Add team prompt
    if data == "matches:add_team":
        answer_callback(cb_id)
        send_telegram("Введите название команды:", {"keyboard": [["❌ Отмена"]], "resize_keyboard": True})
        state.user_state[chat_id] = {"waiting_query": "add_team"}
        return

    # Cancel single download
    if data.startswith("cancel_dl:"):
        hash_prefix = data.split(":", 1)[1]
        answer_callback(cb_id, "Останавливаю...")
        edit_message(msg.get("message_id"), "🛑 Останавливаю загрузку...")
        threading.Thread(target=do_cancel_download, args=(hash_prefix,), daemon=True).start()
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


def _fix_prowlarr_torrent_category(release, expected_cat):
    """After Prowlarr grabs a torrent, fix its qBit category (Prowlarr hardcodes 'sports')."""
    time.sleep(10)  # Wait for torrent to appear in qBit
    try:
        torrents, cookie = _qb_session()
        if not torrents or not cookie:
            return
        title_lower = release.get("title", "").lower()[:30]
        for t in torrents:
            if title_lower and title_lower in t.get("name", "").lower():
                if t.get("category") != expected_cat:
                    qb_url = "http://qbittorrent:8080/api/v2"
                    data = urllib.parse.urlencode({"hashes": t["hash"], "category": expected_cat}).encode()
                    req = urllib.request.Request(f"{qb_url}/torrents/setCategory", data=data)
                    req.add_header("Cookie", cookie)
                    urllib.request.urlopen(req, timeout=5)
                    log(f"Fixed qBit category: {t['name'][:40]} → {expected_cat}")
                return
    except Exception as e:
        log(f"Fix category error: {e}")


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
        threading.Thread(target=_fix_prowlarr_torrent_category, args=(release, "movies"), daemon=True).start()
    elif rtype == "series":
        sid = ensure_in_sonarr(release)
        if sid and push_to_sonarr(release, series_id=sid):
            return
        log(f"Sonarr route failed for {title}, falling back to Prowlarr")
        push_to_prowlarr(release)
        threading.Thread(target=_fix_prowlarr_torrent_category, args=(release, "tv"), daemon=True).start()
    else:
        # Sport — use Prowlarr directly (category stays "sports")
        push_to_prowlarr(release)
        # Ask about team tracking
        _offer_team_tracking(release)


# ── Teams tracking (TheSportsDB) ─────────────────────────────────────────

def _load_teams():
    """Load tracked teams from JSON file."""
    try:
        with open(TEAMS_FILE, "r") as f:
            return json.loads(f.read())
    except Exception:
        return []


def _save_teams(teams):
    """Save tracked teams to JSON file."""
    try:
        os.makedirs(os.path.dirname(TEAMS_FILE), exist_ok=True)
        with open(TEAMS_FILE, "w") as f:
            f.write(json.dumps(teams, ensure_ascii=False, indent=2))
    except Exception as e:
        log(f"Save teams error: {e}")


def _sportsdb_get(endpoint):
    """Call TheSportsDB free API."""
    try:
        url = f"{SPORTSDB_API}/{endpoint}"
        req = urllib.request.Request(url, headers={"User-Agent": "media-hub/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"TheSportsDB error: {endpoint}: {e}")
        return None


def _search_team(name):
    """Search for a team by name. Returns list of matches."""
    data = _sportsdb_get(f"searchteams.php?t={urllib.parse.quote(name)}")
    if not data or not data.get("teams"):
        return []
    return data["teams"]


def _get_team_next_events(team_id):
    """Get upcoming events for a team (all tournaments)."""
    data = _sportsdb_get(f"eventsnext.php?id={team_id}")
    if not data or not data.get("events"):
        return []
    return data["events"]


def _add_team(team_name, message_id):
    """Search TheSportsDB for team, add to tracked list."""
    results = _search_team(team_name)
    if not results:
        edit_message(message_id, f"❌ Команда \"{team_name}\" не найдена")
        return

    team = results[0]
    tid = team.get("idTeam", "")
    tname = team.get("strTeam", team_name)
    sport = team.get("strSport", "?")
    league = team.get("strLeague", "?")

    teams = _load_teams()
    # Check not already tracked
    if any(t.get("id") == tid for t in teams):
        edit_message(message_id, f"✅ {tname} уже отслеживается")
        return

    teams.append({"id": tid, "name": tname, "sport": sport, "league": league})
    _save_teams(teams)

    # Get upcoming matches
    events = _get_team_next_events(tid)
    lines = [f"✅ <b>{tname}</b> добавлен для отслеживания\n🏆 {league} • {sport}\n"]
    if events:
        lines.append(f"<b>Ближайшие матчи:</b>")
        for e in events[:5]:
            date = (e.get("dateEvent") or "?")
            lines.append(f"  📅 {date} • {e.get('strEvent', '?')}")
            lines.append(f"     🏆 {e.get('strLeague', '')}")
    else:
        lines.append("Нет предстоящих матчей")

    edit_message(message_id, "\n".join(lines))
    log(f"Team tracked: {tname} (id:{tid})")


def _remove_team(team_id, message_id):
    """Remove team from tracked list."""
    teams = _load_teams()
    removed = [t for t in teams if t.get("id") == team_id]
    teams = [t for t in teams if t.get("id") != team_id]
    _save_teams(teams)
    name = removed[0]["name"] if removed else "?"
    edit_message(message_id, f"🗑 {name} удалён из отслеживания")
    log(f"Team untracked: {name}")


def _add_team_via_search(query):
    """Search TheSportsDB and offer team selection."""
    results = _search_team(query)
    if not results:
        send_telegram(f"❌ Ничего не найдено по запросу \"{query}\"")
        return

    teams = _load_teams()
    tracked_ids = {t.get("id") for t in teams}

    lines = [f"⚽ <b>Результаты: {query}</b>\n"]
    buttons = []
    for t in results[:8]:
        tid = t.get("idTeam", "")
        name = t.get("strTeam", "?")
        sport = t.get("strSport", "?")
        league = t.get("strLeague", "?")
        country = t.get("strCountry", "")
        already = " ✅" if tid in tracked_ids else ""
        lines.append(f"  • {name} ({sport}, {league}){already}")
        if tid not in tracked_ids:
            buttons.append([{"text": f"📌 {name} — {sport}", "callback_data": f"track_team:add:{name}"}])

    if not buttons:
        lines.append("\nВсе команды уже отслеживаются!")
    send_telegram("\n".join(lines), reply_markup={"inline_keyboard": buttons} if buttons else None)


def _offer_team_tracking(release):
    """After downloading a sport match, offer to track teams involved."""
    title = release.get("title", "")
    # Extract team names: "Team1 vs Team2", "Team1 - Team2"
    m = re.search(r'(.+?)\s+(?:vs?\.?|[-–])\s+(.+?)(?:\s+\d{4}|\s+\(|\s+S\d|$)', title, re.IGNORECASE)
    if not m:
        return
    team1 = m.group(1).strip()[:30]
    team2 = m.group(2).strip()[:30]
    for prefix in ("[", "{"):
        if prefix in team1:
            team1 = team1.split("]")[-1].split("}")[-1].strip()

    # Check if already tracked
    teams = _load_teams()
    tracked_names = {t.get("name", "").lower() for t in teams}

    buttons = []
    if team1 and team1.lower() not in tracked_names:
        buttons.append([{"text": f"📌 Следить: {team1}", "callback_data": f"track_team:add:{team1}"}])
    if team2 and team2.lower() not in tracked_names:
        buttons.append([{"text": f"📌 Следить: {team2}", "callback_data": f"track_team:add:{team2}"}])

    if not buttons:
        return  # Both already tracked

    buttons.append([{"text": "Нет, спасибо", "callback_data": "track_team:skip:skip"}])
    send_telegram(
        f"⚽ Отслеживать матчи этих команд?\nБот будет присылать расписание.",
        reply_markup={"inline_keyboard": buttons}
    )


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


def _qb_session():
    """Login to qBittorrent, return (torrents_list, cookie) or ([], None)."""
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
            return json.loads(resp2.read()), cookie
    except Exception as e:
        log(f"QB session error: {e}")
        return [], None


def show_status():
    lines = ["📊 <b>Статус</b>\n"]

    torrents, _ = _qb_session()
    if torrents is None or _ is None:
        lines.append("qBittorrent недоступен")
        send_telegram("\n".join(lines))
        return

    if not torrents:
        lines.append("Нет активных торрентов")
        send_telegram("\n".join(lines))
        return

    downloading, moving, seeding, other = [], [], [], []
    cancel_buttons = []

    for t in torrents:
        name = t.get("name", "?")[:50]
        h = t.get("hash", "")
        pct = int(t.get("progress", 0) * 100)
        st = t.get("state", "")
        speed_dl = t.get("dlspeed", 0) / (1024 * 1024)
        size = fmt_size(t.get("total_size", 0))
        uploaded = fmt_size(t.get("uploaded", 0))
        ratio = t.get("ratio", 0)

        if st in ("downloading", "forcedDL", "stalledDL", "metaDL", "allocating"):
            downloading.append(f"  ⬇️ {name}\n     {pct}% • {speed_dl:.1f} MB/s • {size}")
            cancel_buttons.append([{"text": f"🛑 {name[:40]}", "callback_data": f"cancel_dl:{h[:20]}"}])
        elif st == "moving":
            moving.append(f"  📦 {name}\n     Перемещение...")
        elif st in ("uploading", "forcedUP", "stalledUP", "queuedUP"):
            seeding.append(f"  🌱 {name}\n     {uploaded} отдано • {ratio:.1f}x")
        elif st in ("checkingDL", "checkingUP", "checkingResumeData"):
            other.append(f"  🔍 {name}\n     Проверка...")
        elif st in ("pausedDL", "pausedUP"):
            other.append(f"  ⏸ {name}\n     Пауза ({pct}%)")
            cancel_buttons.append([{"text": f"🛑 {name[:40]}", "callback_data": f"cancel_dl:{h[:20]}"}])
        elif st == "queuedDL":
            other.append(f"  🕐 {name}\n     В очереди")
            cancel_buttons.append([{"text": f"🛑 {name[:40]}", "callback_data": f"cancel_dl:{h[:20]}"}])

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

    if cancel_buttons:
        if len(cancel_buttons) > 1:
            cancel_buttons.append([{"text": "🛑 Остановить все загрузки", "callback_data": "cancel_all_dl"}])
        markup = {"inline_keyboard": cancel_buttons}
    else:
        markup = None
    send_telegram("\n".join(lines), reply_markup=markup)


def do_cancel_download(hash_prefix):
    """Cancel a download: delete torrent + files from qBit, clean up Sonarr/Radarr."""
    torrents, cookie = _qb_session()
    if not torrents or not cookie:
        send_telegram("❌ qBittorrent недоступен")
        return

    # Find torrent by hash prefix
    target = None
    for t in torrents:
        if t.get("hash", "").startswith(hash_prefix):
            target = t
            break
    if not target:
        send_telegram("❌ Торрент не найден (уже удалён?)")
        return

    name = target.get("name", "?")
    full_hash = target.get("hash", "")

    # Delete torrent from qBittorrent with files
    try:
        qb_url = "http://qbittorrent:8080/api/v2"
        data = urllib.parse.urlencode({"hashes": full_hash, "deleteFiles": "true"}).encode()
        req = urllib.request.Request(f"{qb_url}/torrents/delete", data=data)
        req.add_header("Cookie", cookie)
        urllib.request.urlopen(req, timeout=10)
        log(f"Cancelled download: {name[:50]}")
    except Exception as e:
        log(f"Cancel torrent error: {e}")

    # Clean up Sonarr queue (if torrent was managed by Sonarr)
    sq = api_get(f"{SONARR_URL}/api/v3/queue/details", SONARR_KEY) or []
    for item in sq:
        if item.get("downloadId", "").lower() == full_hash.lower():
            api_delete(f"{SONARR_URL}/api/v3/queue/{item['id']}?removeFromClient=false&blocklist=false", SONARR_KEY)
            log(f"Removed from Sonarr queue: {item.get('title','?')[:50]}")

    # Clean up Radarr queue
    rq = api_get(f"{RADARR_URL}/api/v3/queue/details", RADARR_KEY) or []
    for item in rq:
        if item.get("downloadId", "").lower() == full_hash.lower():
            api_delete(f"{RADARR_URL}/api/v3/queue/{item['id']}?removeFromClient=false&blocklist=false", RADARR_KEY)
            log(f"Removed from Radarr queue: {item.get('title','?')[:50]}")

    send_telegram(f"🛑 <b>Остановлено:</b> {name[:50]}\nТоррент и файлы удалены")


def do_cancel_all_downloads():
    """Cancel ALL active downloads (not seeding). Delete torrents + files, clean queues."""
    torrents, cookie = _qb_session()
    if not torrents or not cookie:
        send_telegram("❌ qBittorrent недоступен")
        return

    dl_states = ("downloading", "forcedDL", "stalledDL", "metaDL", "allocating", "pausedDL", "queuedDL")
    active = [t for t in torrents if t.get("state", "") in dl_states]

    if not active:
        send_telegram("Нет активных загрузок для остановки")
        return

    hashes = [t["hash"] for t in active]
    names = [t.get("name", "?")[:40] for t in active]

    # Delete all from qBittorrent
    try:
        qb_url = "http://qbittorrent:8080/api/v2"
        data = urllib.parse.urlencode({"hashes": "|".join(hashes), "deleteFiles": "true"}).encode()
        req = urllib.request.Request(f"{qb_url}/torrents/delete", data=data)
        req.add_header("Cookie", cookie)
        urllib.request.urlopen(req, timeout=10)
        log(f"Cancelled all downloads: {len(hashes)} torrents")
    except Exception as e:
        log(f"Cancel all error: {e}")

    # Clean Sonarr/Radarr queues
    hash_set = {h.lower() for h in hashes}
    sq = api_get(f"{SONARR_URL}/api/v3/queue/details", SONARR_KEY) or []
    for item in sq:
        if item.get("downloadId", "").lower() in hash_set:
            api_delete(f"{SONARR_URL}/api/v3/queue/{item['id']}?removeFromClient=false&blocklist=false", SONARR_KEY)

    rq = api_get(f"{RADARR_URL}/api/v3/queue/details", RADARR_KEY) or []
    for item in rq:
        if item.get("downloadId", "").lower() in hash_set:
            api_delete(f"{RADARR_URL}/api/v3/queue/{item['id']}?removeFromClient=false&blocklist=false", RADARR_KEY)

    lines = [f"🛑 <b>Остановлено {len(hashes)} загрузок:</b>\n"]
    for n in names:
        lines.append(f"  • {n}")
    lines.append("\nТорренты и файлы удалены. Раздачи не тронуты.")
    send_telegram("\n".join(lines))


def show_matches():
    """Show upcoming matches for tracked teams via TheSportsDB."""
    teams = _load_teams()
    lines = ["⚽ <b>Матчи</b>\n"]
    buttons = []
    api_ok = True

    if not teams:
        lines.append("Нет отслеживаемых команд.\nСкачай матч через 🔍 Поиск → ⚽ Матч — бот предложит отслеживать команду.\n")
        buttons.append([{"text": "➕ Добавить команду", "callback_data": "matches:add_team"}])
        send_telegram("\n".join(lines), reply_markup={"inline_keyboard": buttons})
        return

    for team in teams:
        tid = team.get("id", "")
        tname = team.get("name", "?")
        events = _get_team_next_events(tid)

        if events is None:
            api_ok = False
            lines.append(f"<b>📌 {tname}</b>")
            lines.append(f"  ⚠️ Ошибка получения расписания\n")
            continue

        lines.append(f"<b>📌 {tname}</b>")
        if events:
            for e in events[:5]:
                date = e.get("dateEvent", "?")
                time_str = (e.get("strTime") or "")[:5]
                event_name = e.get("strEvent", "?")
                league = e.get("strLeague", "")
                time_part = f" {time_str}" if time_str else ""
                lines.append(f"  📅 {date}{time_part} • {event_name}")
                if league:
                    lines.append(f"     🏆 {league}")
        else:
            lines.append(f"  Нет предстоящих матчей")
        lines.append("")

    if not api_ok:
        lines.append("⚠️ <b>TheSportsDB API недоступен для некоторых команд</b>")

    # Show tracked teams with remove buttons
    lines.append(f"<b>📌 Отслеживаемые ({len(teams)}):</b>")
    for t in teams:
        lines.append(f"  • {t.get('name','?')} ({t.get('sport','?')})")
        buttons.append([{"text": f"🗑 {t['name'][:30]}", "callback_data": f"track_team:remove:{t['id']}"}])

    buttons.append([{"text": "➕ Добавить команду", "callback_data": "matches:add_team"}])
    buttons.append([{"text": "🔄 Обновить", "callback_data": "matches:refresh"}])
    send_telegram("\n".join(lines), reply_markup={"inline_keyboard": buttons})


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
        torrents, cookie = _qb_session()
        if not torrents or not cookie:
            return
        search_lower = search_name.lower()
        hashes = [t["hash"] for t in torrents if search_lower in t.get("name", "").lower()]
        if hashes:
            qb_url = "http://qbittorrent:8080/api/v2"
            data = urllib.parse.urlencode({"hashes": "|".join(hashes), "deleteFiles": "true"}).encode()
            req = urllib.request.Request(f"{qb_url}/torrents/delete", data=data)
            req.add_header("Cookie", cookie)
            urllib.request.urlopen(req, timeout=10)
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

def _get_release_group(series_id):
    """Get the most common release group for a series from existing episode files."""
    files = api_get(f"{SONARR_URL}/api/v3/episodefile?seriesId={series_id}", SONARR_KEY)
    if not files:
        return None
    groups = {}
    for f in files:
        rg = f.get("releaseGroup", "")
        if rg:
            groups[rg] = groups.get(rg, 0) + 1
    if not groups:
        return None
    # Return the most frequent release group
    return max(groups, key=groups.get)


def _ensure_release_profile(group, series_id):
    """Ensure a Sonarr release profile exists that prefers this release group for this series."""
    if not group:
        return
    profiles = api_get(f"{SONARR_URL}/api/v3/releaseprofile", SONARR_KEY) or []
    # Check if profile already exists for this group
    for p in profiles:
        preferred = p.get("preferred", [])
        for pref in preferred:
            if pref.get("key", "").lower() == group.lower():
                return  # Already exists
    # Create release profile with preferred word for this group
    payload = {
        "enabled": True,
        "required": [],
        "ignored": [],
        "preferred": [{"key": group, "value": 100}],
        "includePreferredWhenRenaming": False,
        "indexerId": 0,
        "tags": [],
    }
    result = api_post(f"{SONARR_URL}/api/v3/releaseprofile", SONARR_KEY, payload)
    if result and isinstance(result, dict):
        log(f"Created release profile: prefer [{group}] +100")


def auto_search():
    series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
    if not series: return
    # Only search continuing series that already have some files downloaded
    continuing = [s for s in series
                  if s.get("status") == "continuing"
                  and s.get("monitored")
                  and s.get("statistics", {}).get("episodeFileCount", 0) > 0]
    if not continuing: return
    log(f"Auto-search: {len(continuing)} continuing series")
    for s in continuing:
        # Set preferred release group based on existing files
        rg = _get_release_group(s["id"])
        if rg:
            _ensure_release_profile(rg, s["id"])
            log(f"Auto-search: {s.get('title', '?')} (prefer: {rg})")
        else:
            log(f"Auto-search: {s.get('title', '?')}")
        api_post(f"{SONARR_URL}/api/v3/command", SONARR_KEY, {"name": "SeriesSearch", "seriesId": s["id"]})
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
