#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# ============================================
# Wait for services
# ============================================
wait_for_service() {
  local name=$1
  local url=$2
  local max_attempts=${3:-60}
  local attempt=0

  log "Waiting for $name..."
  while [ $attempt -lt $max_attempts ]; do
    if curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null | grep -qE '200|302|401'; then
      log "$name is ready!"
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  error "$name failed to start after $max_attempts attempts"
  return 1
}

# ============================================
# Extract API keys from config.xml files
# ============================================
get_api_key() {
  local config_file=$1
  grep -oP '<ApiKey>\K[^<]+' "$config_file" 2>/dev/null
}

# ============================================
# Wait for all services
# ============================================
log "Starting media server auto-configuration..."

# Services on default network use container names
# Services behind gluetun (radarr, prowlarr) use 127.0.0.1 (host network)
wait_for_service "qBittorrent" "http://127.0.0.1:8080"
wait_for_service "Sonarr" "http://127.0.0.1:8989"
wait_for_service "Radarr" "http://127.0.0.1:7878"
wait_for_service "Prowlarr" "http://127.0.0.1:9696"
wait_for_service "Bazarr" "http://127.0.0.1:6767"
wait_for_service "Jellyfin" "http://127.0.0.1:8096"

# Get API keys
SONARR_KEY=$(get_api_key /config/sonarr/config.xml)
RADARR_KEY=$(get_api_key /config/radarr/config.xml)
PROWLARR_KEY=$(get_api_key /config/prowlarr/config.xml)
BAZARR_KEY=$(grep 'apikey:' /config/bazarr/config/config.yaml 2>/dev/null | head -1 | awk '{print $2}')

log "API Keys found:"
log "  Sonarr:   ${SONARR_KEY:0:8}..."
log "  Radarr:   ${RADARR_KEY:0:8}..."
log "  Prowlarr: ${PROWLARR_KEY:0:8}..."
log "  Bazarr:   ${BAZARR_KEY:0:8}..."

# ============================================
# 1. Configure qBittorrent
# ============================================
log "Configuring qBittorrent..."

# Get temp password from logs or use default
QB_TEMP_PASS="${QB_PASSWORD:-adminadmin}"

# Login
QB_COOKIE=$(curl -s -c - "http://127.0.0.1:8080/api/v2/auth/login" \
  --data-urlencode "username=admin" \
  --data-urlencode "password=$QB_TEMP_PASS" 2>/dev/null | grep SID | awk '{print $NF}')

if [ -z "$QB_COOKIE" ]; then
  warn "qBittorrent login failed with default password, trying temp password from logs..."
  # Try to read from the container logs - skip if can't login
  warn "Skipping qBittorrent config - login manually first time"
else
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/app/setPreferences" \
    --data-urlencode "json={
      \"save_path\": \"/downloads/complete/\",
      \"temp_path_enabled\": true,
      \"temp_path\": \"/downloads/incomplete/\",
      \"listen_port\": 6881,
      \"max_active_downloads\": 5,
      \"max_active_torrents\": 10,
      \"max_active_uploads\": 5
    }"

  # Create categories
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/torrents/createCategory" \
    --data-urlencode "category=tv" --data-urlencode "savePath=/downloads/complete/"
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/torrents/createCategory" \
    --data-urlencode "category=movies" --data-urlencode "savePath=/downloads/complete/"

  log "qBittorrent configured!"
fi

# ============================================
# 2. Configure Sonarr
# ============================================
log "Configuring Sonarr..."

# Check if download client already exists
EXISTING_DC=$(curl -s "http://127.0.0.1:8989/sonarr/api/v3/downloadclient" \
  -H "X-Api-Key: $SONARR_KEY" | grep -c "qBittorrent" 2>/dev/null || echo "0")

