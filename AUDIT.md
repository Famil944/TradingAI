# Проверка доставки и интерфейса · 20.11.2026

Проверены текущий локальный код, пользовательское и административное меню,
маршруты отправки S1–S4, учёт доставки и существующие тесты. Рабочие базы
открывались только для чтения агрегатов; сетевые вызовы Telegram в тестах
заменены подставными клиентами. Рабочий бот не запускался и не перезапускался.

На момент чтения последние 100 доставленных S1 имели запас до входа
17,45–19,77 с, среднее 19,29 с. Последние 100 S2–S4: 10,58–13,83 с,
среднее 13,11 с. Это время ответа Telegram в журнале, не наблюдение телефона.

Исправления:

- Основной экран статистики заменён единым обзором циклов ×3 с отдельными S1–S4.
  Добавлены победы по попыткам 1/2/3, число известных WIN/LOSS и отдельные
  ожидание/ничьи/нет данных. Неизвестный номер победной попытки не выдумывается.
  Фильтры периода и экспирации сохраняются при переходах, есть общая и личная
  статистика по подтверждённым доставкам. Старые кнопки поддерживаются.
- Убраны дублирующие входы в статистику в основной админской панели. В личном
  кабинете видны подписка, сеанс, выбранные стратегии и экспирации, состояние потока.
  Список пользователей получил страницы; одобрение и продление разделены.
  Таблица access_grants защищает повторную обработку кнопки выдачи доступа.
- Опубликованные S1 сохраняются при завершении процесса и следующем запуске.
  Явная отмена будущего плана теперь передаётся уже получившему карточку подписчику;
  отменённые записи остаются исключёнными из статистики и новых рассылок.
- Расчёт/редактирование результатов вынесены в отдельные управляемые задачи,
  отменяемые при завершении. Планирование S1 и отправка S2–S4 больше не ожидают
  их сетевых ответов. Чтение котировок S1 ограничено периодом ожидающих планов.
  Новый режим доставки S2–S4 получил отдельный идентификатор версии.

- S2–S4 переведены с намеренного ожидания позднего окна на общие 15–20 секунд.
  Независимые отправки не ждут сетевых ответов друг друга. В рабочем сеансе
  добавлена финальная проверка актуальности котировок и доступности инструмента.
- Немедленная рассылка и резервный опрос подписчиков использовали проверку
  «ещё не доставлено» без атомарного резерва. Добавлена отдельная таблица
  delivery_attempts с уникальной парой пользователь/сигнал; она создаётся при
  следующем запуске. Существующие таблицы и история не удаляются.
- У подписчиков было допустимо начинать отправку за 3 секунды до входа без
  ограниченного времени сетевого запроса. Теперь перед каждым запросом проверяется
  общее окно, ожидание ограничено дедлайном. Опоздавший подтверждённый ответ
  помечается «НЕ ВХОДИТЬ» и не записывается как успешная доставка подписчику.
- Исключение рассылки S1 могло отменить уже подтверждённый сигнал владельца
  и его виртуальный учёт. Обработка этих ошибок разделена.
- Выключение стратегии в настройках больше не прекращает обновление результата
  ранее полученного сигнала при сохранённом доступе.
- В текущую админскую панель возвращены переходы к пользователям и предложениям.
  Справка, винрейт, обзор стратегий и возврат в меню обновляют сообщение вместо
  создания копий; пустой список предложений также имеет навигацию.
- Исправлены незакрытые SQLite-соединения в тестовых фикстурах Windows,
  устаревшие тестовые времена доставки и ожидания отключённого M1-сборщика.
  Проверка POSIX-прав сохраняет исходные права, тест символьной ссылки явно
  пропускается там, где Windows не предоставляет нужного разрешения.

Сохранены правила поиска направлений, раздельные резервы S1–S4/M2/M5/M15,
расчёт результатов и фоновое окно 30–40 секунд. Не включались реальные сделки,
не изменялись подписки, баланс, ключи, .env или статистика. Совпадения разных
независимых стратегий не удалялись из истории как «дубли».

Проверка: полный `python -m pytest --tb=short` — **499 passed, 1 skipped,
30 subtests passed**, 45,04 с. Пропущена только проверка создания символьной
ссылки без соответствующих прав Windows. Ruff E9/F63/F7/F82 для изменённых
модулей доставки и меню, а также `git diff --check` прошли. Тесты выполнялись
вне песочницы из-за блокировки локального socketpair, необходимого asyncio;
это не запуск рабочего бота.

