# Audit before OTC refactor

Baseline: origin/main 65066670cd5a1e824d565febcec5cba970ad1700. Local source matched that commit. The delivered folder lacked Git metadata; origin history was fetched without replacing working files. Work is isolated on codex/otc-only-mvp.

No local .env or SQLite files were found. Environment secrets are not displayed or modified. No .env tracked in fetched main history. This is not a guarantee against historical secret exposure in other files/branches. Rotate any Telegram tokens, data-provider keys or retired exchange credentials previously published.

Findings: main starts unrelated scanners; OTC stream uses an unverified UTC+2 offset; payout/open checks accept unknown status and omit payout timestamps; signals are not persisted; no reproducible outcome settlement.

Deletion candidates are recorded in audit_removal_manifest.json before removal. Shared single-instance protection is retained; database code will be replaced with additive OTC tables and SQLite online backup before migration. Existing tables will never be dropped.