if [ "$EXISTING_DC" = "0" ]; then
  curl -s -X POST "http://127.0.0.1:8989/sonarr/api/v3/downloadclient" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $SONARR_KEY" \
    -d '{
      "enable": true,
      "protocol": "torrent",
      "priority": 1,
      "removeCompletedDownloads": true,
      "removeFailedDownloads": true,
      "name": "qBittorrent",
      "implementation": "QBittorrent",
      "configContract": "QBittorrentSettings",
      "fields": [
        {"name": "host", "value": "qbittorrent"},
        {"name": "port", "value": 8080},
        {"name": "useSsl", "value": false},
        {"name": "urlBase", "value": ""},
        {"name": "username", "value": "admin"},
        {"name": "password", "value": "'"${QB_PASSWORD:-adminadmin}"'"},
        {"name": "tvCategory", "value": "tv"},
        {"name": "tvImportedCategory", "value": ""},
        {"name": "recentTvPriority", "value": 0},
        {"name": "olderTvPriority", "value": 0},
        {"name": "initialState", "value": 0},
        {"name": "sequentialOrder", "value": false},
        {"name": "firstAndLast", "value": false}
      ],
      "tags": []
    }' > /dev/null
  log "  Added qBittorrent download client"
else
  log "  qBittorrent download client already exists"
fi

# Add root folder
EXISTING_RF=$(curl -s "http://127.0.0.1:8989/sonarr/api/v3/rootfolder" \
  -H "X-Api-Key: $SONARR_KEY" | grep -c "/tv" 2>/dev/null || echo "0")

if [ "$EXISTING_RF" = "0" ]; then
  curl -s -X POST "http://127.0.0.1:8989/sonarr/api/v3/rootfolder" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $SONARR_KEY" \
    -d '{"path": "/tv"}' > /dev/null
  log "  Added root folder /tv"
fi

log "Sonarr configured!"

# ============================================
# 3. Configure Radarr
# ============================================
log "Configuring Radarr..."

EXISTING_DC=$(curl -s "http://127.0.0.1:7878/radarr/api/v3/downloadclient" \
  -H "X-Api-Key: $RADARR_KEY" | grep -c "qBittorrent" 2>/dev/null || echo "0")

if [ "$EXISTING_DC" = "0" ]; then
  curl -s -X POST "http://127.0.0.1:7878/radarr/api/v3/downloadclient" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $RADARR_KEY" \
    -d '{
      "enable": true,
      "protocol": "torrent",
      "priority": 1,
      "removeCompletedDownloads": true,
      "removeFailedDownloads": true,
      "name": "qBittorrent",
      "implementation": "QBittorrent",
      "configContract": "QBittorrentSettings",
      "fields": [
        {"name": "host", "value": "qbittorrent"},
        {"name": "port", "value": 8080},
        {"name": "useSsl", "value": false},
        {"name": "urlBase", "value": ""},
        {"name": "username", "value": "admin"},
        {"name": "password", "value": "'"${QB_PASSWORD:-adminadmin}"'"},
        {"name": "movieCategory", "value": "movies"},
        {"name": "movieImportedCategory", "value": ""},
        {"name": "recentMoviePriority", "value": 0},
        {"name": "olderMoviePriority", "value": 0},
        {"name": "initialState", "value": 0},
        {"name": "sequentialOrder", "value": false},
        {"name": "firstAndLast", "value": false}
      ],
      "tags": []
    }' > /dev/null
  log "  Added qBittorrent download client"
fi

EXISTING_RF=$(curl -s "http://127.0.0.1:7878/radarr/api/v3/rootfolder" \
  -H "X-Api-Key: $RADARR_KEY" | grep -c "/movies" 2>/dev/null || echo "0")

if [ "$EXISTING_RF" = "0" ]; then
  curl -s -X POST "http://127.0.0.1:7878/radarr/api/v3/rootfolder" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $RADARR_KEY" \
    -d '{"path": "/movies"}' > /dev/null
  log "  Added root folder /movies"
fi

log "Radarr configured!"

# ============================================
# 4. Configure Prowlarr - Indexers
# ============================================
log "Configuring Prowlarr indexers..."

# Add Nyaa.si
EXISTING_IDX=$(curl -s "http://127.0.0.1:9696/prowlarr/api/v1/indexer" \
  -H "X-Api-Key: $PROWLARR_KEY" | grep -c "Nyaa" 2>/dev/null || echo "0")

