#!/usr/bin/env python3
"""Media Server Setup Wizard API"""

import http.server
import json
import subprocess
import os
import threading
import time
import urllib.request
import urllib.parse
import ssl
import re
import socketserver

PORT = 8888
MEDIA_DIR = os.environ.get('MEDIA_DIR', '/opt/media')
WIZARD_DIR = os.path.dirname(os.path.abspath(__file__))

# Deployment state - persisted to file for refresh survival
STATE_FILE = os.path.join(MEDIA_DIR, '.wizard_state.json')

def load_state():
    try:
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    except:
        return {'status': 'idle', 'step': '', 'progress': 0, 'logs': []}

def save_state():
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump(deploy_state, f)
    except:
        pass

deploy_state = load_state()


class WizardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WIZARD_DIR, **kwargs)

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            data = {}

        if self.path == '/api/validate/vpn':
            self.handle_validate_vpn(data)
        elif self.path == '/api/validate/rutracker':
            self.handle_validate_rutracker(data)
        elif self.path == '/api/validate/telegram':
            self.handle_validate_telegram(data)
        elif self.path == '/api/deploy':
            self.handle_deploy(data)
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_GET(self):
        if self.path == '/api/status':
            self.send_json(200, deploy_state)
        elif self.path == '/':
            self.path = '/index.html'
            super().do_GET()
        else:
            super().do_GET()

    def send_json(self, code, data):
        response = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(response))
        self.end_headers()
        self.wfile.write(response)

    def handle_validate_vpn(self, data):
        key = data.get('wireguard_private_key', '').strip()
        provider = data.get('provider', 'protonvpn')

        # Client-side format validation
        if not key:
            self.send_json(400, {'valid': False, 'error': 'WireGuard PrivateKey is required'})
            return

        # WireGuard key = base64 encoded 32 bytes = 44 chars ending with =
        if not re.match(r'^[A-Za-z0-9+/]{42,43}=$', key):
            self.send_json(400, {'valid': False, 'error': 'Invalid WireGuard key format. Must be base64, 44 characters'})
            return

        # Try actual VPN connection test using a quick gluetun container
        try:
            result = subprocess.run(
                ['docker', 'run', '--rm', '--cap-add=NET_ADMIN', '--device=/dev/net/tun',
                 '-e', f'VPN_SERVICE_PROVIDER={provider}',
                 '-e', 'VPN_TYPE=wireguard',
                 '-e', f'WIREGUARD_PRIVATE_KEY={key}',
                 '-e', 'SERVER_COUNTRIES=Netherlands',
                 '-e', 'FREE_ONLY=on',
                 'qmcgaw/gluetun:latest',
                 '/bin/sh', '-c', 'sleep 15 && wget -qO- https://ipinfo.io/json'],
                capture_output=True, text=True, timeout=45
            )

            if result.returncode == 0 and 'ip' in result.stdout:
                ip_info = json.loads(result.stdout)
                self.send_json(200, {
                    'valid': True,
                    'ip': ip_info.get('ip', ''),
                    'country': ip_info.get('country', ''),
                    'city': ip_info.get('city', ''),
                    'message': f"VPN OK! IP: {ip_info.get('ip', '?')} ({ip_info.get('country', '?')}, {ip_info.get('city', '?')})"
                })
                return
        except (subprocess.TimeoutExpired, Exception):
            pass

        # If docker test fails, just validate format (VPN will be tested on deploy)
        self.send_json(200, {
            'valid': True,
            'message': 'Key format valid. VPN will be verified during deployment.'
        })

    def handle_validate_rutracker(self, data):
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()

        if not username or not password:
            self.send_json(400, {'valid': False, 'error': 'Username and password required'})
            return

        try:
            # Try to login to RuTracker
            login_data = urllib.parse.urlencode({
                'login_username': username,
                'login_password': password,
                'login': '\u0432\u0445\u043e\u0434'
            }).encode('utf-8')

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(
                'https://rutracker.org/forum/login.php',
                data=login_data,
                headers={
                    'User-Agent': 'Mozilla/5.0',
                    'Content-Type': 'application/x-www-form-urlencoded'
                }
            )

            response = urllib.request.urlopen(req, timeout=15, context=ctx)
            cookies = response.headers.get_all('Set-Cookie') or []
            cookie_str = ' '.join(str(c) for c in cookies)

            if 'bb_session' in cookie_str or response.url != 'https://rutracker.org/forum/login.php':
                self.send_json(200, {'valid': True, 'message': f'RuTracker login successful for {username}'})
            else:
                self.send_json(400, {'valid': False, 'error': 'Login failed. Check username/password.'})
        except Exception as e:
            # If can't reach rutracker (blocked), accept credentials as-is
            self.send_json(200, {
                'valid': True,
                'message': f'Cannot verify (site may be blocked). Credentials saved for {username}.'
            })

    def handle_validate_telegram(self, data):
        token = data.get('bot_token', '').strip()

        if not token:
            self.send_json(200, {'valid': True, 'message': 'Skipped (optional)', 'skipped': True})
            return

        # Validate format
        if not re.match(r'^\d+:[A-Za-z0-9_-]{30,}$', token):
            self.send_json(400, {'valid': False, 'error': 'Invalid token format. Expected: 123456:ABC-DEF...'})
            return

        try:
            ctx = ssl.create_default_context()
            req = urllib.request.Request(f'https://api.telegram.org/bot{token}/getMe')
            response = urllib.request.urlopen(req, timeout=10, context=ctx)
            result = json.loads(response.read().decode('utf-8'))

            if result.get('ok'):
                bot = result['result']
                self.send_json(200, {
                    'valid': True,
                    'bot_name': bot.get('first_name', ''),
                    'bot_username': bot.get('username', ''),
                    'message': f"Bot found: @{bot.get('username', '?')} ({bot.get('first_name', '?')})"
                })
            else:
                self.send_json(400, {'valid': False, 'error': 'Invalid bot token'})
        except Exception as e:
            self.send_json(400, {'valid': False, 'error': f'Telegram API error: {str(e)}'})

    def handle_deploy(self, data):
        global deploy_state

        if deploy_state['status'] == 'running':
            self.send_json(400, {'error': 'Deployment already in progress'})
            return

        deploy_state.clear()
        deploy_state.update({'status': 'running', 'step': 'Starting...', 'progress': 0, 'logs': []})
        save_state()

        # Start deployment in background thread
        thread = threading.Thread(target=run_deployment, args=(data,), daemon=True)
        thread.start()

        self.send_json(200, {'message': 'Deployment started'})

    def log_message(self, format, *args):
        # Suppress default access logs
        pass


