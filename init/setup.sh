#!/bin/bash
set -uo pipefail
# NOT using set -e — we handle errors per-step, script must continue

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[SETUP]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error(){ echo -e "${RED}[ERROR]${NC} $1"; }
step() { echo -e "\n${BLUE}━━━ $1 ━━━${NC}"; }

# Global API response vars
API_BODY=""
API_STATUS=""

# ============================================
# Helper: API call with retry
# Usage: api_call METHOD URL [DATA] [EXTRA_HEADER]
# Sets: API_BODY, API_STATUS
# Returns: 0 on success (2xx), 1 on failure
# ============================================
api_call() {
  local method=$1 url=$2 data="${3:-}" extra_header="${4:-}"
  local attempt=0 max=10

  while [ $attempt -lt $max ]; do
    local curl_args=(-s -w "\n%{http_code}" -X "$method" "$url")
    [ -n "$data" ] && curl_args+=(-H "Content-Type: application/json" -d "$data")
    [ -n "$extra_header" ] && curl_args+=(-H "$extra_header")

    local response
    response=$(curl "${curl_args[@]}" 2>/dev/null) || true
    API_STATUS=$(echo "$response" | tail -1)
    API_BODY=$(echo "$response" | sed '$d')

    case "$API_STATUS" in
      200|201|204|302|307) return 0 ;;
    esac

    attempt=$((attempt + 1))
    [ $attempt -lt $max ] && sleep 5
  done
  return 1
}

# ============================================
# Helper: API call with cookie (for Jellyseerr)
# Usage: api_call_cookie METHOD URL DATA COOKIE_FILE
# ============================================
api_call_cookie() {
  local method=$1 url=$2 data="${3:-}" cookie_file="${4:-/tmp/jellyseerr-cookies.txt}"
  local attempt=0 max=10

  while [ $attempt -lt $max ]; do
    local curl_args=(-s -w "\n%{http_code}" -X "$method" "$url" -b "$cookie_file" -c "$cookie_file")
    [ -n "$data" ] && curl_args+=(-H "Content-Type: application/json" -d "$data")

    local response
    response=$(curl "${curl_args[@]}" 2>/dev/null) || true
    API_STATUS=$(echo "$response" | tail -1)
    API_BODY=$(echo "$response" | sed '$d')

    case "$API_STATUS" in
      200|201|204|302|307) return 0 ;;
    esac

    attempt=$((attempt + 1))
    [ $attempt -lt $max ] && sleep 5
  done
  return 1
}

# ============================================
# Helper: API call with form data (for qBittorrent)
# Usage: api_call_form METHOD URL FORM_DATA COOKIE_VALUE
# ============================================
api_call_form() {
  local method=$1 url=$2 form_data="$3" cookie="${4:-}"
  local attempt=0 max=10

  while [ $attempt -lt $max ]; do
    local response
    response=$(curl -s -w "\n%{http_code}" -X "$method" "$url" \
      ${cookie:+-b "SID=$cookie"} \
      --data-urlencode "$form_data" 2>/dev/null) || true
    API_STATUS=$(echo "$response" | tail -1)
    API_BODY=$(echo "$response" | sed '$d')

    case "$API_STATUS" in
      200|201|204) return 0 ;;
    esac

    attempt=$((attempt + 1))
    [ $attempt -lt $max ] && sleep 5
  done
  return 1
}

# ============================================
# Helper: Verify something was created
# ============================================
verify_exists() {
  local url=$1 search=$2 header="${3:-}" label="${4:-item}"
  if api_call GET "$url" "" "$header"; then
    if echo "$API_BODY" | grep -q "$search"; then
      log "  ✓ Verified: $label exists"
      return 0
    fi
  fi
  error "  ✗ Verification failed: $label not found"
  return 1
}

# ============================================
# Helper: Wait for service
# ============================================
wait_for_service() {
  local name=$1 url=$2 max=${3:-60} attempt=0
  log "Waiting for $name..."
  while [ $attempt -lt $max ]; do
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null) || true
    case "$code" in
      200|302|401|307) log "$name is ready!"; return 0 ;;
    esac
    attempt=$((attempt + 1))
    sleep 2
  done
  error "$name failed to start after $((max * 2))s"
  return 1
}