if [ "$EXISTING_IDX" = "0" ]; then
  curl -s -X POST "http://127.0.0.1:9696/prowlarr/api/v1/indexer" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $PROWLARR_KEY" \
    -d '{
      "name": "Nyaa.si",
      "definitionName": "nyaasi",
      "implementation": "Cardigann",
      "configContract": "CardigannSettings",
      "enable": true,
      "redirect": false,
      "supportsRss": true,
      "supportsSearch": true,
      "appProfileId": 1,
      "protocol": "torrent",
      "privacy": "public",
      "priority": 25,
      "fields": [
        {"name": "definitionFile", "value": "nyaasi"},
        {"name": "baseUrl", "value": "https://nyaa.si/"},
        {"name": "torrentBaseSettings.appMinimumSeeders", "value": ""},
        {"name": "torrentBaseSettings.seedRatio", "value": ""},
        {"name": "torrentBaseSettings.seedTime", "value": ""}
      ],
      "tags": []
    }' > /dev/null
  log "  Added Nyaa.si indexer"
fi

# Add RuTracker (if credentials provided)
if [ -n "$RUTRACKER_USERNAME" ] && [ -n "$RUTRACKER_PASSWORD" ]; then
  EXISTING_RT=$(curl -s "http://127.0.0.1:9696/prowlarr/api/v1/indexer" \
    -H "X-Api-Key: $PROWLARR_KEY" | grep -c "RuTracker" 2>/dev/null || echo "0")

  if [ "$EXISTING_RT" = "0" ]; then
    curl -s -X POST "http://127.0.0.1:9696/prowlarr/api/v1/indexer" \
      -H "Content-Type: application/json" \
      -H "X-Api-Key: $PROWLARR_KEY" \
      -d '{
        "name": "RuTracker",
        "definitionName": "rutracker",
        "implementation": "Cardigann",
        "configContract": "CardigannSettings",
        "enable": true,
        "redirect": false,
        "supportsRss": true,
        "supportsSearch": true,
        "appProfileId": 1,
        "protocol": "torrent",
        "privacy": "private",
        "priority": 25,
        "fields": [
          {"name": "definitionFile", "value": "rutracker"},
          {"name": "baseUrl", "value": "https://rutracker.org/"},
          {"name": "username", "value": "'"$RUTRACKER_USERNAME"'"},
          {"name": "password", "value": "'"$RUTRACKER_PASSWORD"'"},
          {"name": "torrentBaseSettings.appMinimumSeeders", "value": ""},
          {"name": "torrentBaseSettings.seedRatio", "value": ""},
          {"name": "torrentBaseSettings.seedTime", "value": ""}
        ],
        "tags": []
      }' > /dev/null
    log "  Added RuTracker indexer"
  fi
else
  warn "  RuTracker skipped - RUTRACKER_USERNAME/PASSWORD not set in .env"
fi

# ============================================
# 5. Configure Prowlarr - App Sync
# ============================================
log "Configuring Prowlarr app sync..."

# Prowlarr and Radarr share gluetun network, so they can reach each other via localhost
# Sonarr is on default network, reachable by container name

EXISTING_APPS=$(curl -s "http://127.0.0.1:9696/prowlarr/api/v1/applications" \
  -H "X-Api-Key: $PROWLARR_KEY")

# Add Sonarr sync
if echo "$EXISTING_APPS" | grep -qv "Sonarr" 2>/dev/null; then
  curl -s -X POST "http://127.0.0.1:9696/prowlarr/api/v1/applications" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $PROWLARR_KEY" \
    -d '{
      "name": "Sonarr",
      "syncLevel": "fullSync",
      "implementation": "Sonarr",
      "configContract": "SonarrSettings",
      "fields": [
        {"name": "baseUrl", "value": "http://127.0.0.1:8989/sonarr"},
        {"name": "prowlarrUrl", "value": "http://localhost:9696/prowlarr"},
        {"name": "apiKey", "value": "'"$SONARR_KEY"'"},
        {"name": "syncCategories", "value": [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5070]},
        {"name": "animeSyncCategories", "value": [5070]}
      ],
      "tags": []
    }' > /dev/null
  log "  Added Sonarr app sync"
fi

