#!/usr/bin/env python3
"""Media Hub — unified notification, cleanup, and automation service.

- Webhooks from Radarr, Sonarr, Prowlarr, Jellyseerr, Jellyfin
- Adaptive polling for download progress and status changes
- Telegram bot: notifications + interactive torrent selection
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
        log(f"API GET failed {url}: {e}")
        return None


def api_post(url, api_key, data=None):
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"API POST failed {url}: {e}")
        return None


def api_delete(url, api_key):
    req = urllib.request.Request(url, method="DELETE", headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception as e:
        log(f"API DELETE failed {url}: {e}")
        return None


def fmt_size(bytes_val):
    if not bytes_val:
        return "?"
    gb = bytes_val / (1024 ** 3)
    return f"{gb:.1f}GB" if gb >= 1 else f"{bytes_val / (1024 ** 2):.0f}MB"


# ── Telegram ─────────────────────────────────────────────────────────────

def tg_api(method, params=None):
    if not TELEGRAM_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    if params:
        data = urllib.parse.urlencode(params).encode()
    else:
        data = None
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"TG API {method} failed: {e}")
        return None


def send_telegram(text):
    log(f"TG: {text[:100]}")
    tg_api("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"})


# ── State tracker ────────────────────────────────────────────────────────

class StateTracker:
    def __init__(self):
        self.movies = {}
        self.series = {}
        self.queue_items = {}
        self.notified_progress = {}
        self.initialized = False
        self.pending_episodes = {}
        self.pending_lock = threading.Lock()
        # Interactive selection
        self.pending_choices = {}  # chat message context
        self.last_auto_search = 0

    def update_movies(self, movies):
        for m in movies:
            mid = m["id"]
            title = m.get("title", "?")
            status = m.get("status", "?")
            has_file = m.get("hasFile", False)
            monitored = m.get("monitored", False)
            if not monitored:
                continue
            prev = self.movies.get(mid)
            if prev and self.initialized:
                notified = prev.get("notified_status")
                if status == "released" and not has_file and notified != "no_file_released":
                    send_telegram(f"❌ <b>Не найдено:</b> {title}\nНет подходящих релизов")
                    self.movies[mid]["notified_status"] = "no_file_released"
                    continue
                if status in ("announced", "inCinemas") and notified != f"waiting_{status}":
                    year = m.get("year", "")
                    msg = f"⏳ <b>{'Ожидаем выхода' if status == 'announced' else 'В кинотеатрах'}:</b> {title} ({year})"
                    send_telegram(msg)
                    self.movies[mid]["notified_status"] = f"waiting_{status}"
                    continue
                if has_file and not prev.get("hasFile"):
                    send_telegram(f"✅ <b>Готово:</b> {title}\nДоступно в Jellyfin")
                    self.movies[mid]["notified_status"] = "done"
                    continue
            self.movies[mid] = {
                "title": title, "status": status, "hasFile": has_file,
                "notified_status": self.movies.get(mid, {}).get("notified_status"),
            }

    def update_series(self, series_list):
        for s in series_list:
            sid = s["id"]
            self.series[sid] = {
                "title": s.get("title", "?"),
                "status": s.get("status", "?"),
                "episodeFileCount": s.get("statistics", {}).get("episodeFileCount", 0),
                "totalEpisodeCount": s.get("statistics", {}).get("totalEpisodeCount", 0),
            }

    def update_queue(self, queue_items):
        current_ids = set()
        for item in queue_items:
            dl_id = item.get("downloadId", "")
            if not dl_id:
                continue
            current_ids.add(dl_id)
            title = item.get("title", "?")
            size = item.get("size", 0)
            sizeleft = item.get("sizeleft", 0)
            pct = int(((size - sizeleft) / size) * 100) if size > 0 else 0
            milestones = self.notified_progress.get(dl_id, set())
            if not self.initialized:
                for m in [25, 50, 75]:
                    if pct >= m:
                        milestones.add(m)
                self.notified_progress[dl_id] = milestones
                self.queue_items[dl_id] = {"title": title, "pct": pct}
                continue
            for m in [25, 50, 75]:
                if pct >= m and m not in milestones:
                    send_telegram(f"📊 <b>Прогресс:</b> {title}\n{pct}% ({fmt_size(size - sizeleft)}/{fmt_size(size)})")
                    milestones.add(m)
                    break
            self.notified_progress[dl_id] = milestones
            self.queue_items[dl_id] = {"title": title, "pct": pct}
        for dl_id in list(self.notified_progress.keys()):
            if dl_id not in current_ids:
                del self.notified_progress[dl_id]
        for dl_id in list(self.queue_items.keys()):
            if dl_id not in current_ids:
                del self.queue_items[dl_id]


state = StateTracker()

# ── Interactive torrent selection ────────────────────────────────────────

def search_releases_movie(movie_id):
    """Get available releases for a movie from Radarr."""
    return api_get(f"{RADARR_URL}/api/v3/release?movieId={movie_id}", RADARR_KEY, timeout=60)


def search_releases_series(series_id):
    """Get available releases for a series from Sonarr."""
    return api_get(f"{SONARR_URL}/api/v3/release?seriesId={series_id}", SONARR_KEY, timeout=60)


def grab_release(url, api_key, guid, indexer_id):
    """Grab a specific release by GUID."""
    return api_post(url, api_key, {"guid": guid, "indexerId": indexer_id})


def offer_movie_choice(movie_id, title):
    """Search for releases and send choices to Telegram."""
    threading.Thread(target=_do_movie_search, args=(movie_id, title), daemon=True).start()


def _do_movie_search(movie_id, title):
    send_telegram(f"🔍 <b>Ищем:</b> {title}...")
    releases = search_releases_movie(movie_id)
    if not releases:
        send_telegram(f"❌ <b>{title}:</b> ничего не найдено")
        return
    # Filter approved or show all, limit to 10
    good = [r for r in releases if r.get("approved")]
    if not good:
        good = releases[:10]
    else:
        good = good[:10]
    if not good:
        send_telegram(f"❌ <b>{title}:</b> все релизы отклонены по качеству/языку")
        return
    lines = [f"🎬 <b>{title}</b> — выберите номер:\n"]
    choice_data = []
    for i, r in enumerate(good, 1):
        q = r.get("quality", {}).get("quality", {}).get("name", "?")
        size = fmt_size(r.get("size", 0))
        seeders = r.get("seeders", "?")
        indexer = r.get("indexer", "?")
        rtitle = r.get("title", "?")[:60]
        approved = "✅" if r.get("approved") else "⚠️"
        lines.append(f"{approved} <b>{i}.</b> [{q}] {size} 🌱{seeders}\n{rtitle}\n<i>{indexer}</i>\n")
        choice_data.append({"guid": r.get("guid"), "indexerId": r.get("indexerId")})
    lines.append("\nОтправьте номер для скачивания или <b>0</b> для отмены")
    send_telegram("\n".join(lines))
    state.pending_choices[f"movie_{movie_id}"] = {
        "type": "movie", "id": movie_id, "title": title,
        "releases": choice_data, "expires": time.time() + 600,
    }


def offer_series_choice(series_id, title):
    """Search for releases and send choices to Telegram."""
    threading.Thread(target=_do_series_search, args=(series_id, title), daemon=True).start()


def _do_series_search(series_id, title):
    send_telegram(f"🔍 <b>Ищем:</b> {title}...")
    releases = search_releases_series(series_id)
    if not releases:
        send_telegram(f"❌ <b>{title}:</b> ничего не найдено")
        return
    good = [r for r in releases if r.get("approved")]
    if not good:
        good = releases[:10]
    else:
        good = good[:10]
    if not good:
        send_telegram(f"❌ <b>{title}:</b> все релизы отклонены")
        return
    lines = [f"📺 <b>{title}</b> — выберите номер:\n"]
    choice_data = []
    for i, r in enumerate(good, 1):
        q = r.get("quality", {}).get("quality", {}).get("name", "?")
        size = fmt_size(r.get("size", 0))
        seeders = r.get("seeders", "?")
        indexer = r.get("indexer", "?")
        rtitle = r.get("title", "?")[:60]
        approved = "✅" if r.get("approved") else "⚠️"
        lines.append(f"{approved} <b>{i}.</b> [{q}] {size} 🌱{seeders}\n{rtitle}\n<i>{indexer}</i>\n")
        choice_data.append({"guid": r.get("guid"), "indexerId": r.get("indexerId")})
    lines.append("\nОтправьте номер для скачивания или <b>0</b> для отмены")
    send_telegram("\n".join(lines))
    state.pending_choices[f"series_{series_id}"] = {
        "type": "series", "id": series_id, "title": title,
        "releases": choice_data, "expires": time.time() + 600,
    }


def handle_telegram_choice(text):
    """Process user's numeric choice from Telegram."""
    text = text.strip()
    if not text.isdigit():
        return False
    num = int(text)
    # Find active choice (most recent)
    active = None
    active_key = None
    for key, choice in list(state.pending_choices.items()):
        if time.time() > choice["expires"]:
            del state.pending_choices[key]
            continue
        active = choice
        active_key = key
    if not active:
        return False
    if num == 0:
        send_telegram(f"🚫 Отменено: {active['title']}")
        del state.pending_choices[active_key]
        return True
    idx = num - 1
    if idx < 0 or idx >= len(active["releases"]):
        send_telegram(f"❌ Неверный номер. Введите 1-{len(active['releases'])} или 0")
        return True
    rel = active["releases"][idx]
    if active["type"] == "movie":
        result = grab_release(f"{RADARR_URL}/api/v3/release", RADARR_KEY, rel["guid"], rel["indexerId"])
    else:
        result = grab_release(f"{SONARR_URL}/api/v3/release", SONARR_KEY, rel["guid"], rel["indexerId"])
    if result:
        send_telegram(f"⬇️ <b>Начинаем скачивание:</b> {active['title']}\nРелиз #{num}")
    else:
        send_telegram(f"❌ Ошибка при скачивании {active['title']}")
    del state.pending_choices[active_key]
    return True


