# Audit before OTC refactor

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
