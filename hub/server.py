#!/usr/bin/env python3
"""Media Hub — unified notification and cleanup service.

Receives webhooks from Radarr, Sonarr, Jellyseerr, Jellyfin.
Polls Radarr/Sonarr for status changes (not available, search failed, download progress).
Sends formatted Telegram notifications in Russian.
Handles Jellyfin deletion cascading to Radarr/Sonarr.
"""

import json
import os
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

# Config from env
RADARR_URL = os.environ.get("RADARR_URL", "http://gluetun:7878/radarr")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989/sonarr")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
JELLYSEERR_URL = os.environ.get("JELLYSEERR_URL", "http://jellyseerr:5055")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096/jellyfin")
PORT = int(os.environ.get("PORT", "9999"))

# ── Helpers ──────────────────────────────────────────────────────────────

def log(msg):
    print(f"[hub] {msg}", flush=True)


def api_get(url, api_key="", timeout=15):
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"API GET failed {url}: {e}")
        return None


def api_delete(url, api_key):
    req = urllib.request.Request(url, method="DELETE", headers={"X-Api-Key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except Exception as e:
        log(f"API DELETE failed {url}: {e}")
        return None


def send_telegram(text):
    log(f"TG: {text[:100]}")
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log(f"Telegram not configured")
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Telegram send failed: {e}")


import urllib.parse  # noqa: E402 — needed for urlencode


def fmt_size(bytes_val):
    if not bytes_val:
        return "?"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.0f}MB"


# ── State tracker ────────────────────────────────────────────────────────

class StateTracker:
    """Tracks known items and their states to detect changes."""

    def __init__(self):
        self.movies = {}       # id -> {title, status, hasFile, notified_status}
        self.series = {}       # id -> {title, status, hasFile, notified_status}
        self.queue_items = {}  # downloadId -> {title, last_pct}
        self.notified_progress = {}  # downloadId -> set of milestones sent
        self.initialized = False

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

            if prev and not self.initialized:
                pass  # Skip notifications on first load

            elif prev:
                # Detect status changes
                notified = prev.get("notified_status")

                # Movie became available but no file — search likely failed
                if (status == "released" and not has_file
                        and notified != "no_file_released"):
                    send_telegram(f"❌ <b>Не найдено:</b> {title}\nНет подходящих релизов. Radarr продолжает мониторить.")
                    self.movies[mid]["notified_status"] = "no_file_released"
                    continue

                # Movie not released
                if (status in ("announced", "inCinemas")
                        and notified != f"waiting_{status}"):
                    year = m.get("year", "")
                    if status == "announced":
                        send_telegram(f"⏳ <b>Ожидаем выхода:</b> {title} ({year})\nФильм ещё не анонсирован для проката.")
                    else:
                        send_telegram(f"⏳ <b>В кинотеатрах:</b> {title} ({year})\nОжидаем цифровой релиз.")
                    self.movies[mid]["notified_status"] = f"waiting_{status}"
                    continue

                # Movie downloaded
                if has_file and not prev.get("hasFile"):
                    send_telegram(f"✅ <b>Готово:</b> {title}\nДоступно в Jellyfin")
                    self.movies[mid]["notified_status"] = "done"
                    continue

            self.movies[mid] = {
                "title": title,
                "status": status,
                "hasFile": has_file,
                "notified_status": self.movies.get(mid, {}).get("notified_status"),
            }

    def update_series(self, series_list):
        for s in series_list:
            sid = s["id"]
            title = s.get("title", "?")
            monitored = s.get("monitored", False)
            stats = s.get("statistics", {})
            episode_count = stats.get("episodeFileCount", 0)
            total_episodes = stats.get("totalEpisodeCount", 0)

            if not monitored:
                continue

            prev = self.series.get(sid)

            if prev and self.initialized:
                prev_episodes = prev.get("episodeFileCount", 0)
                if episode_count > prev_episodes:
                    new = episode_count - prev_episodes
                    send_telegram(
                        f"✅ <b>Новые серии:</b> {title}\n"
                        f"Скачано {new} серий (всего {episode_count}/{total_episodes}). Доступно в Jellyfin"
                    )

            self.series[sid] = {
                "title": title,
                "status": s.get("status", "?"),
                "episodeFileCount": episode_count,
                "totalEpisodeCount": total_episodes,
                "notified_status": self.series.get(sid, {}).get("notified_status"),
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
            status = item.get("status", "")

            if size > 0:
                pct = int(((size - sizeleft) / size) * 100)
            else:
                pct = 0

            milestones = self.notified_progress.get(dl_id, set())

            # On first init, just record current state without notifying
            if not self.initialized:
                for milestone in [25, 50, 75]:
                    if pct >= milestone:
                        milestones.add(milestone)
                self.notified_progress[dl_id] = milestones
                self.queue_items[dl_id] = {"title": title, "pct": pct, "status": status}
                continue

            # Notify at 25%, 50%, 75%
            for milestone in [25, 50, 75]:
                if pct >= milestone and milestone not in milestones:
                    send_telegram(
                        f"📊 <b>Прогресс:</b> {title}\n"
                        f"{pct}% ({fmt_size(size - sizeleft)}/{fmt_size(size)})"
                    )
                    milestones.add(milestone)
                    break  # Only one milestone per poll cycle

            self.notified_progress[dl_id] = milestones
            self.queue_items[dl_id] = {"title": title, "pct": pct, "status": status}

        # Clean up finished downloads
        for dl_id in list(self.notified_progress.keys()):
            if dl_id not in current_ids:
                del self.notified_progress[dl_id]
        for dl_id in list(self.queue_items.keys()):
            if dl_id not in current_ids:
                del self.queue_items[dl_id]


state = StateTracker()

# ── Webhook handlers ─────────────────────────────────────────────────────

def handle_radarr_webhook(data):
    event = data.get("eventType", "")
    movie = data.get("movie", {})
    title = movie.get("title", "?")
    release = data.get("release", {})

    if event == "Grab":
        quality = release.get("quality", "?")
        size = fmt_size(release.get("size", 0))
        indexer = release.get("indexer", "?")
        send_telegram(
            f"⬇️ <b>Качаем фильм:</b> {title}\n"
            f"{quality} • {size} • {indexer}"
        )
    elif event == "Download":
        is_upgrade = data.get("isUpgrade", False)
        if is_upgrade:
            send_telegram(f"⬆️ <b>Обновлено:</b> {title}\nНовое качество доступно в Jellyfin")
        else:
            send_telegram(f"✅ <b>Фильм готов:</b> {title}\nДоступно в Jellyfin")
    elif event == "MovieAdded":
        send_telegram(f"🎬 <b>Добавлен фильм:</b> {title}")
    elif event == "MovieDelete":
        send_telegram(f"🗑 <b>Удалён фильм:</b> {title}")
    elif event == "Health":
        msg = data.get("message", "?")
        send_telegram(f"⚠️ <b>Radarr:</b> {msg}")


def handle_sonarr_webhook(data):
    event = data.get("eventType", "")
    series = data.get("series", {})
    title = series.get("title", "?")
    episodes = data.get("episodes", [])
    release = data.get("release", {})

    if event == "Grab":
        quality = release.get("quality", "?")
        size = fmt_size(release.get("size", 0))
        indexer = release.get("indexer", "?")
        ep_info = ""
        if episodes:
            nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes[:3]]
            ep_info = f" ({', '.join(nums)})"
        send_telegram(
            f"⬇️ <b>Качаем сериал:</b> {title}{ep_info}\n"
            f"{quality} • {size} • {indexer}"
        )
    elif event == "Download":
        ep_info = ""
        if episodes:
            nums = [f"S{e.get('seasonNumber',0):02d}E{e.get('episodeNumber',0):02d}" for e in episodes[:3]]
            ep_info = f" ({', '.join(nums)})"
        is_upgrade = data.get("isUpgrade", False)
        if is_upgrade:
            send_telegram(f"⬆️ <b>Обновлено:</b> {title}{ep_info}\nНовое качество в Jellyfin")
        else:
            send_telegram(f"✅ <b>Серия готова:</b> {title}{ep_info}\nДоступно в Jellyfin")
    elif event == "SeriesAdd":
        send_telegram(f"📺 <b>Добавлен сериал:</b> {title}")
    elif event == "SeriesDelete":
        send_telegram(f"🗑 <b>Удалён сериал:</b> {title}")
    elif event == "Health":
        msg = data.get("message", "?")
        send_telegram(f"⚠️ <b>Sonarr:</b> {msg}")


def handle_jellyfin_webhook(data):
    """Handle Jellyfin ItemDeleted — cascade to Radarr/Sonarr."""
    notification_type = data.get("NotificationType", "")
    if notification_type != "ItemDeleted":
        return

    item_type = data.get("ItemType", "")
    provider_tmdb = data.get("Provider_tmdb", "")
    provider_tvdb = data.get("Provider_tvdb", "")

    if not provider_tmdb and not provider_tvdb:
        item = data.get("Item", {})
        provider_tmdb = item.get("ProviderIds", {}).get("Tmdb", "")
        provider_tvdb = item.get("ProviderIds", {}).get("Tvdb", "")

    name = data.get("Name", data.get("SeriesName", "unknown"))

    if item_type == "Movie" and provider_tmdb:
        try:
            tmdb_id = int(provider_tmdb)
            log(f"Jellyfin deleted movie: {name} (tmdb={tmdb_id})")
            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies:
                for movie in movies:
                    if movie.get("tmdbId") == tmdb_id:
                        api_delete(
                            f"{RADARR_URL}/api/v3/movie/{movie['id']}?deleteFiles=true",
                            RADARR_KEY,
                        )
                        log(f"Deleted from Radarr: {name}")
                        return
        except ValueError:
            pass

    elif item_type in ("Series", "Season", "Episode") and provider_tvdb:
        try:
            tvdb_id = int(provider_tvdb)
            log(f"Jellyfin deleted series: {name} (tvdb={tvdb_id})")
            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series:
                for s in series:
                    if s.get("tvdbId") == tvdb_id:
                        api_delete(
                            f"{SONARR_URL}/api/v3/series/{s['id']}?deleteFiles=true",
                            SONARR_KEY,
                        )
                        log(f"Deleted from Sonarr: {name}")
                        return
        except ValueError:
            pass


def handle_prowlarr_webhook(data):
    """Handle Prowlarr grab events (manual downloads via Prowlarr search)."""
    event = data.get("eventType", "")
    if event == "Grab":
        title = data.get("release", {}).get("releaseTitle", "?")
        indexer = data.get("release", {}).get("indexer", "?")
        size = fmt_size(data.get("release", {}).get("size", 0))
        send_telegram(f"⬇️ <b>Качаем:</b> {title}\n{size} • {indexer}")


def handle_jellyseerr_webhook(data):
    """Handle Jellyseerr notification webhooks."""
    ntype = data.get("notification_type", "")
    subject = data.get("subject", "")
    message = data.get("message", "")
    media_type = data.get("media_type", "")
    username = data.get("requestedBy_username", "")

    type_icon = "🎬" if media_type == "movie" else "📺"

    if ntype == "MEDIA_PENDING":
        send_telegram(f"📋 <b>Запрос:</b> {subject}\n{type_icon} Ожидает одобрения (от {username})")
    elif ntype in ("MEDIA_APPROVED", "MEDIA_AUTO_APPROVED"):
        send_telegram(f"👍 <b>Одобрено:</b> {subject}\n{type_icon} Отправлено на поиск")
    elif ntype == "MEDIA_AVAILABLE":
        send_telegram(f"✅ <b>Доступно:</b> {subject}\n{type_icon} Можно смотреть в Jellyfin")
    elif ntype == "MEDIA_FAILED":
        send_telegram(f"❌ <b>Ошибка:</b> {subject}\n{message}")
    elif ntype == "MEDIA_DECLINED":
        send_telegram(f"🚫 <b>Отклонено:</b> {subject}")


# ── HTTP Server ──────────────────────────────────────────────────────────

class HubHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log("Invalid JSON")
            return

        path = self.path

        if path == "/radarr":
            log(f"Radarr webhook: {data.get('eventType', '?')}")
            handle_radarr_webhook(data)
        elif path == "/sonarr":
            log(f"Sonarr webhook: {data.get('eventType', '?')}")
            handle_sonarr_webhook(data)
        elif path == "/jellyfin" or path == "/webhook":
            log(f"Jellyfin webhook: {data.get('NotificationType', '?')}")
            handle_jellyfin_webhook(data)
        elif path == "/prowlarr":
            log(f"Prowlarr webhook: {data.get('eventType', '?')}")
            handle_prowlarr_webhook(data)
        elif path == "/jellyseerr":
            log(f"Jellyseerr webhook: {data.get('notification_type', '?')}")
            handle_jellyseerr_webhook(data)
        else:
            log(f"Unknown webhook path: {path}")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        status = {
            "service": "media-hub",
            "movies": len(state.movies),
            "series": len(state.series),
            "queue": len(state.queue_items),
        }
        self.wfile.write(json.dumps(status).encode())

    def log_message(self, format, *args):
        pass


# ── Polling loop ─────────────────────────────────────────────────────────

def poll_loop():
    """Background polling with adaptive intervals."""
    log("Poller started")

    # Wait for services to be ready
    time.sleep(30)

    while True:
        try:
            # Check queue first to determine poll speed
            has_active = False

            # Radarr queue
            rq = api_get(f"{RADARR_URL}/api/v3/queue/details", RADARR_KEY)
            # Sonarr queue
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

            # Poll movie/series status (less frequently)
            movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
            if movies:
                state.update_movies(movies)

            series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
            if series:
                state.update_series(series)

            if not state.initialized:
                state.initialized = True
                log(f"State initialized: {len(state.movies)} movies, {len(state.series)} series, {len(state.queue_items)} queue")

            # Adaptive sleep
            if has_active:
                log(f"Poll: {len(all_queue)} queue items, sleeping 30s")
                time.sleep(30)
            else:
                log(f"Poll: idle, sleeping 120s")
                time.sleep(120)

        except Exception as e:
            import traceback
            log(f"Poll error: {e}")
            log(traceback.format_exc())
            time.sleep(60)


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    log("Media Hub starting...")
    log(f"Radarr: {RADARR_URL}")
    log(f"Sonarr: {SONARR_URL}")
    log(f"Jellyseerr: {JELLYSEERR_URL}")
    log(f"Telegram: {'configured' if TELEGRAM_TOKEN else 'NOT configured'}")

    # Start polling thread
    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", PORT), HubHandler)
    log(f"Listening on port {PORT}")
    log("Webhook endpoints: /radarr /sonarr /prowlarr /jellyfin /jellyseerr")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