Ограничения: время уведомления на телефоне и доставка всем подписчикам при
ограничениях Telegram не гарантируются. Неоднозначные сетевые ошибки сохраняют
резерв и могут приводить к пропуску сигнала. Дополнительная таблица не означает
exactly-once доставку Telegram. Реальный пользовательский сеанс, массовая нагрузка
и применение изменений к работающему процессу не проверялись. Винрейт журналов
не проверялся как доказательство доходности реального счёта. Опциональный публичный
канал имеет отдельный маршрут рассылки; текущая работа касается приватного бота.

## Исторический аудит перед OTC-рефакторингом

Baseline: origin/main 65066670cd5a1e824d565febcec5cba970ad1700. Local source matched that commit. The delivered folder lacked Git metadata; origin history was fetched without replacing working files. Work is isolated on codex/otc-only-mvp.

No local .env or SQLite files were found. Environment secrets are not displayed or modified. No .env tracked in fetched main history. This is not a guarantee against historical secret exposure in other files/branches. Rotate any Telegram tokens, data-provider keys or retired exchange credentials previously published.

Findings: main starts unrelated scanners; OTC stream uses an unverified UTC+2 offset; payout/open checks accept unknown status and omit payout timestamps; signals are not persisted; no reproducible outcome settlement.

Deletion candidates are recorded in audit_removal_manifest.json before removal. Shared single-instance protection is retained; database code will be replaced with additive OTC tables and SQLite online backup before migration. Existing tables will never be dropped.

## Implemented result

Removed retired scanners, clients, exports, risk/news/OCR modules and their tests (see manifest). Shared single-instance protection retained. Replaced database implementation with additive journal tables; existing tables/rows are untouched and backed up before first migration. Main, Telegram, config, dependencies, supervisor, CI and deployment definitions now use OTC only. Kept the existing deployment disk name and database path to avoid hiding existing server data.

Verified installed otcharts 0.7.0 source and publisher README. The explicit venue OTC UTC+2 conversion is backed by contract tests. Used async documented HTTP/SSE endpoints because the synchronous SDK stream reads with no timeout and is unsuitable for clean asyncio cancellation. Published Instrument omits payout timestamps and may have no open flag for Pocket Option. The adapter therefore blocks live signals; no receipt-time substitution or override was added.

All three required local configuration values are absent (values were not printed). No authenticated Telegram/provider/platform or Android checks performed. No push, merge or deployment performed. Physical Android dependency installation remains unverified.

Validation: 33 offline unit/integration tests, including a loopback HTTP/SSE server, actual strategy-to-journal path, access controls, SQLite backup preservation, cancellation/restart, missing expiry watchdog, UNKNOWN, replay future isolation and independent quote checks. Additional checks: compilation, dependency consistency, Ruff standard correctness/import rules, Git whitespace, package build/install and CLI smoke checks are recorded in the final report.

## Follow-up: explicitly authorized research metadata policy

The owner requested free-tier testing with signals despite missing payout/open metadata. Added explicit OTC_TEST_MODE and bounded REST snapshots for zero-stream plans; unknown metadata stays NULL and is disclosed in notifications. Strict mode remains available. Journal schema v2 relaxes only payout/payout_at nullability with an online backup and atomic data/index/trigger-preserving migration. Updated validation: 46 passing offline tests. No live credentials used, no push/deploy performed.


## 2026-11-21: S1 stricter selection; shadow experiments retired

S1 v2 requires the closed trigger candle to close inside Bollinger bands and
reject at least 60% of its high-low range in the signal direction. Missing
pattern evidence fails closed. The existing 15–20 second delivery window and
full-cycle WR calculation are unchanged; S2–S4 are unchanged.

Exploratory retrospective screen of 665 delivered, resolved WIN/LOSS cycles,
ordered chronologically: earlier 443 cycles had 88.49% WR; the filter retained
117 at 93.16%. Later 222 had 87.39%; retained 71 at 91.55%. This is a conditional
historical subset, not an independent prospective test or a replay of scheduling
and cooldowns. It excludes UNKNOWN/DRAW and does not establish future accuracy.

Early 30–40 second observer and community shadow cycle generation are retired
from runtime. Their databases are preserved; the owner menu no longer advertises
background WR/comparison. Legacy report routes remain for historical access.