# ── Webhook handlers ─────────────────────────────────────────────────────

def handle_radarr_webhook(data):
    event = data.get("eventType", "")
    movie = data.get("movie", {})
    title = movie.get("title", "?")
    movie_id = movie.get("id")
    release = data.get("release", {})

    if event == "Grab":
        q = release.get("quality", "?")
        send_telegram(f"⬇️ <b>Качаем фильм:</b> {title}\n{q} • {fmt_size(release.get('size', 0))} • {release.get('indexer', '?')}")
    elif event == "Download":
        if data.get("isUpgrade"):
            send_telegram(f"⬆️ <b>Обновлено:</b> {title}\nНовое качество в Jellyfin")
        else:
            send_telegram(f"✅ <b>Фильм готов:</b> {title}\nДоступно в Jellyfin")
    elif event == "MovieAdded":
        if movie_id:
            offer_movie_choice(movie_id, title)
        else:
            send_telegram(f"🎬 <b>Добавлен фильм:</b> {title}")
    elif event == "MovieDelete":
        send_telegram(f"🗑 <b>Удалён фильм:</b> {title}")
    elif event == "Health":
        send_telegram(f"⚠️ <b>Radarr:</b> {data.get('message', '?')}")


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
    series_id = series.get("id")
    episodes = data.get("episodes", [])
    release = data.get("release", {})

    if event == "Grab":
        q = release.get("quality", "?")
        ep_info = ""
        if episodes:
            nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes[:3]]
            ep_info = f" ({', '.join(nums)})"
        send_telegram(f"⬇️ <b>Качаем сериал:</b> {title}{ep_info}\n{q} • {fmt_size(release.get('size', 0))} • {release.get('indexer', '?')}")
    elif event == "Download":
        if data.get("isUpgrade"):
            ep_info = ""
            if episodes:
                nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes[:3]]
                ep_info = f" ({', '.join(nums)})"
            send_telegram(f"⬆️ <b>Обновлено:</b> {title}{ep_info}")
        else:
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
        if series_id:
            offer_series_choice(series_id, title)
        else:
            send_telegram(f"📺 <b>Добавлен сериал:</b> {title}")
    elif event == "SeriesDelete":
        send_telegram(f"🗑 <b>Удалён сериал:</b> {title}")
    elif event == "Health":
        send_telegram(f"⚠️ <b>Sonarr:</b> {data.get('message', '?')}")