# ============================================
# Helper: Get API key from config.xml
# ============================================
get_api_key() {
  grep -oP '<ApiKey>\K[^<]+' "$1" 2>/dev/null || echo ""
}

# ============================================
# START
# ============================================
log "Starting media server auto-configuration..."
echo ""

# ============================================
# Phase 1: Wait for all services
# ============================================
step "1/10: Waiting for services..."

wait_for_service "qBittorrent" "http://127.0.0.1:8080"
wait_for_service "Sonarr" "http://127.0.0.1:8989"
wait_for_service "Radarr" "http://127.0.0.1:7878"
wait_for_service "Prowlarr" "http://127.0.0.1:9696"
wait_for_service "Bazarr" "http://127.0.0.1:6767"
wait_for_service "Jellyfin" "http://127.0.0.1:8096"
wait_for_service "Jellyseerr" "http://127.0.0.1:5055"

echo ""

# ============================================
# Phase 2: Set URL Bases + Auth in config.xml
# Done AFTER services started (so config.xml exists)
# ============================================
step "2/10: Setting URL Bases and Auth..."

for pair in "sonarr:sonarr" "radarr:radarr" "prowlarr:prowlarr"; do
  svc="${pair%%:*}"
  base="${pair##*:}"
  cfg="/config/$svc/config.xml"

  if [ -f "$cfg" ]; then
    sed -i "s|<UrlBase/>|<UrlBase>/$base</UrlBase>|" "$cfg" 2>/dev/null || true
    sed -i "s|<UrlBase></UrlBase>|<UrlBase>/$base</UrlBase>|" "$cfg" 2>/dev/null || true

    sed -i 's|<AuthenticationMethod>None</AuthenticationMethod>|<AuthenticationMethod>Forms</AuthenticationMethod>|' "$cfg" 2>/dev/null || true
    sed -i 's|<AuthenticationRequired>Enabled</AuthenticationRequired>|<AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>|' "$cfg" 2>/dev/null || true
    if ! grep -q "AuthenticationMethod" "$cfg" 2>/dev/null; then
      sed -i 's|</Config>|  <AuthenticationMethod>Forms</AuthenticationMethod>\n  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>\n</Config>|' "$cfg" 2>/dev/null || true
    fi

    log "  ✓ $svc: URL Base /$base, auth disabled"
  else
    warn "  $svc: config.xml not found"
  fi
done

echo ""

# ============================================
# Phase 3: Get API keys
# ============================================
step "3/10: Reading API keys..."

SONARR_KEY=$(get_api_key /config/sonarr/config.xml)
RADARR_KEY=$(get_api_key /config/radarr/config.xml)
PROWLARR_KEY=$(get_api_key /config/prowlarr/config.xml)

log "  Sonarr:   ${SONARR_KEY:0:8}..."
log "  Radarr:   ${RADARR_KEY:0:8}..."
log "  Prowlarr: ${PROWLARR_KEY:0:8}..."

echo ""

# ============================================
# Phase 4: Configure Jellyfin
# ============================================
step "4/10: Configuring Jellyfin..."

JF_BASE="http://127.0.0.1:8096"
JF_AUTH_HEADER='Authorization: MediaBrowser Client="Setup", Device="Init", DeviceId="media-init", Version="1.0"'

