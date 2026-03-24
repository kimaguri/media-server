# Media Server

Домашний медиа-сервер с Telegram-ботом для управления. Качает фильмы, сериалы, аниме — всё автоматически.

**Домен:** `lampa.sadmin.app`

---

## Быстрый старт (для пользователя)

### Что умеет бот в Telegram

Три кнопки внизу — это всё что нужно:

| Кнопка | Что делает |
|--------|-----------|
| 🔍 **Поиск** | Найти и скачать фильм/сериал/аниме |
| 📊 **Статус** | Что сейчас качается, раздаётся, перемещается |
| 📋 **Список** | Всё что мониторится и уже скачано |

Можно просто **написать название** — бот найдёт на всех трекерах сразу.

### Как скачать фильм

```
1. Нажми 🔍 Поиск → 🎬 Фильм
2. Напиши название (русское или английское)
3. Бот покажет список торрентов с размером и качеством
4. Нажми на нужный — скачивание начнётся
5. Бот пришлёт уведомление когда файл готов
6. Смотри в Jellyfin или через Lampa
```

### Как скачать сериал/аниме

```
1. Нажми 🔍 Поиск → 📺 Сериал
2. Напиши название
3. Выбери нужный торрент (сезонпак или отдельные серии)
4. Новые серии будут скачиваться АВТОМАТИЧЕСКИ
5. Уведомления в Telegram о каждой новой серии
```

### Как удалить

Удаляешь из Jellyfin → автоматически удаляется отовсюду:
- Из Sonarr/Radarr (файлы)
- Из qBittorrent (торрент перестаёт раздаваться)
- Уведомление в Telegram: "🗑 Удалено"

### Где смотреть

| Способ | Когда использовать |
|--------|-------------------|
| **Jellyfin** (`lampa.sadmin.app/jellyfin`) | Библиотека со скачанными файлами, постеры, метаданные |
| **Lampa** (приложение на телефоне/ТВ) | Стриминг торрентов в реальном времени, без скачивания |

---

## Как всё работает (полная схема)

### Два режима просмотра

**Режим 1: Скачивание через бота (основной)**

```
Ты пишешь в Telegram
    ↓
Media Hub ищет на трекерах через Prowlarr
    ↓
Ты выбираешь торрент
    ↓
Media Hub добавляет фильм/сериал в Sonarr или Radarr
    ↓
Sonarr/Radarr отправляет торрент в qBittorrent
    ↓
qBittorrent скачивает → файл импортируется в /tv/ или /movies/
    ↓
Bazarr подтягивает субтитры (RU/EN)
    ↓
Jellyfin видит файл → доступен для просмотра
    ↓
Торрент продолжает раздаваться
```

**Режим 2: Стриминг через Lampa (ручной)**

```
Открываешь Lampa → ищешь фильм → выбираешь торрент → смотришь прямо сейчас
```

Ничего не качается на сервер. TorrServer стримит видео как YouTube.

### Что происходит при удалении

```
Удаляешь из Jellyfin
    ↓
Jellyfin Webhook → media-hub
    ↓
media-hub удаляет из Sonarr/Radarr (файлы с диска)
    ↓
media-hub удаляет торрент из qBittorrent
    ↓
Telegram: "🗑 Удалено: Название"
```

### Архитектура

```
                    ┌─────────────────────┐
                    │   Telegram Bot      │
                    │   (ты пишешь сюда)  │
                    └────────┬────────────┘
                             │
                    ┌────────▼────────────┐
                    │    Media Hub        │
                    │  (мозг системы)     │
                    │  - поиск            │
                    │  - уведомления      │
                    │  - каскадное удал.  │
                    └──┬──────┬───────┬───┘
                       │      │       │
           ┌───────────▼┐  ┌──▼───┐  ┌▼──────────┐
           │  Prowlarr   │  │Sonarr│  │  Radarr   │
           │  (трекеры)  │  │(сер.)│  │  (фильмы) │
           │  через VPN  │  └──┬───┘  └──┬────────┘
           └─────────────┘     │         │
                          ┌────▼─────────▼───┐
                          │   qBittorrent    │
                          │   (качает P2P)   │
                          │   раздаёт потом  │
                          └────────┬─────────┘
                                   │
                          ┌────────▼─────────┐
                          │   Диск сервера   │
                          │  /movies/ /tv/   │
                          └──┬───────────┬───┘
                             │           │
                    ┌────────▼──┐   ┌────▼──────┐
                    │  Bazarr   │   │  Jellyfin │
                    │ (субтитры)│   │ (просмотр)│
                    └───────────┘   └───────────┘
```

### Сервисы