def handle_jellyfin_webhook(data):
    if data.get("NotificationType") != "ItemDeleted":
        return
    item_type = data.get("ItemType", "")
    tmdb = data.get("Provider_tmdb", "") or data.get("Item", {}).get("ProviderIds", {}).get("Tmdb", "")
    tvdb = data.get("Provider_tvdb", "") or data.get("Item", {}).get("ProviderIds", {}).get("Tvdb", "")
    name = data.get("Name", data.get("SeriesName", "unknown"))
    if item_type == "Movie" and tmdb:
        try:
            tmdb_id = int(tmdb)
            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies:
                for m in movies:
                    if m.get("tmdbId") == tmdb_id:
                        api_delete(f"{RADARR_URL}/api/v3/movie/{m['id']}?deleteFiles=true", RADARR_KEY)
                        log(f"Deleted from Radarr: {name}")
                        return
        except ValueError:
            pass
    elif item_type in ("Series", "Season", "Episode") and tvdb:
        try:
            tvdb_id = int(tvdb)
            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series:
                for s in series:
                    if s.get("tvdbId") == tvdb_id:
                        api_delete(f"{SONARR_URL}/api/v3/series/{s['id']}?deleteFiles=true", SONARR_KEY)
                        log(f"Deleted from Sonarr: {name}")
                        return
        except ValueError:
            pass


def handle_prowlarr_webhook(data):
    if data.get("eventType") == "Grab":
        rel = data.get("release", {})
        send_telegram(f"⬇️ <b>Качаем:</b> {rel.get('releaseTitle', '?')}\n{fmt_size(rel.get('size', 0))} • {rel.get('indexer', '?')}")