# Check if startup wizard is available
if api_call GET "$JF_BASE/Startup/Configuration"; then
  # MUST call GET /Startup/User first (triggers InitializeAsync in 10.10+)
  log "  Initializing Jellyfin user (GET /Startup/User)..."
  api_call GET "$JF_BASE/Startup/User" || true

  # Set username and password
  log "  Setting admin credentials..."
  api_call POST "$JF_BASE/Startup/User" \
    "{\"Name\":\"${JELLYFIN_USER:-admin}\",\"Password\":\"${JELLYFIN_PASSWORD:-admin}\"}" || true

  # Set configuration
  api_call POST "$JF_BASE/Startup/Configuration" \
    '{"UICulture":"ru-RU","MetadataCountryCode":"RU","PreferredMetadataLanguage":"ru"}' || true

  # Enable remote access
  api_call POST "$JF_BASE/Startup/RemoteAccess" \
    '{"EnableRemoteAccess":true,"EnableAutomaticPortMapping":false}' || true

  # Complete wizard
  api_call POST "$JF_BASE/Startup/Complete" || true
  log "  Startup wizard completed"

  # Authenticate to get token
  log "  Authenticating..."
  if api_call POST "$JF_BASE/Users/AuthenticateByName" \
    "{\"Username\":\"${JELLYFIN_USER:-admin}\",\"Pw\":\"${JELLYFIN_PASSWORD:-admin}\"}" \
    "$JF_AUTH_HEADER"; then

    JF_TOKEN=$(echo "$API_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('AccessToken',''))" 2>/dev/null) || true

    if [ -n "$JF_TOKEN" ]; then
      log "  Auth successful, adding libraries..."
      JF_TOKEN_HEADER="Authorization: MediaBrowser Client=\"Setup\", Device=\"Init\", DeviceId=\"media-init\", Version=\"1.0\", Token=\"$JF_TOKEN\""

      # Add Movies library
      if api_call POST "$JF_BASE/Library/VirtualFolders?name=Movies&collectionType=movies&refreshLibrary=false" \
        '{"LibraryOptions":{"PathInfos":[{"Path":"/movies"}],"EnableRealtimeMonitor":true}}' \
        "$JF_TOKEN_HEADER"; then
        log "  ✓ Movies library added"
      else
        warn "  Movies library may already exist"
      fi

      # Add TV library
      if api_call POST "$JF_BASE/Library/VirtualFolders?name=TV%20Shows&collectionType=tvshows&refreshLibrary=false" \
        '{"LibraryOptions":{"PathInfos":[{"Path":"/tv"}],"EnableRealtimeMonitor":true,"EnableAutomaticSeriesGrouping":true}}' \
        "$JF_TOKEN_HEADER"; then
        log "  ✓ TV Shows library added"
      else
        warn "  TV Shows library may already exist"
      fi
    else
      error "  Could not get auth token"
    fi
  else
    error "  Jellyfin authentication failed"
  fi
else
  log "  Jellyfin already configured (wizard not available)"
fi

echo ""

# ============================================
# Phase 5: Configure qBittorrent
# ============================================
step "5/10: Configuring qBittorrent..."

QB_COOKIE=""

# qBittorrent 4.6+ generates random temp password on first run
# Try temp password from docker logs FIRST (most likely on fresh install)
QB_TEMP=$(docker logs qbittorrent 2>&1 | grep "temporary password" | tail -1 | awk '{print $NF}' 2>/dev/null) || true

if [ -n "$QB_TEMP" ]; then
  log "  Found temp password: ${QB_TEMP:0:4}..."
  QB_LOGIN=$(curl -s -c - "http://127.0.0.1:8080/api/v2/auth/login" \
    --data-urlencode "username=admin" \
    --data-urlencode "password=$QB_TEMP" 2>/dev/null) || true
  QB_COOKIE=$(echo "$QB_LOGIN" | grep SID | awk '{print $NF}') || true
fi

# If temp didn't work, try configured password
if [ -z "$QB_COOKIE" ]; then
  log "  Trying configured password..."
  QB_LOGIN=$(curl -s -c - "http://127.0.0.1:8080/api/v2/auth/login" \
    --data-urlencode "username=admin" \
    --data-urlencode "password=${QB_PASSWORD:-adminadmin}" 2>/dev/null) || true
  QB_COOKIE=$(echo "$QB_LOGIN" | grep SID | awk '{print $NF}') || true
fi

