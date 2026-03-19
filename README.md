# Media Server

Автоматизированный медиа-сервер для отслеживания аниме, сериалов, фильмов и футбольных матчей. Развёрнут на VPS с Docker Compose.

**Домен:** `lampa.sadmin.app`

## Как это работает

В стеке два режима потребления контента, которые работают параллельно:

### Режим 1: Стриминг через Lampa (ручной)

```
Lampa (телефон/ТВ) → ищешь фильм → выбираешь торрент → TorrServer стримит в реальном времени
```

- Ничего не скачивается на диск сервера
- Смотришь прямо сейчас, как YouTube
- Качество зависит от скорости интернета
- Подходит для: "хочу посмотреть фильм прямо сейчас"

### Режим 2: Автоматическая качка (автопилот)

```
Sonarr/Radarr мониторят → Prowlarr ищет на трекерах → qBittorrent качает → Bazarr добавляет субтитры → Telegram уведомляет
```

- Добавляешь сериал/фильм один раз — дальше всё автоматически
- Новые серии Naruto? Скачаются сами, с субтитрами, уведомление в Telegram
- Файлы на диске в максимальном качестве
- После просмотра: Trakt.tv отмечает → Deleterr удаляет через N дней
- Подходит для: "хочу следить за сериалами на автопилоте"

### Пайплайн автоудаления просмотренного

```
Смотришь в Lampa → TraktTV плагин отправляет scrobble → ≥80% просмотрено = watched
→ Deleterr видит в Trakt API → ждёт grace period (7 дней фильмы / 14 дней сериалы)
→ удаляет файл + unmonitor в Sonarr/Radarr
```

## Архитектура

```
┌──────────────────────────────┐
│  Gluetun VPN (Netherlands)   │
│  ├── Prowlarr (поиск)        │──── HTTP ────→ RuTracker, Nyaa.si, Rutor
│  └── Radarr (фильмы)         │               (заблокированные РКН)
└──────────┬───────────────────┘
           │ Docker internal network
           ▼
┌────────────────────────────────────────────────────────┐
│  Прямая сеть VPS (полная скорость)                     │
│  ├── qBittorrent ←── P2P скачивание ←── пиры           │
│  ├── Sonarr ←→ Prowlarr + qBittorrent (сериалы/аниме)  │
│  ├── Bazarr ←→ Sonarr/Radarr (субтитры RU/EN)          │
│  ├── TorrServer ←── стриминг ←── Lampa (телефон/ТВ)    │
│  ├── Seerr ←→ Sonarr/Radarr (UI для запросов)          │
│  ├── Deleterr ←→ Trakt.tv + Sonarr/Radarr (автоудал.)  │
│  └── Caddy (reverse proxy, HTTPS, Let's Encrypt)        │
└────────────────────────────────────────────────────────┘
```

## Что установлено и зачем

