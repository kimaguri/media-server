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


def send_telegram(text, reply_markup=None):
    log(f"TG: {text[:80]}")
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        data["reply_markup"] = reply_markup
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
            if prev and self.initialized:
                n = prev.get("ns")
                if status == "released" and not has_file and n != "nf":
                    send_telegram(f"❌ <b>Не найдено:</b> {title}\nНет подходящих релизов")
                    self.movies[mid]["ns"] = "nf"
                    continue
                if status in ("announced", "inCinemas") and n != f"w_{status}":
                    label = "Ожидаем выхода" if status == "announced" else "В кинотеатрах"
                    send_telegram(f"⏳ <b>{label}:</b> {title} ({m.get('year', '')})")
                    self.movies[mid]["ns"] = f"w_{status}"
                    continue
                if has_file and not prev.get("hasFile"):
                    send_telegram(f"✅ <b>Готово:</b> {title}\nДоступно в Jellyfin")
                    self.movies[mid]["ns"] = "done"
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
                for m in [25, 50, 75]:
                    if pct >= m: ms.add(m)
            else:
                for m in [25, 50, 75]:
                    if pct >= m and m not in ms:
                        send_telegram(f"📊 <b>Прогресс:</b> {title}\n{pct}% ({fmt_size(size - left)}/{fmt_size(size)})")
                        ms.add(m)
                        break
            self.notified_progress[dl_id] = ms
            self.queue_items[dl_id] = {"title": title, "pct": pct}
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
    # Limit to 8, filter only with seeders
    filtered = [r for r in results if (r.get("seeders") or 0) > 0][:8]
    if not filtered:
        filtered = results[:5]

    lines = [f"🔍 <b>{query}</b>\n"]
    buttons = []
    state.search_results[callback_prefix] = filtered

    for i, r in enumerate(filtered):
        idx = r.get("indexer", "?")
        title = r.get("title", "?")[:65]
        size = fmt_size(r.get("size", 0))
        seeders = r.get("seeders", 0)
        cat_names = ", ".join([c.get("name", "") for c in r.get("categories", []) if c.get("name")])[:20]
        icon = "🟢" if seeders >= 10 else "🟡" if seeders >= 3 else "🔴"
        lines.append(f"{icon} <b>{i+1}.</b> {size} 🌱{seeders} <i>{idx}</i>\n{title}\n")
        buttons.append([{"text": f"{i+1}. {size} 🌱{seeders} — {idx}", "callback_data": f"{callback_prefix}:{i}"}])

    buttons.append([{"text": "❌ Отмена", "callback_data": f"{callback_prefix}:cancel"}])
    send_telegram("\n".join(lines), {"inline_keyboard": buttons})


def grab_from_prowlarr(release):
    """Download a release found via Prowlarr."""
    guid = release.get("guid", "")
    indexer_id = release.get("indexerId", 0)
    download_url = release.get("downloadUrl", "")
    # Try direct grab via Prowlarr
    result = api_post(f"{PROWLARR_URL}/api/v1/search", PROWLARR_KEY, {
        "guid": guid, "indexerId": indexer_id
    })
    if not result:
        # Fallback: use download URL directly with qBittorrent
        log(f"Prowlarr grab failed, using downloadUrl")
        if download_url:
            return download_url
    return result


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
    if text == "/start" or text == "/help":
        send_telegram(
            "🎬 <b>Media Hub Bot</b>\n\n"
            "/search — поиск фильма, сериала или матча\n"
            "/status — статус скачиваний\n"
            "/list — список отслеживаемого\n\n"
            "Или просто напиши название для поиска",
            {"inline_keyboard": [
                [{"text": "🔍 Поиск", "callback_data": "menu:search"}],
                [{"text": "📊 Статус", "callback_data": "menu:status"}, {"text": "📋 Список", "callback_data": "menu:list"}],
            ]}
        )
        return

    if text == "/search":
        send_telegram("Что ищем?", {"inline_keyboard": [
            [{"text": "🎬 Фильм", "callback_data": "type:movie"},
             {"text": "📺 Сериал", "callback_data": "type:series"},
             {"text": "⚽ Матч", "callback_data": "type:sport"}],
        ]})
        return

    if text == "/status":
        show_status()
        return

    if text == "/list":
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

    # Menu actions
    if data == "menu:search":
        answer_callback(cb_id)
        send_telegram("Что ищем?", {"inline_keyboard": [
            [{"text": "🎬 Фильм", "callback_data": "type:movie"},
             {"text": "📺 Сериал", "callback_data": "type:series"},
             {"text": "⚽ Матч", "callback_data": "type:sport"}],
        ]})
        return

    if data == "menu:status":
        answer_callback(cb_id)
        show_status()
        return

    if data == "menu:list":
        answer_callback(cb_id)
        show_list()
        return

    # Search type selected
    if data.startswith("type:"):
        search_type = data.split(":")[1]
        labels = {"movie": "фильма", "series": "сериала", "sport": "матча"}
        answer_callback(cb_id)
        send_telegram(f"Введите название {labels.get(search_type, '')}:")
        state.user_state[chat_id] = {"waiting_query": search_type}
        return

    # Release selection from search results
    if ":" in data:
        prefix, action = data.rsplit(":", 1)
        if prefix in state.search_results:
            if action == "cancel":
                answer_callback(cb_id, "Отменено")
                edit_message(msg.get("message_id"), "❌ Поиск отменён")
                state.search_results.pop(prefix, None)
                return
            try:
                idx = int(action)
                releases = state.search_results.get(prefix, [])
                if 0 <= idx < len(releases):
                    rel = releases[idx]
                    answer_callback(cb_id, "Скачиваем...")
                    title = rel.get("title", "?")[:60]
                    edit_message(msg.get("message_id"), f"⬇️ <b>Скачиваем:</b>\n{title}")
                    threading.Thread(target=do_grab, args=(rel,), daemon=True).start()
                    state.search_results.pop(prefix, None)
                    return
            except (ValueError, IndexError):
                pass

    answer_callback(cb_id)