if [ -n "$QB_COOKIE" ]; then
  log "  Logged in (SID: ${QB_COOKIE:0:8}...)"

  # Set preferences + new password
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/app/setPreferences" \
    --data-urlencode "json={\"save_path\":\"/downloads/complete/\",\"temp_path_enabled\":true,\"temp_path\":\"/downloads/incomplete/\",\"web_ui_password\":\"${QB_PASSWORD:-adminadmin}\"}" > /dev/null 2>&1
  log "  ✓ Download paths set"

  # Create categories
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/torrents/createCategory" \
    --data-urlencode "category=tv" --data-urlencode "savePath=/downloads/complete/" > /dev/null 2>&1
  curl -s -b "SID=$QB_COOKIE" "http://127.0.0.1:8080/api/v2/torrents/createCategory" \
    --data-urlencode "category=movies" --data-urlencode "savePath=/downloads/complete/" > /dev/null 2>&1
  log "  ✓ Categories created (tv, movies)"
else
  error "  qBittorrent login failed"
fi

echo ""

# ============================================
# Phase 6: Configure Sonarr
# ============================================
step "6/10: Configuring Sonarr..."

S_URL="http://127.0.0.1:8989"
S_AUTH="X-Api-Key: $SONARR_KEY"

# Download client
if api_call GET "$S_URL/api/v3/downloadclient" "" "$S_AUTH" && echo "$API_BODY" | grep -q "qBittorrent"; then
  log "  Download client already exists"
else
  log "  Adding qBittorrent..."
  api_call POST "$S_URL/api/v3/downloadclient" '{
    "enable":true,"protocol":"torrent","priority":1,
    "removeCompletedDownloads":true,"removeFailedDownloads":true,
    "name":"qBittorrent","implementation":"QBittorrent","configContract":"QBittorrentSettings",
    "fields":[
      {"name":"host","value":"qbittorrent"},{"name":"port","value":8080},
      {"name":"useSsl","value":false},{"name":"urlBase","value":""},
      {"name":"username","value":"admin"},{"name":"password","value":"'"${QB_PASSWORD:-adminadmin}"'"},
      {"name":"tvCategory","value":"tv"},{"name":"tvImportedCategory","value":""},
      {"name":"recentTvPriority","value":0},{"name":"olderTvPriority","value":0},
      {"name":"initialState","value":0},{"name":"sequentialOrder","value":false},
      {"name":"firstAndLast","value":false}
    ],"tags":[]}' "$S_AUTH" && log "  ✓ Download client added"
  verify_exists "$S_URL/api/v3/downloadclient" "qBittorrent" "$S_AUTH" "Sonarr download client"
fi

# Root folder
if api_call GET "$S_URL/api/v3/rootfolder" "" "$S_AUTH" && echo "$API_BODY" | grep -q "/tv"; then
  log "  Root folder /tv already exists"
else
  log "  Adding root folder /tv..."
  api_call POST "$S_URL/api/v3/rootfolder" '{"path":"/tv"}' "$S_AUTH" && log "  ✓ Root folder added"
  verify_exists "$S_URL/api/v3/rootfolder" "/tv" "$S_AUTH" "Sonarr root folder"
fi

echo ""

# ============================================
# Phase 7: Configure Radarr
# ============================================
step "7/10: Configuring Radarr..."

R_URL="http://127.0.0.1:7878"
R_AUTH="X-Api-Key: $RADARR_KEY"

# Download client
if api_call GET "$R_URL/api/v3/downloadclient" "" "$R_AUTH" && echo "$API_BODY" | grep -q "qBittorrent"; then
  log "  Download client already exists"
else
  log "  Adding qBittorrent..."
  api_call POST "$R_URL/api/v3/downloadclient" '{
    "enable":true,"protocol":"torrent","priority":1,
    "removeCompletedDownloads":true,"removeFailedDownloads":true,
    "name":"qBittorrent","implementation":"QBittorrent","configContract":"QBittorrentSettings",
    "fields":[
      {"name":"host","value":"qbittorrent"},{"name":"port","value":8080},
      {"name":"useSsl","value":false},{"name":"urlBase","value":""},
      {"name":"username","value":"admin"},{"name":"password","value":"'"${QB_PASSWORD:-adminadmin}"'"},
      {"name":"movieCategory","value":"movies"},{"name":"movieImportedCategory","value":""},
      {"name":"recentMoviePriority","value":0},{"name":"olderMoviePriority","value":0},
      {"name":"initialState","value":0},{"name":"sequentialOrder","value":false},
      {"name":"firstAndLast","value":false}
    ],"tags":[]}' "$R_AUTH" && log "  ✓ Download client added"
  verify_exists "$R_URL/api/v3/downloadclient" "qBittorrent" "$R_AUTH" "Radarr download client"
fi

# Root folder
if api_call GET "$R_URL/api/v3/rootfolder" "" "$R_AUTH" && echo "$API_BODY" | grep -q "/movies"; then
  log "  Root folder /movies already exists"
