#!/usr/bin/env python3
"""Media Cleanup Webhook Service.

Receives Jellyfin webhook events (ItemDeleted) and cascades deletions
to Radarr/Sonarr so they don't re-download removed content.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

RADARR_URL = os.environ.get("RADARR_URL", "http://gluetun:7878/radarr")
RADARR_KEY = os.environ.get("RADARR_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr:8989/sonarr")
SONARR_KEY = os.environ.get("SONARR_KEY", "")
PORT = int(os.environ.get("PORT", "9999"))


def log(msg):
    print(f"[cleanup] {msg}", flush=True)


def api_get(url, api_key):
    req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def api_delete(url, api_key):
    req = urllib.request.Request(url, method="DELETE", headers={"X-Api-Key": api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status


def delete_from_radarr(tmdb_id):
    """Find movie by TMDB ID in Radarr and delete it."""
    if not RADARR_KEY:
        log("RADARR_KEY not set, skipping")
        return False
    try:
        movies = api_get(f"{RADARR_URL}/api/v3/movie", RADARR_KEY)
        for movie in movies:
            if movie.get("tmdbId") == tmdb_id:
                movie_id = movie["id"]
                title = movie.get("title", "?")
                api_delete(
                    f"{RADARR_URL}/api/v3/movie/{movie_id}?deleteFiles=true",
                    RADARR_KEY,
                )
                log(f"Deleted from Radarr: {title} (tmdb={tmdb_id})")
                return True
        log(f"Movie not found in Radarr (tmdb={tmdb_id})")
        return False
    except Exception as e:
        log(f"Radarr error: {e}")
        return False


def delete_from_sonarr(tvdb_id):
    """Find series by TVDB ID in Sonarr and delete it."""
    if not SONARR_KEY:
        log("SONARR_KEY not set, skipping")
        return False
    try:
        series = api_get(f"{SONARR_URL}/api/v3/series", SONARR_KEY)
        for s in series:
            if s.get("tvdbId") == tvdb_id:
                series_id = s["id"]
                title = s.get("title", "?")
                api_delete(
                    f"{SONARR_URL}/api/v3/series/{series_id}?deleteFiles=true",
                    SONARR_KEY,
                )
                log(f"Deleted from Sonarr: {title} (tvdb={tvdb_id})")
                return True
        log(f"Series not found in Sonarr (tvdb={tvdb_id})")
        return False
    except Exception as e:
        log(f"Sonarr error: {e}")
        return False


class WebhookHandler(BaseHTTPRequestHandler):
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
            log(f"Invalid JSON received")
            return

        notification_type = data.get("NotificationType", "")
        item_type = data.get("ItemType", "")

        log(f"Received: {notification_type} | {item_type}")

        if notification_type != "ItemDeleted":
            return

        provider_ids = data.get("Provider_tmdb", "")
        provider_tvdb = data.get("Provider_tvdb", "")

        # Also check nested structure (different webhook plugin versions)
        if not provider_ids and not provider_tvdb:
            item = data.get("Item", {})
            provider_ids = item.get("ProviderIds", {}).get("Tmdb", "")
            provider_tvdb = item.get("ProviderIds", {}).get("Tvdb", "")

        name = data.get("Name", data.get("SeriesName", "unknown"))

        if item_type == "Movie" and provider_ids:
            try:
                tmdb_id = int(provider_ids)
                log(f"Movie deleted from Jellyfin: {name} (tmdb={tmdb_id})")
                delete_from_radarr(tmdb_id)
            except ValueError:
                log(f"Invalid TMDB ID: {provider_ids}")

        elif item_type in ("Series", "Season", "Episode") and provider_tvdb:
            try:
                tvdb_id = int(provider_tvdb)
                log(f"Series deleted from Jellyfin: {name} (tvdb={tvdb_id})")
                delete_from_sonarr(tvdb_id)
            except ValueError:
                log(f"Invalid TVDB ID: {provider_tvdb}")
        else:
            log(f"Ignoring: {item_type} (no provider IDs or not movie/series)")

    def log_message(self, format, *args):
        pass  # Suppress default access logs


def main():
    if not RADARR_KEY and not SONARR_KEY:
        log("WARNING: Neither RADARR_KEY nor SONARR_KEY is set")

    server = HTTPServer(("0.0.0.0", PORT), WebhookHandler)
    log(f"Listening on port {PORT}")
    log(f"Radarr: {RADARR_URL}")
    log(f"Sonarr: {SONARR_URL}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