# Add Radarr sync (Radarr is on same gluetun network as Prowlarr)
if echo "$EXISTING_APPS" | grep -qv "Radarr" 2>/dev/null; then
  curl -s -X POST "http://127.0.0.1:9696/prowlarr/api/v1/applications" \
    -H "Content-Type: application/json" \
    -H "X-Api-Key: $PROWLARR_KEY" \
    -d '{
      "name": "Radarr",
      "syncLevel": "fullSync",
      "implementation": "Radarr",
      "configContract": "RadarrSettings",
      "fields": [
        {"name": "baseUrl", "value": "http://localhost:7878/radarr"},
        {"name": "prowlarrUrl", "value": "http://localhost:9696/prowlarr"},
        {"name": "apiKey", "value": "'"$RADARR_KEY"'"},
        {"name": "syncCategories", "value": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080]}
      ],
      "tags": []
    }' > /dev/null
  log "  Added Radarr app sync"
fi

# Trigger sync
curl -s -X POST "http://127.0.0.1:9696/prowlarr/api/v1/indexer/action/syncAll" \
  -H "X-Api-Key: $PROWLARR_KEY" > /dev/null 2>&1 || true

log "Prowlarr configured!"

# ============================================
# 6. Configure Bazarr
# ============================================
log "Configuring Bazarr..."

# Bazarr is best configured via config.yaml
BAZARR_CONFIG="/config/bazarr/config/config.yaml"

if [ -f "$BAZARR_CONFIG" ]; then
  # Set Sonarr connection
  sed -i "s|ip: 127.0.0.1|ip: sonarr|" "$BAZARR_CONFIG" 2>/dev/null || true

  # Use python for reliable YAML editing
  python3 -c "
import yaml, sys

config_path = '$BAZARR_CONFIG'
try:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f) or {}
except:
    config = {}

# Sonarr settings
if 'sonarr' not in config:
    config['sonarr'] = {}
config['sonarr']['ip'] = 'sonarr'
config['sonarr']['port'] = '8989'
config['sonarr']['base_url'] = '/sonarr'
config['sonarr']['apikey'] = '$SONARR_KEY'
config['sonarr']['full_update'] = 'Daily'
config['sonarr']['only_monitored'] = 'True'

# Radarr settings (behind gluetun)
if 'radarr' not in config:
    config['radarr'] = {}
config['radarr']['ip'] = 'gluetun'
config['radarr']['port'] = '7878'
config['radarr']['base_url'] = '/radarr'
config['radarr']['apikey'] = '$RADARR_KEY'
config['radarr']['full_update'] = 'Daily'
config['radarr']['only_monitored'] = 'True'

# General settings
if 'general' not in config:
    config['general'] = {}
config['general']['use_sonarr'] = 'True'
config['general']['use_radarr'] = 'True'
config['general']['base_url'] = '/bazarr'

# Languages
if 'languages' not in config:
    config['languages'] = {}
config['languages']['enabled'] = 'True'

with open(config_path, 'w') as f:
    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

print('Bazarr config updated')
" 2>/dev/null || warn "  Could not update Bazarr config (python3/pyyaml not available)"

  log "Bazarr configured!"
else
  warn "  Bazarr config not found at $BAZARR_CONFIG - will configure on next run"
fi

# ============================================
# 7. Configure Jellyfin
# ============================================
log "Configuring Jellyfin..."

# Check if Jellyfin is already configured (has users)
JF_STATUS=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8096/jellyfin/Startup/Configuration" 2>/dev/null)