def run_deployment(data):
    global deploy_state

    def update(step, progress, log_msg=''):
        deploy_state['step'] = step
        deploy_state['progress'] = progress
        if log_msg:
            deploy_state['logs'].append(log_msg)
        save_state()

    try:
        # Step 1: Generate .env
        update('Generating configuration...', 5, 'Creating .env file')

        env_content = f"""# === VPN Configuration ===
VPN_SERVICE_PROVIDER={data.get('vpn_provider', 'protonvpn')}
VPN_TYPE=wireguard
WIREGUARD_PRIVATE_KEY={data.get('wireguard_private_key', '')}
SERVER_COUNTRIES={data.get('vpn_country', 'Netherlands')}
FREE_ONLY={data.get('free_only', 'on')}

# === Common ===
PUID=1000
PGID=1000
TZ={data.get('timezone', 'Asia/Almaty')}

# === Telegram ===
TELEGRAM_BOT_TOKEN={data.get('telegram_bot_token', '')}
TELEGRAM_CHAT_ID={data.get('telegram_chat_id', '')}

# === qBittorrent ===
QB_PASSWORD={data.get('qb_password', 'changeme')}

# === RuTracker ===
RUTRACKER_USERNAME={data.get('rutracker_username', '')}
RUTRACKER_PASSWORD={data.get('rutracker_password', '')}

# === Jellyfin ===
JELLYFIN_USER={data.get('jellyfin_user', 'admin')}
JELLYFIN_PASSWORD={data.get('jellyfin_password', 'changeme')}
"""

        env_path = os.path.join(MEDIA_DIR, '.env')
        with open(env_path, 'w') as f:
            f.write(env_content)
        os.chmod(env_path, 0o600)
        update('Configuration saved', 10, '.env file created')

        # Step 2: Pull images
        update('Pulling Docker images...', 15, 'This may take a few minutes')
        result = subprocess.run(
            ['docker', 'compose', 'pull'],
            cwd=MEDIA_DIR, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            update('Pull failed', 15, result.stderr[-500:] if result.stderr else 'Unknown error')
            deploy_state['status'] = 'error'
            return
        update('Images pulled', 40, 'All Docker images downloaded')

        # Step 3: Start VPN first
        update('Starting VPN...', 45, 'Connecting to ProtonVPN')
        subprocess.run(
            ['docker', 'compose', 'up', '-d', 'gluetun'],
            cwd=MEDIA_DIR, capture_output=True, text=True, timeout=60
        )

        # Wait for VPN healthy
        for i in range(30):
            result = subprocess.run(
                ['docker', 'inspect', 'gluetun', '--format', '{{.State.Health.Status}}'],
                capture_output=True, text=True
            )
            if 'healthy' in result.stdout:
                break
            time.sleep(2)

        if 'healthy' not in result.stdout:
            update('VPN failed to connect', 45, 'Gluetun did not become healthy')
            deploy_state['status'] = 'error'
            return

        # Get VPN IP
        ip_result = subprocess.run(
            ['docker', 'exec', 'gluetun', 'wget', '-qO-', 'https://ipinfo.io/ip'],
            capture_output=True, text=True, timeout=15
        )
        vpn_ip = ip_result.stdout.strip() if ip_result.returncode == 0 else 'unknown'
        update('VPN connected', 55, f'VPN IP: {vpn_ip}')

        # Step 4: Start all services
        update('Starting services...', 60, 'Launching all containers')
        subprocess.run(
            ['docker', 'compose', 'up', '-d'],
            cwd=MEDIA_DIR, capture_output=True, text=True, timeout=120
        )
        update('Services starting', 70, 'Waiting for services to initialize')
        time.sleep(15)

        # Step 5: Wait for init container
        update('Auto-configuring services...', 75, 'Init container is setting up connections')
        for i in range(60):
            result = subprocess.run(
                ['docker', 'inspect', 'media-init', '--format', '{{.State.Status}}'],
                capture_output=True, text=True
            )
            status = result.stdout.strip()
            if status == 'exited':
                # Check exit code
                exit_result = subprocess.run(
                    ['docker', 'inspect', 'media-init', '--format', '{{.State.ExitCode}}'],
                    capture_output=True, text=True
                )
                exit_code = exit_result.stdout.strip()
                if exit_code == '0':
                    update('Auto-configuration complete', 85, 'All services configured')
                else:
                    init_logs = subprocess.run(
                        ['docker', 'logs', 'media-init', '--tail', '20'],
                        capture_output=True, text=True
                    )
                    update('Configuration warnings', 85, init_logs.stdout[-300:] if init_logs.stdout else 'Check logs')
                break
            time.sleep(3)

        # Step 6: Disable auth wizards in *arr services
        update('Configuring authentication...', 87, 'Setting up service auth')

        for config_path, service_name in [
            (os.path.join(MEDIA_DIR, 'config/sonarr/config.xml'), 'Sonarr'),
            (os.path.join(MEDIA_DIR, 'config/radarr/config.xml'), 'Radarr'),
            (os.path.join(MEDIA_DIR, 'config/prowlarr/config.xml'), 'Prowlarr'),
        ]:
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    content = f.read()
                # Set auth to Forms + DisabledForLocalAddresses (skips first-run wizard)
                content = content.replace(
                    '<AuthenticationMethod>None</AuthenticationMethod>',
                    '<AuthenticationMethod>Forms</AuthenticationMethod>'
                )
                content = content.replace(
                    '<AuthenticationRequired>Enabled</AuthenticationRequired>',
                    '<AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>'
                )
                # If no auth method set yet, add it
                if '<AuthenticationMethod>' not in content:
                    content = content.replace(
                        '</Config>',
                        '  <AuthenticationMethod>Forms</AuthenticationMethod>\n  <AuthenticationRequired>DisabledForLocalAddresses</AuthenticationRequired>\n</Config>'
                    )
                with open(config_path, 'w') as f:
                    f.write(content)

        # Step 7: Restore full Caddyfile and restart
        update('Applying URL settings...', 90, 'Switching to production Caddy config')

        # Read production Caddyfile and prepend staging CA (if rate limited)
        full_caddyfile = os.path.join(MEDIA_DIR, 'Caddyfile')
        caddy_config = os.path.join(MEDIA_DIR, 'config/caddy/Caddyfile')
        if os.path.exists(full_caddyfile):
            with open(full_caddyfile, 'r') as f:
                caddyfile_content = f.read()
            # Check current Caddyfile for staging CA setting
            current_caddy = ''
            if os.path.exists(caddy_config):
                with open(caddy_config, 'r') as f:
                    current_caddy = f.read()
            if 'acme-staging' in current_caddy:
                caddyfile_content = '{\n    acme_ca https://acme-staging-v02.api.letsencrypt.org/directory\n}\n\n' + caddyfile_content
            with open(caddy_config, 'w') as f:
                f.write(caddyfile_content)

        # Also fix Jellyfin base URL (Jellyfin overwrites network.xml on first start)
        jf_network = os.path.join(MEDIA_DIR, 'config/jellyfin/config/network.xml')
        if os.path.exists(jf_network):
            with open(jf_network, 'r') as f:
                jf_content = f.read()
            jf_content = jf_content.replace('<BaseUrl />', '<BaseUrl>/jellyfin</BaseUrl>')
            jf_content = jf_content.replace('<BaseUrl></BaseUrl>', '<BaseUrl>/jellyfin</BaseUrl>')
            with open(jf_network, 'w') as f:
                f.write(jf_content)

        subprocess.run(
            ['docker', 'compose', 'restart', 'sonarr', 'radarr', 'prowlarr', 'bazarr', 'jellyfin', 'caddy'],
            cwd=MEDIA_DIR, capture_output=True, text=True, timeout=60
        )
        update('Services restarting...', 92, 'Waiting for services to apply settings')
        time.sleep(20)

        # Step 8: Verify
        update('Verifying...', 95, 'Checking all services')
        services_ok = []
        services_fail = []

        checks = [
            ('Sonarr', 'http://localhost:8989'),
            ('Radarr', 'http://localhost:7878'),
            ('Prowlarr', 'http://localhost:9696'),
            ('qBittorrent', 'http://localhost:8080'),
            ('Bazarr', 'http://localhost:6767'),
            ('TorrServer', 'http://localhost:8090'),
            ('Jellyfin', 'http://localhost:8096'),
            ('Jellyseerr', 'http://localhost:5055'),
        ]

        for name, url in checks:
            try:
                req = urllib.request.Request(url)
                response = urllib.request.urlopen(req, timeout=5)
                if response.status in (200, 302):
                    services_ok.append(name)
                else:
                    services_fail.append(name)
            except:
                services_fail.append(name)

        update('Deployment complete!', 100,
               f"Services OK: {', '.join(services_ok)}. " +
               (f"Failed: {', '.join(services_fail)}" if services_fail else "All services running!"))

        deploy_state['status'] = 'complete'
        deploy_state['services_ok'] = services_ok
        deploy_state['services_fail'] = services_fail
        save_state()

    except Exception as e:
        deploy_state['status'] = 'error'
        deploy_state['step'] = f'Error: {str(e)}'
        deploy_state['logs'].append(str(e))
        save_state()


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True


if __name__ == '__main__':
    print(f"Setup Wizard running on http://0.0.0.0:{PORT}")
    server = ThreadedServer(('0.0.0.0', PORT), WizardHandler)
    server.serve_forever()