| Сервис | Назначение | За VPN? |
|--------|-----------|---------|
| **Gluetun** | VPN-тоннель (WireGuard/ProtonVPN Free). Даёт NL IP для доступа к заблокированным трекерам | Сам является VPN |
| **Prowlarr** | Менеджер индексеров. Ходит на RuTracker, Nyaa.si, Rutor и другие трекеры для поиска торрентов. Синхронизирует индексеры в Sonarr/Radarr | **Да** — трекеры заблокированы РКН |
| **Radarr** | Автоматическое отслеживание и скачивание фильмов. Мониторит новые релизы, отправляет торренты в qBittorrent | **Да** — api.radarr.video блокирует дата-центровые IP (Cloudflare) |
| **Sonarr** | Автоматическое отслеживание сериалов и аниме. Мониторит новые эпизоды, скачивает автоматически | Нет |
| **qBittorrent** | Торрент-клиент. Скачивает файлы на полной скорости VPS (без VPN, чтобы не замедлять) | Нет |
| **Bazarr** | Автоматические субтитры. Подтягивает RU/EN субтитры из OpenSubtitles, Podnapisi | Нет |
| **TorrServer** | Стриминг торрентов. Lampa на телефоне/ТВ отправляет magnet-ссылку → TorrServer стримит видео в реальном времени без скачивания на диск | Нет |
| **Seerr** | Веб-интерфейс для запросов контента. Удобный мобильный UI для добавления фильмов/сериалов в Sonarr/Radarr | Нет |
| **Deleterr** | Автоудаление просмотренного контента. Читает Trakt.tv API, удаляет файлы после grace period | Нет |
| **Jellyfin** | Медиа-сервер. Сканирует скачанные файлы, создаёт библиотеку с постерами/метаданными, стримит на устройства. Интеграция с Lampa через плагин | Нет |
| **Caddy** | Reverse proxy с автоматическим HTTPS (Let's Encrypt). Все сервисы доступны через один домен | Нет |
| **Sportarr** | Отслеживание спортивных трансляций (La Liga, Barcelona) | Нет |

## Зачем VPN

VPN решает **одну задачу**: доступ к сайтам трекеров, заблокированных РКН (RuTracker, Nyaa.si и др.).

- **Prowlarr** — делает HTTP-запросы к трекерам для поиска → нужен VPN
- **Radarr** — его API (api.radarr.video) блокирует дата-центровые IP → нужен VPN
- **qBittorrent** — скачивает P2P напрямую, без VPN, на полной скорости VPS
- **Все остальные** — работают без VPN

ProtonVPN Free подходит, потому что Prowlarr/Radarr делают обычные HTTP-запросы, а не P2P-трафик.

## URL-адреса

| Сервис | URL |
|--------|-----|
| Sonarr | `https://lampa.sadmin.app/sonarr` |
| Radarr | `https://lampa.sadmin.app/radarr` |
| Prowlarr | `https://lampa.sadmin.app/prowlarr` |
| qBittorrent | `https://lampa.sadmin.app/qbt/` |
| Bazarr | `https://lampa.sadmin.app/bazarr` |
| Seerr | `https://lampa.sadmin.app/seerr` |
| Deleterr | `https://lampa.sadmin.app/deleterr` |
| TorrServer | `https://lampa.sadmin.app/torrserver/` |

> qBittorrent и TorrServer требуют trailing slash (`/qbt/`, `/torrserver/`), т.к. не поддерживают URL Base нативно.

## Порты

| Сервис | Порт | Bind | Открыт наружу? |
|--------|------|------|----------------|
| Gluetun (Prowlarr) | 9696 | 127.0.0.1 | Нет, через Caddy |
| Gluetun (Radarr) | 7878 | 127.0.0.1 | Нет, через Caddy |
| qBittorrent WebUI | 8080 | 127.0.0.1 | Нет, через Caddy |
| qBittorrent P2P | 6881 | 0.0.0.0 | Да (нужно для пиров) |
| Sonarr | 8989 | 127.0.0.1 | Нет, через Caddy |
| Bazarr | 6767 | 127.0.0.1 | Нет, через Caddy |
| TorrServer | 8090 | 0.0.0.0 | Да (для Lampa) |
| Seerr | 5055 | 127.0.0.1 | Нет, через Caddy |
| Deleterr | 8082 | 127.0.0.1 | Нет, через Caddy |
| Caddy HTTP | 80 | 0.0.0.0 | Да |
| Caddy HTTPS | 443 | 0.0.0.0 | Да |

## Коммуникации между сервисами

```
Prowlarr (VPN) ───HTTP поиск───→ RuTracker, Nyaa.si, Rutor
Prowlarr (VPN) ───sync────────→ Sonarr/Radarr (Docker DNS)
Sonarr/Radarr  ───.torrent─────→ qBittorrent (прямая сеть)
qBittorrent    ───P2P──────────→ Интернет (полная скорость VPS)
qBittorrent    ───complete─────→ Sonarr/Radarr (webhook)
Bazarr         ───API──────────→ Sonarr/Radarr + провайдеры субтитров
Seerr          ───API──────────→ Sonarr/Radarr
Lampa (моб)    ───stream───────→ TorrServer
Lampa (Trakt)  ───watched─────→ Trakt.tv (бесплатный)
Deleterr       ───poll─────────→ Trakt.tv API → удаление через Sonarr/Radarr
*arr сервисы   ───notify───────→ Telegram Bot
```

## Бесплатные внешние сервисы

| Сервис | Зачем | Стоимость |
|--------|-------|-----------|
| ProtonVPN Free | VPN для Prowlarr/Radarr (доступ к трекерам) | $0 |
| Trakt.tv | Трекинг просмотренного → автоудаление | $0 |
| OpenSubtitles.com | Провайдер субтитров для Bazarr | $0 |
| Telegram Bot | Уведомления о скачивании | $0 |

## Установка с нуля

### Требования

- VPS: Ubuntu 22.04+, 2 vCPU / 2GB+ RAM / 30GB+ SSD
- Docker + Docker Compose
- Домен, направленный на VPS IP
- ProtonVPN Free аккаунт (WireGuard PrivateKey)

### 1. Подготовка VPS

```bash
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin curl ufw

# Swap (если RAM < 4GB)
fallocate -l 4G /swapfile && chmod 600 /swapfile
mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# Firewall
ufw allow 22,80,443,8090,6881/tcp
ufw allow 6881/udp
ufw --force enable
```

### 2. Структура каталогов

```bash
mkdir -p /opt/media/{movies,tv,sports,downloads/complete,downloads/incomplete}
mkdir -p /opt/media/config/{gluetun,prowlarr,qbittorrent,sonarr,radarr,bazarr,sportarr,seerr,torrserver,deleterr,caddy}
chown -R 1000:1000 /opt/media
```

### 3. Конфигурация

```bash
cd /opt/media
# Скопировать docker-compose.yml, Caddyfile, .env из этого репозитория
cp .env.example .env
# Заполнить .env: VPN ключ, Telegram токен
nano .env
# Обновить домен в Caddyfile
nano config/caddy/Caddyfile
```

### 4. Запуск

```bash
docker compose up -d
# Init-контейнер автоматически настроит все сервисы:
# - URL Base для каждого сервиса
# - qBittorrent: пути скачивания, категории
# - Sonarr/Radarr: download client, root folders
# - Prowlarr: индексеры (Nyaa.si, RuTracker), синхронизация с Sonarr/Radarr
# - Bazarr: подключение к Sonarr/Radarr, языки
# - Jellyfin: admin пользователь, библиотеки Movies/TV

# После первого запуска перезапустить для применения URL Base:
docker compose restart sonarr radarr prowlarr bazarr jellyfin
```

Все сервисы настраиваются автоматически через init-контейнер (`media-init`). Ручная настройка через UI не требуется.

**Lampa → TorrServer:**
- Settings → TorrServer → URL: `https://your-domain.com/torrserver/`

## Настройка Lampa

1. Установить Lampa (lampa.mx или приложение)
2. Settings → TorrServer: `https://lampa.sadmin.app/torrserver/`
3. Settings → Парсер: `https://jacred.xyz` (или другой рабочий)
4. Установить TraktTV плагин: Settings → Plugins → `https://nb557.github.io/plugins/trakt.js`
5. Авторизовать Trakt в плагине

## Обслуживание

### Обновление контейнеров

```bash
cd /opt/media
docker compose pull
docker compose up -d
```

### Просмотр логов

```bash
docker compose logs -f gluetun     # VPN
docker compose logs -f prowlarr    # Индексеры
docker compose logs -f sonarr      # Сериалы
docker compose logs -f qbittorrent # Торренты
```

### Проверка VPN

```bash
# Prowlarr/Radarr через VPN (должен быть NL IP):
docker exec gluetun wget -qO- https://ipinfo.io/ip

# qBittorrent напрямую (должен быть VPS IP):
docker exec qbittorrent wget -qO- https://ipinfo.io/ip
```

### Бэкап конфигурации

```bash
tar -czf /root/media-backup-$(date +%Y%m%d).tar.gz /opt/media/config/
```

## Структура файлов на VPS

```
/opt/media/
├── docker-compose.yml
├── .env                          # Секреты (VPN ключ, Telegram токен)
├── config/
│   ├── gluetun/                  # VPN конфигурация
│   ├── prowlarr/                 # Индексеры, связи с arr
│   ├── radarr/                   # Фильмы, профили качества
│   ├── sonarr/                   # Сериалы, профили качества
│   ├── qbittorrent/              # Торрент-клиент
│   ├── bazarr/                   # Субтитры
│   ├── sportarr/                 # Спорт
│   ├── seerr/                    # UI для запросов
│   ├── deleterr/                 # Автоудаление
│   ├── torrserver/               # Стриминг для Lampa
│   └── caddy/
│       ├── Caddyfile             # Reverse proxy конфигурация
│       ├── data/                 # HTTPS сертификаты
│       └── config/
├── downloads/
│   ├── complete/                 # Завершённые загрузки
│   └── incomplete/               # Незавершённые загрузки
├── movies/                       # Фильмы (Radarr)
├── tv/                           # Сериалы/аниме (Sonarr)
└── sports/                       # Спорт (Sportarr)
```
