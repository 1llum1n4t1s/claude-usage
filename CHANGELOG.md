# Changelog

## [1.0.0] - 2026-04-18

- Add `--no-browser` CLI flag and `NO_BROWSER` environment variable to suppress automatic browser launch when the dashboard is started from a scheduled task
- Add background periodic scan while the dashboard is running (default 300s, tunable via `SCAN_INTERVAL_SEC`, set `0` to disable)
- Fix "page not found" error when reloading URLs with query strings (e.g. `/?range=7d`, `/api/data?t=123`)
- Serialize the background scan and `/api/rescan` with a shared lock to prevent SQLite concurrent-write errors and `DB_PATH.unlink()` races
- Clean up `usage.db-wal` / `usage.db-shm` side files on rescan to avoid leftover corruption
- Respect `--projects-dir` in the background periodic scan (previously only the startup scan honored it)
- Harden `SCAN_INTERVAL_SEC` parsing against non-integer values — fall back to the default instead of crashing
- Japanese UI redesign with washi-inspired light theme and sumi-inspired dark theme (auto-detected via `prefers-color-scheme`), plus scanner performance fixes
- Internal code cleanup, normalization, and optimization pass

## 2026-04-09

- Fix token counts inflated ~2x by deduplicating streaming events that share the same message ID
- Fix session cost totals that were inflated when sessions spanned multiple JSONL files
- Fix pricing to match current Anthropic API rates (Opus $5/$25, Sonnet $3/$15, Haiku $1/$5)
- Add CI test suite (84 tests) and GitHub Actions workflow running on every PR
- Add sortable columns to Sessions, Cost by Model, and new Cost by Project tables
- Add CSV export for Sessions and Projects (all filtered data, not just top 20)
- Add Rescan button to dashboard for full database rebuild
- Add Xcode project directory support and `--projects-dir` CLI option
- Non-Anthropic models (gemma, glm, etc.) no longer incorrectly charged at Sonnet rates
- CLI and dashboard now both compute costs per-turn for consistent results