def handle_jellyseerr_webhook(data):
    ntype = data.get("notification_type", "")
    subject = data.get("subject", "")
    icon = "🎬" if data.get("media_type") == "movie" else "📺"
    if ntype == "MEDIA_PENDING":
        send_telegram(f"📋 <b>Запрос:</b> {subject}\n{icon} Ожидает одобрения")
    elif ntype in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
        send_telegram(f"👍 <b>Одобрено:</b> {subject}\n{icon} Отправлено на поиск")
    elif ntype == "MEDIA_AVAILABLE":
        send_telegram(f"✅ <b>Доступно:</b> {subject}\n{icon} Можно смотреть в Jellyfin")
    elif ntype == "MEDIA_FAILED":
        send_telegram(f"❌ <b>Ошибка:</b> {subject}\n{data.get('message', '')}")
    elif ntype == "MEDIA_DECLINED":
        send_telegram(f"🚫 <b>Отклонено:</b> {subject}")


# ── HTTP Server ──────────────────────────────────────────────────────────

class HubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return
        path = self.path
        handlers = {
            "/radarr": ("Radarr", "eventType", handle_radarr_webhook),
            "/sonarr": ("Sonarr", "eventType", handle_sonarr_webhook),
            "/jellyfin": ("Jellyfin", "NotificationType", handle_jellyfin_webhook),
            "/webhook": ("Jellyfin", "NotificationType", handle_jellyfin_webhook),
            "/prowlarr": ("Prowlarr", "eventType", handle_prowlarr_webhook),
            "/jellyseerr": ("Jellyseerr", "notification_type", handle_jellyseerr_webhook),
        }
        if path in handlers:
            name, key, handler = handlers[path]
            log(f"{name} webhook: {data.get(key, '?')}")
            handler(data)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "service": "media-hub",
            "movies": len(state.movies), "series": len(state.series),
            "queue": len(state.queue_items), "pending_choices": len(state.pending_choices),
        }).encode())

    def log_message(self, format, *args):
        pass


# ── Auto-search for continuing series ────────────────────────────────────

def auto_search():
    """Search for new episodes of continuing series."""
    series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
    if not series:
        return
    continuing = [s for s in series if s.get("status") == "continuing" and s.get("monitored")]
    if not continuing:
        return
    log(f"Auto-search: {len(continuing)} continuing series")
    for s in continuing:
        api_post(f"{SONARR_URL}/api/v3/command", SONARR_KEY, {"name": "SeriesSearch", "seriesId": s["id"]})
        log(f"Auto-search triggered: {s.get('title', '?')}")
        time.sleep(5)  # Don't hammer the API


# ── Telegram bot polling ─────────────────────────────────────────────────

def telegram_poll_loop():
    """Poll Telegram for user replies (torrent selection)."""
    log("Telegram bot polling started")
    offset = 0
    while True:
        try:
            result = tg_api("getUpdates", {"offset": offset, "timeout": 30, "allowed_updates": "message"})
            if not result or not result.get("ok"):
                time.sleep(5)
                continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "")
                if chat_id == TELEGRAM_CHAT_ID and text:
                    handle_telegram_choice(text)
        except Exception as e:
            log(f"TG poll error: {e}")
            time.sleep(10)


# ── Main polling loop ────────────────────────────────────────────────────

def poll_loop():
    log("Poller started")
    time.sleep(30)

    while True:
        try:
            has_active = False
            rq = api_get(f"{RADARR_URL}/api/v3/queue/details", RADARR_KEY)
            sq = api_get(f"{SONARR_URL}/api/v3/queue/details", SONARR_KEY)
            all_queue = []
            if rq and isinstance(rq, list):
                all_queue.extend(rq)
            if sq and isinstance(sq, list):
                all_queue.extend(sq)
            if all_queue:
                has_active = True
                state.update_queue(all_queue)
            else:
                state.update_queue([])

            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies:
                state.update_movies(movies)
            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series:
                state.update_series(series)

            if not state.initialized:
                state.initialized = True
                log(f"State: {len(state.movies)} movies, {len(state.series)} series, {len(state.queue_items)} queue")

            # Auto-search every N hours
            now = time.time()
            if now - state.last_auto_search > AUTO_SEARCH_HOURS * 3600:
                state.last_auto_search = now
                if state.initialized:
                    auto_search()

            if has_active:
                time.sleep(30)
            else:
                time.sleep(120)
        except Exception as e:
            log(f"Poll error: {e}")
            log(traceback.format_exc())
            time.sleep(60)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    log("Media Hub starting...")
    log(f"Radarr: {RADARR_URL} | Sonarr: {SONARR_URL}")
    log(f"Telegram: {'configured' if TELEGRAM_TOKEN else 'NOT configured'}")
    log(f"Auto-search: every {AUTO_SEARCH_HOURS}h for continuing series")

    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=telegram_poll_loop, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), HubHandler)
    log(f"Listening on :{PORT}")
    log("Endpoints: /radarr /sonarr /prowlarr /jellyfin /jellyseerr")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