else
  log "  Adding root folder /movies..."
  api_call POST "$R_URL/api/v3/rootfolder" '{"path":"/movies"}' "$R_AUTH" && log "  ✓ Root folder added"
  verify_exists "$R_URL/api/v3/rootfolder" "/movies" "$R_AUTH" "Radarr root folder"
fi

echo ""

# ============================================
# Phase 8: Configure Prowlarr
# ============================================
step "8/10: Configuring Prowlarr..."

P_URL="http://127.0.0.1:9696"
P_AUTH="X-Api-Key: $PROWLARR_KEY"

# Nyaa.si
if api_call GET "$P_URL/api/v1/indexer" "" "$P_AUTH" && echo "$API_BODY" | grep -q "Nyaa"; then
  log "  Nyaa.si already exists"
else
  log "  Adding Nyaa.si..."
  api_call POST "$P_URL/api/v1/indexer" '{
    "name":"Nyaa.si","definitionName":"nyaasi",
    "implementation":"Cardigann","configContract":"CardigannSettings",
    "enable":true,"supportsRss":true,"supportsSearch":true,
    "appProfileId":1,"protocol":"torrent","privacy":"public","priority":25,
    "fields":[
      {"name":"definitionFile","value":"nyaasi"},
      {"name":"baseUrl","value":"https://nyaa.si/"},
      {"name":"torrentBaseSettings.appMinimumSeeders","value":""},
      {"name":"torrentBaseSettings.seedRatio","value":""},
      {"name":"torrentBaseSettings.seedTime","value":""}
    ],"tags":[]}' "$P_AUTH" && log "  ✓ Nyaa.si added"
  verify_exists "$P_URL/api/v1/indexer" "Nyaa" "$P_AUTH" "Nyaa.si indexer"
fi

# RuTracker
if [ -n "${RUTRACKER_USERNAME:-}" ] && [ -n "${RUTRACKER_PASSWORD:-}" ]; then
  if api_call GET "$P_URL/api/v1/indexer" "" "$P_AUTH" && echo "$API_BODY" | grep -q "RuTracker"; then
    log "  RuTracker already exists"
  else
    log "  Adding RuTracker..."
    api_call POST "$P_URL/api/v1/indexer" '{
      "name":"RuTracker","definitionName":"rutracker",
      "implementation":"Cardigann","configContract":"CardigannSettings",
      "enable":true,"supportsRss":true,"supportsSearch":true,
      "appProfileId":1,"protocol":"torrent","privacy":"private","priority":25,
      "fields":[
        {"name":"definitionFile","value":"rutracker"},
        {"name":"baseUrl","value":"https://rutracker.org/"},
        {"name":"username","value":"'"$RUTRACKER_USERNAME"'"},
        {"name":"password","value":"'"$RUTRACKER_PASSWORD"'"},
        {"name":"torrentBaseSettings.appMinimumSeeders","value":""},
        {"name":"torrentBaseSettings.seedRatio","value":""},
        {"name":"torrentBaseSettings.seedTime","value":""}
      ],"tags":[]}' "$P_AUTH" && log "  ✓ RuTracker added"
  fi
else
  warn "  RuTracker skipped (no credentials)"
fi

# App sync
if api_call GET "$P_URL/api/v1/applications" "" "$P_AUTH"; then
  APPS_BODY="$API_BODY"

  if echo "$APPS_BODY" | grep -q '"Sonarr"'; then
    log "  Sonarr sync already exists"
  else
    log "  Adding Sonarr sync..."
    api_call POST "$P_URL/api/v1/applications" '{
      "name":"Sonarr","syncLevel":"fullSync",
      "implementation":"Sonarr","configContract":"SonarrSettings",
      "fields":[
        {"name":"baseUrl","value":"http://sonarr:8989"},
        {"name":"prowlarrUrl","value":"http://localhost:9696"},
        {"name":"apiKey","value":"'"$SONARR_KEY"'"},
        {"name":"syncCategories","value":[5000,5010,5020,5030,5040,5045,5050,5070]},
        {"name":"animeSyncCategories","value":[5070]}
      ],"tags":[]}' "$P_AUTH" && log "  ✓ Sonarr sync added"
  fi

  if echo "$APPS_BODY" | grep -q '"Radarr"'; then
    log "  Radarr sync already exists"
  else
    log "  Adding Radarr sync..."
    api_call POST "$P_URL/api/v1/applications" '{
      "name":"Radarr","syncLevel":"fullSync",
      "implementation":"Radarr","configContract":"RadarrSettings",
      "fields":[
        {"name":"baseUrl","value":"http://localhost:7878"},
        {"name":"prowlarrUrl","value":"http://localhost:9696"},
        {"name":"apiKey","value":"'"$RADARR_KEY"'"},
        {"name":"syncCategories","value":[2000,2010,2020,2030,2040,2045,2050,2060,2070,2080]}
      ],"tags":[]}' "$P_AUTH" && log "  ✓ Radarr sync added"
  fi
