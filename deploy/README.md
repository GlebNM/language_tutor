# VPS

На VPS запускаются приложение, внутренний MCP и Caddy. Нужны Docker Engine, Compose 2.24.4+ и свободные входящие TCP-порты 80/443. Caddy получает доверенный сертификат для публичного IPv4 через профиль Let's Encrypt `shortlived`, продлевает его автоматически и перенаправляет HTTP на HTTPS. Проверка владения IP проходит через HTTP на порту 80; этот порт должен оставаться доступным для продления. `default_sni` позволяет выбрать сертификат IP для браузеров без SNI. [Описание сертификатов IP](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability), [настройка ACME в Caddy](https://caddyserver.com/docs/caddyfile/directives/tls), [default_sni](https://caddyserver.com/docs/caddyfile/options#default-sni).

Скопируйте исходники в `/opt/language-tutor`, исключив `ssh_creds`, `.env`, credentials, базы, локальные данные, `.git`, `.venv` и `node_modules`. В этом каталоге:

```sh
cp .env.vps.example .env
# Укажите публичный IPv4 в PUBLIC_HOST и https://<IPv4> в PUBLIC_ORIGIN.
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

Откройте `https://<IPv4>`. Создайте собственную учётную запись. Новый том `learning-data` содержит новую установку; пользовательские базы, настройки и подписки с компьютера не переносятся. Учебный пример входит в образ. Backend и MCP не публикуют порты на хост. Caddy передаёт HTTPS и адрес клиента приложению; cookie сессии получает флаг Secure. Данные приложения и сертификаты переживают пересоздание контейнеров. Docker и контейнеры должны автоматически запускаться после перезагрузки VPS.

Для подключения личной подписки опубликуйте callback только на loopback VPS:

```sh
docker compose -f compose.yaml -f compose.vps.yaml -f compose.oauth.yaml up -d
```

На своём компьютере откройте туннель и оставьте его работать во время подключения:

```sh
ssh -N -L 1456:127.0.0.1:1456 root@<IPv4>
```

В браузере откройте приложение на VPS, перейдите в настройки и подключите подписку. OAuth вернёт браузер на `127.0.0.1:1456`, а туннель доставит callback приложению. Порт должен быть свободен на вашем компьютере. Если callback нужен после пересоздания контейнеров, задайте `COMPOSE_FILE=compose.yaml:compose.vps.yaml:compose.oauth.yaml` в серверном `.env`. Уже подключённая подписка работает без туннеля.

Обновление: замените исходники и выполните `docker compose up --build -d --wait --wait-timeout 180`. Проверка: `docker compose ps` и `curl --fail https://<IPv4>/api/health`. Журналы: `docker compose logs --tail=100`. Не используйте `docker compose down --volumes` при обновлении: эта команда удаляет данные и сертификаты.
