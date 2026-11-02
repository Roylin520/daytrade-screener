# CLAUDE.md

A daily after-close screener for Taiwan day-trade candidates, run by GitHub Actions. `daytrade.py` is the whole app: standard library only, Python 3.11+. It follows the owner's `stockMarket` repo (same fetch helpers and CSV history style) but is independent of it. User-facing text is Traditional Chinese. Taiwan colours: red is up, green is down.

## Commands

No tests, linter or build step.

```sh
python -u daytrade.py                  # today's run; writes data/ and docs/index.html
python -u daytrade.py backfill 20      # replay the rules over past trading days
DATE=2026-10-30 python -u daytrade.py  # rerun one day, no email
FORCE=true python -u daytrade.py       # run on a closed day
```

## How it runs

`.github/workflows/daytrade.yml` runs at 19:00 and 20:30 Taipei on weekdays and on demand, then commits `docs/` and `data/` back to `main` as "Update screener …", so pull with `git pull --rebase` before pushing. `docs/index.html` is served by GitHub Pages. A scheduled run skips once the day finished without errors, retries if data was missing, and emails at most once a day (SMTP settings are GitHub Secrets; locally, `secrets.bat`, which is ignored by git).

## Rules that matter

- **Fail closed.** If a market's attention/disposal list can't be read, that market's stocks are excluded, never treated as clean. An empty attention list is reported as an error so the retry runs.
- **No look-ahead.** `analyse()` only reads history up to the data day. A plan is judged against the very next trading day, so evaluation only runs once days are contiguous (backfill defers it to the end) and never for a plan older than the stored history.
- **Conservative simulation.** `simulate()` counts a day touching both stop and target as a stop, and skips a gap past the target. Keep it that way: daily bars can't show the order of events.
- **Paper record is append-only in spirit.** `data/history_paper.csv` stores each plan before its result, with its own levels and cost, so config changes never rewrite the past. Settled rows are never replaced, plans can't be written once the next session has opened, backfill never writes to it, and days before `[paper] start` don't count. Don't tune parameters on it and then cite it as validation.
- Levels come from fixed formulas in units of the 5-day average range, so results stay comparable. Discretionary levels can't be backtested.
- `retain_days` stays small because `data/history_quotes.csv` is committed every day.

## Data sources

TWSE website JSON is throttled to 2 s per call because it blocks faster clients. Quotes come from `MI_INDEX` and `TWTB4U`, flows from `T86`, lists from `announcement/notice` and `punish`. TPEx uses `dailyQuotes`, `intraday/stat` (requires `type=Daily`, tables[1]), `insti/dailyTrade`, `bulletin/attention` (ignores `date=`, needs `startDate`/`endDate`) and `bulletin/disposal`. A stock with no day-trading row is treated as ineligible.