fi

echo ""

# ============================================
# Phase 9: Configure Bazarr
# ============================================
step "9/10: Configuring Bazarr..."

BAZARR_CFG="/config/bazarr/config/config.yaml"
if [ -f "$BAZARR_CFG" ]; then
  python3 -c "
import yaml
config_path = '$BAZARR_CFG'
try:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f) or {}
except: config = {}

config.setdefault('sonarr', {}).update({'ip':'sonarr','port':'8989','base_url':'/sonarr','apikey':'$SONARR_KEY','full_update':'Daily','only_monitored':'True'})
config.setdefault('radarr', {}).update({'ip':'gluetun','port':'7878','base_url':'/radarr','apikey':'$RADARR_KEY','full_update':'Daily','only_monitored':'True'})
config.setdefault('general', {}).update({'use_sonarr':'True','use_radarr':'True','base_url':'/bazarr'})

with open(config_path, 'w') as f:
    yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
print('OK')
" 2>/dev/null && log "  ✓ Bazarr config updated" || warn "  Could not update Bazarr config"
else
  warn "  Bazarr config not found"
fi

echo ""

# ============================================
# Phase 10: Set Jellyfin Base URL + Restart services
# Must happen BEFORE Jellyseerr setup (needs working Jellyfin)
# ============================================
step "10/12: Applying URL Base + restarting services..."

JF_NET="/config/jellyfin/config/network.xml"
if [ -f "$JF_NET" ]; then
  sed -i 's|<BaseUrl />|<BaseUrl>/jellyfin</BaseUrl>|' "$JF_NET" 2>/dev/null || true
  sed -i 's|<BaseUrl></BaseUrl>|<BaseUrl>/jellyfin</BaseUrl>|' "$JF_NET" 2>/dev/null || true
  log "  ✓ Jellyfin Base URL → /jellyfin"
else
  mkdir -p /config/jellyfin/config
  cat > "$JF_NET" << 'XML'
<?xml version="1.0" encoding="utf-8"?>
<NetworkConfiguration xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <BaseUrl>/jellyfin</BaseUrl>
</NetworkConfiguration>
XML
  log "  ✓ Created network.xml with /jellyfin"
fi

log "  Restarting services to apply URL Bases..."
docker restart sonarr radarr prowlarr bazarr jellyfin 2>/dev/null || true
log "  Waiting for services to come back..."
sleep 15
wait_for_service "Sonarr" "http://127.0.0.1:8989"
wait_for_service "Radarr" "http://127.0.0.1:7878"
wait_for_service "Jellyfin" "http://127.0.0.1:8096"

echo ""

# ============================================
# Phase 11: Configure Jellyseerr (AFTER Jellyfin restart)
# ============================================
step "11/12: Configuring Jellyseerr..."

JS_URL="http://127.0.0.1:5055"
JS_COOKIES="/tmp/jellyseerr-cookies.txt"

