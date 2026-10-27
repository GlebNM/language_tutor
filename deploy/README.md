# Необязательная конфигурация HTTPS

`compose.edge.yaml` добавляет Caddy с TLS и перенаправлением HTTP на HTTPS. Для этой конфигурации нужны Docker Engine, Compose 2.24.4+, публичный IPv4 и доступные TCP-порты 80/443. Пример настроек находится в `.env.edge.example`; адрес в нём предназначен для документации.

Для подготовки исходников следуйте правилу развёртывания из [AGENTS.md](../AGENTS.md). Исключите `.env`, `ssh_creds`, credentials, базы, локальные данные, `.git`, `.venv` и `node_modules`.

```sh
cp .env.edge.example .env
# Укажите свой IPv4 в PUBLIC_HOST и https://<IPv4> в PUBLIC_ORIGIN.
docker compose config --quiet
docker compose up --build -d --wait --wait-timeout 180
docker compose ps
```

Caddy получает сертификат IP через профиль Let's Encrypt `shortlived`. Проверка HTTP-01 и последующее продление требуют доступного порта 80. `default_sni` выбирает сертификат для клиентов без SNI. [Сертификаты IP](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability), [ACME в Caddy](https://caddyserver.com/docs/caddyfile/directives/tls), [default_sni](https://caddyserver.com/docs/caddyfile/options#default-sni).

Приложение и MCP доступны внутри Compose; Caddy передаёт схему HTTPS и адрес клиента. Cookie сессии получает Secure. Тома приложения и сертификатов сохраняются при пересоздании контейнеров. При первом запуске создайте учётную запись; каждый учащийся подключает AI в своих настройках.

Для временной модели задайте `OLLAMA_API_KEY` в `.env`, пересоздайте приложение и включите «Настройки → Учебный AI → Временная модель». Ключ передаётся только приложению.

Подключение подписки завершается в веб-интерфейсе: откройте страницу ChatGPT, подтвердите разрешение и при необходимости вставьте полный адрес из строки браузера в поле «Адрес после входа в ChatGPT». Автоматический loopback callback остаётся дополнительным вариантом для локального запуска с `compose.oauth.yaml`.

Для обновления используйте исходники выбранной отправленной ревизии и `docker compose up --build -d --wait --wait-timeout 180`. Проверка состояния: `docker compose ps` и `/api/health`. Журналы: `docker compose logs --tail=100`. Обновление выполняется с сохранением постоянных томов.