if [ "$JF_STATUS" = "200" ]; then
  # Step 1: Set startup configuration
  curl -s -X POST "http://127.0.0.1:8096/jellyfin/Startup/Configuration" \
    -H "Content-Type: application/json" \
    -d '{
      "UICulture": "ru-RU",
      "MetadataCountryCode": "RU",
      "PreferredMetadataLanguage": "ru"
    }' > /dev/null

  # Step 2: Create admin user
  curl -s -X POST "http://127.0.0.1:8096/jellyfin/Startup/User" \
    -H "Content-Type: application/json" \
    -d "{
      \"Name\": \"${JELLYFIN_USER:-admin}\",
      \"Password\": \"${JELLYFIN_PASSWORD:-admin}\"
    }" > /dev/null

  # Step 3: Complete wizard
  curl -s -X POST "http://127.0.0.1:8096/jellyfin/Startup/Complete" \
    -H "Content-Type: application/json" > /dev/null

  # Step 4: Authenticate
  JF_AUTH=$(curl -s -X POST "http://127.0.0.1:8096/jellyfin/Users/AuthenticateByName" \
    -H "Content-Type: application/json" \
    -H 'Authorization: MediaBrowser Client="SetupScript", Device="Init", DeviceId="media-server-init", Version="1.0.0"' \
    -d "{
      \"Username\": \"${JELLYFIN_USER:-admin}\",
      \"Pw\": \"${JELLYFIN_PASSWORD:-admin}\"
    }" 2>/dev/null)

  JF_TOKEN=$(echo "$JF_AUTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('AccessToken',''))" 2>/dev/null)

  if [ -n "$JF_TOKEN" ]; then
    JF_HEADER="Authorization: MediaBrowser Client=\"SetupScript\", Device=\"Init\", DeviceId=\"media-server-init\", Version=\"1.0.0\", Token=\"$JF_TOKEN\""

    # Add Movies library
    curl -s -X POST "http://127.0.0.1:8096/jellyfin/Library/VirtualFolders?name=Movies&collectionType=movies&refreshLibrary=false" \
      -H "Content-Type: application/json" \
      -H "$JF_HEADER" \
      -d '{
        "LibraryOptions": {
          "PathInfos": [{"Path": "/movies"}],
          "EnableRealtimeMonitor": true,
          "PreferredMetadataLanguage": "ru",
          "MetadataCountryCode": "RU"
        }
      }' > /dev/null 2>&1

    # Add TV library
    curl -s -X POST "http://127.0.0.1:8096/jellyfin/Library/VirtualFolders?name=TV%20Shows&collectionType=tvshows&refreshLibrary=false" \
      -H "Content-Type: application/json" \
      -H "$JF_HEADER" \
      -d '{
        "LibraryOptions": {
          "PathInfos": [{"Path": "/tv"}],
          "EnableRealtimeMonitor": true,
          "PreferredMetadataLanguage": "ru",
          "MetadataCountryCode": "RU",
          "EnableAutomaticSeriesGrouping": true
        }
      }' > /dev/null 2>&1

    log "  Jellyfin configured with Movies and TV libraries!"
  else
    warn "  Jellyfin auth failed - configure manually"
  fi
else
  log "  Jellyfin already configured (startup wizard completed)"
fi

# ============================================
# 8. Set URL Bases via config.xml (for fresh installs)
# ============================================
log "Setting URL Bases..."

for service_config in \
  "/config/sonarr/config.xml:sonarr" \
  "/config/radarr/config.xml:radarr" \
  "/config/prowlarr/config.xml:prowlarr"; do

  config_file="${service_config%%:*}"
  url_base="${service_config##*:}"

  if [ -f "$config_file" ]; then
    if grep -q "<UrlBase/>" "$config_file" || grep -q "<UrlBase></UrlBase>" "$config_file"; then
      sed -i "s|<UrlBase/>|<UrlBase>/$url_base</UrlBase>|" "$config_file"
      sed -i "s|<UrlBase></UrlBase>|<UrlBase>/$url_base</UrlBase>|" "$config_file"
      log "  Set URL Base /$url_base in $config_file"
    fi
  fi
done

# Jellyfin network.xml
JF_NETWORK="/config/jellyfin/config/network.xml"
if [ ! -f "$JF_NETWORK" ]; then
  mkdir -p /config/jellyfin/config
  cat > "$JF_NETWORK" << 'XML'
<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <BaseUrl>/jellyfin</BaseUrl>
</NetworkConfiguration>
XML
  log "  Set Jellyfin Base URL /jellyfin"
fi

# ============================================
# Done!
# ============================================
echo ""
log "============================================"
log "  Media Server auto-configuration complete!"
log "============================================"
log ""
log "Services configured:"
log "  - qBittorrent: download paths, categories"
log "  - Sonarr: qBittorrent client, /tv root folder"
log "  - Radarr: qBittorrent client, /movies root folder"
log "  - Prowlarr: Nyaa.si + RuTracker indexers, Sonarr/Radarr sync"
log "  - Bazarr: Sonarr/Radarr connections, languages"
log "  - Jellyfin: admin user, Movies/TV libraries"
log ""
log "NOTE: Restart Sonarr, Radarr, Prowlarr after first run"
log "      to apply URL Base changes:"
log "      docker compose restart sonarr radarr prowlarr jellyfin bazarr"
log ""