def do_search(query, search_type):
    """Perform search in background thread."""
    send_telegram(f"🔍 <b>Ищем:</b> {query}...")
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


def do_grab(release):
    """Grab a release in background thread."""
    download_url = release.get("downloadUrl", "")
    if not download_url:
        send_telegram("❌ Нет ссылки для скачивания")
        return
    # Send to Prowlarr download client
    indexer_id = release.get("indexerId", 0)
    guid = release.get("guid", "")
    result = api_post(f"{PROWLARR_URL}/api/v1/search", PROWLARR_KEY, {
        "guid": guid, "indexerId": indexer_id,
    })
    if result:
        send_telegram(f"✅ <b>Отправлено в qBittorrent</b>")
    else:
        # Fallback: grab via download URL
        log(f"Prowlarr grab failed, trying downloadUrl directly")
        send_telegram(f"⚠️ Прямая загрузка не через Prowlarr — попробуйте через Prowlarr UI")


def show_status():
    lines = ["📊 <b>Статус скачиваний</b>\n"]
    if state.queue_items:
        for dl_id, info in state.queue_items.items():
            lines.append(f"⬇️ {info['title'][:50]}\n   {info['pct']}%\n")
    else:
        lines.append("Нет активных скачиваний")
    send_telegram("\n".join(lines))


def show_list():
    lines = ["📋 <b>Отслеживаемое</b>\n"]
    if state.movies:
        lines.append("<b>Фильмы:</b>")
        for m in state.movies.values():
            icon = "✅" if m["hasFile"] else "⏳" if m["status"] in ("announced", "inCinemas") else "🔍"
            lines.append(f"  {icon} {m['title']}")
    if state.series:
        lines.append("\n<b>Сериалы:</b>")
        for s in state.series.values():
            lines.append(f"  📺 {s['title']} ({s['files']}/{s['total']})")
    if not state.movies and not state.series:
        lines.append("Пусто")
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
        send_telegram(f"🎬 <b>Добавлен фильм:</b> {title}\nИщем релизы...")
        threading.Thread(target=do_search, args=(title, "movie"), daemon=True).start()
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
        send_telegram(f"📺 <b>Добавлен сериал:</b> {title}\nИщем релизы...")
        threading.Thread(target=do_search, args=(title, "series"), daemon=True).start()
    elif event == "SeriesDelete":
        send_telegram(f"🗑 <b>Удалён:</b> {title}")


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
                        return
        except ValueError: pass


def handle_prowlarr_webhook(data):
    if data.get("eventType") == "Grab":
        rel = data.get("release", {})
        send_telegram(f"⬇️ <b>Качаем:</b> {rel.get('releaseTitle','?')}\n{fmt_size(rel.get('size',0))} • {rel.get('indexer','?')}")


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
            result = tg("getUpdates", {"offset": offset, "timeout": 30})
            if not result or not result.get("ok"):
                time.sleep(5); continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_bot_message(update["message"])
                elif "callback_query" in update:
                    handle_callback(update["callback_query"])
        except Exception as e:
            log(f"TG poll error: {e}")
            time.sleep(10)


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