# Check if already initialized
if api_call GET "$JS_URL/api/v1/settings/public"; then
  JS_INIT=$(echo "$API_BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('initialized',False))" 2>/dev/null) || true

  if [ "$JS_INIT" = "True" ]; then
    log "  Jellyseerr already initialized"
  else
    log "  Starting Jellyseerr setup..."

    # Step 1: Auth with Jellyfin (first user = admin)
    log "  Authenticating with Jellyfin..."
    if api_call_cookie POST "$JS_URL/api/v1/auth/jellyfin" \
      "{\"username\":\"${JELLYFIN_USER:-admin}\",\"password\":\"${JELLYFIN_PASSWORD:-admin}\",\"hostname\":\"http://jellyfin:8096/jellyfin\",\"email\":\"admin@media.server\"}" \
      "$JS_COOKIES"; then
      log "  ✓ Admin user created"
    else
      error "  Jellyfin auth failed"
    fi

    # Step 2: Configure Jellyfin server
    log "  Configuring Jellyfin connection..."
    api_call_cookie POST "$JS_URL/api/v1/settings/jellyfin" \
      '{"name":"Jellyfin","hostname":"jellyfin","port":8096,"useSsl":false,"urlBase":"/jellyfin"}' \
      "$JS_COOKIES" && log "  ✓ Jellyfin server configured"

    # Step 3: Sync and enable libraries
    log "  Syncing libraries..."
    if api_call_cookie GET "$JS_URL/api/v1/settings/jellyfin/library" "" "$JS_COOKIES"; then
      LIBS=$(echo "$API_BODY" | python3 -c "
import sys,json
libs = json.load(sys.stdin)
for l in libs: l['enabled'] = True
print(json.dumps(libs))
" 2>/dev/null) || true

      if [ -n "$LIBS" ]; then
        api_call_cookie POST "$JS_URL/api/v1/settings/jellyfin/library" "$LIBS" "$JS_COOKIES" \
          && log "  ✓ Libraries enabled"
      fi
    fi

    # Step 4: Add Sonarr
    log "  Adding Sonarr..."
    api_call_cookie POST "$JS_URL/api/v1/settings/sonarr" \
      '{"name":"Sonarr","hostname":"sonarr","port":8989,"apiKey":"'"$SONARR_KEY"'","useSsl":false,"baseUrl":"","activeProfileId":1,"activeProfileName":"Any","activeDirectory":"/tv","is4k":false,"isDefault":true,"enableSeasonFolders":true}' \
      "$JS_COOKIES" && log "  ✓ Sonarr added"

    # Step 5: Add Radarr
    log "  Adding Radarr..."
    api_call_cookie POST "$JS_URL/api/v1/settings/radarr" \
      '{"name":"Radarr","hostname":"gluetun","port":7878,"apiKey":"'"$RADARR_KEY"'","useSsl":false,"baseUrl":"","activeProfileId":1,"activeProfileName":"Any","activeDirectory":"/movies","is4k":false,"isDefault":true,"minimumAvailability":"released"}' \
      "$JS_COOKIES" && log "  ✓ Radarr added"

    # Step 6: Initialize
    log "  Finalizing..."
    api_call_cookie POST "$JS_URL/api/v1/settings/initialize" "" "$JS_COOKIES" \
      && log "  ✓ Jellyseerr initialized"

    rm -f "$JS_COOKIES"
  fi
fi

echo ""

# ============================================
# Phase 12: Final verification
# ============================================
step "12/12: Verifying all services..."

VERIFY_OK=0
VERIFY_FAIL=0
for check in "Sonarr:8989" "Radarr:7878" "Prowlarr:9696" "qBittorrent:8080" "Bazarr:6767" "TorrServer:8090" "Jellyfin:8096" "Jellyseerr:5055"; do
  name="${check%%:*}"
  port="${check##*:}"
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port" 2>/dev/null) || true
  case "$code" in
    200|302|307|308|401) log "  ✓ $name ($code)"; VERIFY_OK=$((VERIFY_OK + 1)) ;;
    *) error "  ✗ $name ($code)"; VERIFY_FAIL=$((VERIFY_FAIL + 1)) ;;
  esac
done

log "  Result: $VERIFY_OK OK, $VERIFY_FAIL failed"

echo ""
log "============================================"
log "  Media Server Auto-Configuration Complete!"
log "============================================"
echo ""
log "Configured:"
log "  ✓ Sonarr:      download client + /tv root folder"
log "  ✓ Radarr:      download client + /movies root folder"
log "  ✓ Prowlarr:    Nyaa.si + RuTracker, Sonarr/Radarr sync"
log "  ✓ qBittorrent: paths, categories, password"
log "  ✓ Bazarr:      Sonarr/Radarr connections"
log "  ✓ Jellyfin:    admin user, Movies/TV libraries"
log "  ✓ Jellyseerr:  Jellyfin auth, Sonarr/Radarr connections"
echo ""
log "URL Bases: /sonarr /radarr /prowlarr /bazarr /jellyfin"
