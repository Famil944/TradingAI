# Запуск на телефоне и публичные отчёты

Код: ветка `main` репозитория `Famil944/TradingAI`.
Отчёты: ветка `reports`, файл `reports/termux/latest.json`.
Синхронизация не делает `git pull`, не меняет код и не загружает базу или `.env`.
По просьбе владельца отчёты публичные. В них только агрегаты виртуальных исходов,
версии стратегий, причины отсева, число подтверждений, состояния рынка и возраст
heartbeat. Нет ID чата, символов сделок, сырых логов, цен, ключей или текста API.

## Установка

Для первого запуска рекомендуется Debian внутри Termux: это обычная Linux-среда
для Python-зависимостей. Нативная установка научных библиотек на Android может
потребовать сборки. Эти команды подготовлены, но на вашем телефоне ещё не проверены.

В Termux:

```sh
pkg update
pkg install proot-distro tmux
termux-wake-lock
proot-distro install debian
proot-distro login debian
```

В Debian:

```sh
apt update
apt install -y python3 python3-venv git gh ca-certificates tmux nano
python3 --version
git clone --branch main https://github.com/Famil944/TradingAI.git
cd TradingAI
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
test -f .env || cp .env.example .env
nano .env
```

Нужен Python 3.12 или новее. В `.env` локально задайте `TELEGRAM_BOT_TOKEN`,
`OTCHARTS_API_KEY`, `OTC_ADMIN_CHAT_ID`, а для Groq — его ключ. Установите
`OTC_PRIMARY_MODE=ready` и `OTC_ML_TRAINING=false`. Не отправляйте ключи в чат или Git.
Основной и Groq работают без XGBoost. Для XGBoost нужны совместимые ML-библиотеки
и модели из локального `data/`; они намеренно не публикуются вместе с кодом.
Без них источник останется недоступен, а не начнёт выдавать выдуманные прогнозы.

## GitHub для отчётов

В Debian один раз выполните:

```sh
gh auth login --hostname github.com --git-protocol https --web
gh auth status
```

Вход выполняется на вашем устройстве. Альтернатива — fine-grained token только
для `Famil944/TradingAI`, с `Contents: Read and write`, через защищённую локальную
настройку `GH_TOKEN`. Не вставляйте токен в командную строку, URL репозитория или Git.

Сначала сформируйте и просмотрите локальную сводку:

```sh
.venv/bin/python -m services.otc_report_sync --device termux
cat data/public-report-termux.json
```

Однократная публичная загрузка и затем постоянный процесс раз в 15 минут:

```sh
.venv/bin/python -m services.otc_report_sync --device termux --upload --allow-public
tmux new-session -d -s tradingai-reports '.venv/bin/python -u -m services.otc_report_sync --device termux --upload --allow-public --watch --interval 900 >> logs/report-sync.log 2>&1'
```

Перед командой `tmux` выполните `mkdir -p logs`. При отсутствии сети процесс
повторит загрузку в следующем цикле. История сводок сохраняется в Git-коммитах.
На разных устройствах используйте разные `--device` (например `windows` и `termux`).
Запускайте только один процесс синхронизации на имя устройства. При закрытии
Termux, остановке Android или потере сети свежих отчётов не будет: проверяйте
`generated_at` и `runtime.heartbeat_age_seconds`, а не только последнюю статистику.

## Запуск бота

Сначала остановите экземпляр на компьютере. Два устройства с одним Telegram-токеном
конкурируют за polling и лимит потока. Локальная защита от второго процесса не
распространяется между устройствами. Рабочие базы при необходимости переносите
отдельно, согласованной SQLite-копией, когда бот остановлен; публичная сводка не
является резервной копией и не переносит ожидающие результаты.

```sh
mkdir -p logs
tmux new-session -d -s tradingai 'OTC_ML_TRAINING=false .venv/bin/python -u main.py --otc-session-minutes 0 >> logs/termux-bot.log 2>&1'
tail -n 30 logs/termux-bot.log
```

Сеанс: до остановки. Чтобы завершить **процесс** бота, откройте
`tmux attach -t tradingai` и нажмите Ctrl+C. Кнопка «Остановить» прекращает
сканирование, но оставляет Telegram polling работающим. Для перехода между
телефоном и компьютером необходимо завершать процесс, а не только сканирование.

Отключите оптимизацию батареи для Termux; Android всё равно может завершить
фоновый процесс. Подробности установки: [официальный Termux](https://github.com/termux/termux-app#installation).
Права GitHub: [официальная инструкция по токенам](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

## Как проверить позже

Откройте [отчёт телефона](https://github.com/Famil944/TradingAI/blob/reports/reports/termux/latest.json)
и попросите проверить его. Отчёт появится после первой загрузки с телефона.
Я смогу прочитать опубликованную сводку в следующем чате, но сам факт синхронизации
не запускает постоянное наблюдение с моей стороны и не даёт доступ к телефону.

Код обновляйте отдельно, с остановленным ботом: `git pull --ff-only origin main`.
Ветка отчётов не должна сливаться в код при каждом обновлении.