| Сервис | Зачем | VPN? |
|--------|-------|------|
| **Media Hub** | Telegram-бот. Поиск, скачивание, уведомления, каскадное удаление. Мозг всей системы | Нет |
| **Prowlarr** | Ищет торренты на RuTracker, Nyaa.si, Rutor | **Да** — трекеры заблокированы |
| **Sonarr** | Управляет сериалами/аниме. Мониторит новые серии, импортирует файлы | Нет |
| **Radarr** | Управляет фильмами. Импортирует файлы в правильные папки | **Да** — API заблокирован |
| **qBittorrent** | Качает и раздаёт торренты. Раздача до удаления из Jellyfin | Нет |
| **Bazarr** | Автоматические субтитры RU/EN | Нет |
| **Jellyfin** | Медиа-библиотека: постеры, метаданные, стриминг на устройства | Нет |
| **TorrServer** | Стриминг торрентов для Lampa (без скачивания на диск) | Нет |
| **Jellyseerr** | Веб-интерфейс для запросов контента (альтернатива боту) | Нет |
| **Sportarr** | Отслеживание спортивных трансляций | Нет |
| **Gluetun** | VPN-тоннель для Prowlarr и Radarr | Сам VPN |
| **Caddy** | HTTPS, reverse proxy для всех сервисов | Нет |

### Зачем VPN

VPN нужен только для **доступа к сайтам трекеров**, заблокированных в РФ.

- Prowlarr → ходит на RuTracker/Nyaa через VPN
- Radarr → его API блокирует дата-центровые IP
- qBittorrent → качает **без VPN** на полной скорости
- Всё остальное → без VPN

### Уведомления в Telegram

| Событие | Сообщение |
|---------|----------|
| Начало скачивания | ⬇️ Качаем: Название (размер, источник) |
| Прогресс 50% | 📊 Прогресс: 50% |
| Скачано | ✅ Скачано: Название |
| Удалено | 🗑 Удалено: Название |

---

## Установка с нуля

### Требования

- VPS: Ubuntu 22.04+, 2 vCPU / 2GB+ RAM / 30GB+ SSD
- Docker + Docker Compose
- Домен, направленный на IP сервера
- ProtonVPN Free аккаунт (WireGuard PrivateKey)
- Telegram Bot Token (от @BotFather)

### 1. Подготовка

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

### 2. Установка

```bash
cd /opt
git clone https://github.com/kimaguri/media-server.git media
cd media
cp .env.example .env
nano .env   # Заполнить: VPN ключ, Telegram токен, домен
nano config/caddy/Caddyfile   # Обновить домен
docker compose up -d
```

Init-контейнер (`media-init`) автоматически настроит все сервисы:
- qBittorrent: категории, пути
- Sonarr/Radarr: download client, root folders, webhooks
- Prowlarr: индексеры (Nyaa, RuTracker), синхронизация
- Bazarr: языки, подключение к Sonarr/Radarr
- Jellyfin: admin-пользователь, библиотеки

После первого запуска:
```bash
docker compose restart sonarr radarr prowlarr bazarr jellyfin
```

### 3. Настройка Lampa (опционально)

1. Установить Lampa (lampa.mx)
2. Settings → TorrServer: `https://ваш-домен/torrserver/`
3. Settings → Парсер: `https://jacred.xyz`

---

## URL-адреса сервисов

| Сервис | URL |
|--------|-----|
| Jellyfin | `https://домен/jellyfin` |
| Sonarr | `https://домен/sonarr` |
| Radarr | `https://домен/radarr` |
| Prowlarr | `https://домен/prowlarr` |
| qBittorrent | `https://домен/qbt/` |
| Bazarr | `https://домен/bazarr` |
| Jellyseerr | `https://домен/seerr` |
| TorrServer | `https://домен/torrserver/` |
| Sportarr | `https://домен/sportarr` |

> qBittorrent и TorrServer требуют `/` в конце URL.

---

## Обслуживание

### Обновление

```bash
cd /opt/media
docker compose pull
docker compose up -d
```

### Логи

```bash
docker compose logs -f media-hub    # Telegram-бот
docker compose logs -f sonarr       # Сериалы
docker compose logs -f qbittorrent  # Торренты
docker compose logs -f gluetun      # VPN
```

### Проверка VPN

```bash
docker exec gluetun wget -qO- https://ipinfo.io/ip    # NL IP
docker exec qbittorrent wget -qO- https://ipinfo.io/ip # VPS IP
```

### Бэкап

```bash
tar -czf /root/media-backup-$(date +%Y%m%d).tar.gz /opt/media/config/
```

---

## Структура на диске

```
/opt/media/
├── docker-compose.yml
├── .env                    # Секреты
├── hub/                    # Media Hub (Telegram-бот)
│   ├── server.py
│   └── Dockerfile
├── init/                   # Автонастройка сервисов
├── config/                 # Конфиги всех сервисов
│   ├── sonarr/
│   ├── radarr/
│   ├── prowlarr/
│   ├── qbittorrent/
│   ├── bazarr/
│   ├── jellyfin/
│   ├── torrserver/
│   ├── caddy/Caddyfile
│   └── ...
├── movies/                 # Фильмы (Radarr)
├── tv/                     # Сериалы/аниме (Sonarr)
├── sports/                 # Спорт
└── downloads/              # Загрузки qBittorrent
    ├── complete/
    └── incomplete/
```
