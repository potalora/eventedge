# EventEdge → `hermes` VPS deployment + Hermes monitoring

**Date:** 2026-06-18
**Status:** Approved design (pre-implementation)
**Author:** Pedro + Claude

## Goal

Run the EventEdge daily paper-trading cycle on the always-on `hermes` VPS instead of
the laptop (which sleeps on battery and silently missed/corrupted runs on 6/15–6/16),
and give the Hermes agent enough access to **catch failures, report daily, and answer
questions** over WhatsApp/Telegram.

### Success criteria
- The 16-cohort daily cycle runs **every weekday at 10:00 ET** on `hermes`, unattended,
  surviving reboots — independent of whether the Hermes container is up.
- API keys are transferred **once** over Tailscale; never re-entered, never committed.
- Hermes **alerts** the user if a run fails or doesn't happen; otherwise **writes the
  daily report** and **sends a summary**.
- The user can DM Hermes ad-hoc ("how's gen_002?", "rerun today") and get answers / a rerun.
- The Mac stops running the job (no dual-writes / divergence).

## Context & constraints (discovered during recon)

| Fact | Implication |
|---|---|
| `hermes` = Hetzner CPX31, Ubuntu 24.04, **7.6 GB RAM, 0 swap**, 108 GB free, Python 3.12, git | Project needs `>=3.10` → 3.12 OK. No swap + 8 GB shared with Hermes → add swap. |
| Hermes runs in **Docker** (`nousresearch/hermes-agent`), only `~/.hermes` mounted, `network_mode: host` | Hermes can't see host files or run the trading venv unless we add mounts. |
| Hermes has **cron/routines** (`~/.hermes/cron/jobs.json`) + **Telegram + WhatsApp** | Monitoring/reporting = a Hermes routine; alerts via existing channels. |
| Box timezone = **UTC** | Schedule must be TZ-pinned to `America/New_York` (DST-aware). |
| Trading **state is gitignored** (`data/generations`, `.worktrees`) | Fresh clone carries no state; start fresh (decided). |
| Timeout/MTM fixes are **uncommitted** | Must commit + push to `potalora/eventedge` `main` before clone. |
| gen_002 behavior = 3 config values: `reentry_cooldown_days:7`, `short_conviction_threshold:0.45`, `regime_vix_stressed:20.0` | "Start on gen 2" = set these in the live generation's `default_config.py`. |
| Tailscale up: Mac `pedros-macbook-air` ↔ `hermes` (100.112.88.99), passwordless SSH | Use tailnet for key transfer + admin. |

## Decisions (from brainstorming)
1. **Approach A** — native host execution; Hermes reads via mounts; reruns via trigger-file.
2. **Start fresh** on the VPS, **keep the generation framework**, live generation = **gen_002 behavior**.
3. **VPS is sole runner** — retire the Mac launchd job once the VPS run is verified.
4. Hermes writes the daily report into the repo's `docs/reports/` **and** messages a summary.
5. Hermes role = **watch + report + answer** (full read access; can re-trigger via trigger-file).

## Architecture (Approach A)

```
        ┌───────────────────────── hermes VPS (Ubuntu, UTC) ─────────────────────────┐
        │                                                                             │
 systemd timer (Mon–Fri 10:00 America/New_York, Persistent=true)                      │
        │  └─ trade.service → daily_trading.sh → run_generations.py run-daily         │
        │         writes: data/generations/** , data/logs/daily_YYYY-MM-DD.log        │
        │                                                                             │
 systemd path unit  ◀── touch .triggers/run-now  ── (rerun on request)                │
        │  └─ starts trade.service                                                    │
        │                                                                             │
   ~/trading_agents/         (host: venv, repo, state, .env chmod 600)                │
        │   data/      ──ro mount──►  ┌──────────── Hermes container ───────────┐     │
        │   data/logs/ ──ro mount──►  │  routine (cron ~10:30 ET):              │     │
        │   docs/reports/ ─rw mount─► │   - read manifest run_history + log     │     │
        │   .triggers/  ──rw mount──► │   - FAIL/missing → alert (WA/Telegram)  │     │
        │                            │   - else → write report + send summary   │     │
        │                            │  ad-hoc DM → read state, answer / rerun   │     │
        │                            └──────────────────────────────────────────┘     │
        └─────────────────────────────────────────────────────────────────────────────┘
   Mac: launchctl unload com.trading.daily.plist   (sole-runner cutover)
```

## Components / runbook

### 1. Prep on the Mac (before touching the VPS)
- **1a — commit the already-done fixes** (currently uncommitted in the working tree) and push to `potalora/eventedge` `main`:
  `scripts/daily_trading.sh`, `tradingagents/strategies/orchestration/{generation_manager,multi_strategy_engine}.py`,
  `tradingagents/strategies/data_sources/yfinance_source.py`, `tests/test_fetch_timeout.py`.
- **1b — new work: data-integrity guard.** The 6/16 NaN corruption was a **missing NaN guard**, not a write problem:
  `equity_snapshot._mark_to_market` does `if current_price is None or current_price <= 0` — but `nan <= 0` is
  `False`, so a NaN price (yfinance returning NaN during the suspended run) flows into `cash`/`short_liability`.
  Fix: NaN-guard `_current_price_for` (return `None` for NaN) and `_mark_to_market` (fall back to entry on NaN);
  add atomic write to `write_snapshot` as defense-in-depth, reusing the `generation_manager._save_manifest`
  tempfile+`os.replace` pattern. With tests; commit + push.
  - Note: `_save_manifest` is **already atomic** — no change. Today's manifest clobber was a read-modify-write
    race from hand-editing during a live run, not a code defect.
- Net effect: `main` carries every fix before the VPS clones it.

### 2. Code + venv on the VPS
- `git clone https://github.com/potalora/eventedge.git ~/trading_agents` — **auth already works**: the host
  `hermes` user has a stored `potalora` HTTPS token (`credential.helper=store --file=~/.hermes/.git-credentials`)
  that reaches `eventedge` today (verified `ls-remote` EXIT=0). No new deploy key needed. (The container has no
  git auth; the host's `id_ed25519` is not a GitHub key — irrelevant here.)
- `python3 -m venv ~/trading_agents/.venv && ~/trading_agents/.venv/bin/pip install ".[openbb]"`.
- Smoke: `~/trading_agents/.venv/bin/python -m pytest tests/ -q` (expect green; the stale `test_regulations_source` date test may fail — pre-existing, optionally fix).

### 3. Secrets over Tailscale
- From the Mac: `rsync -av --chmod=600 ~/ai_workspace/trading_agents/.env hermes:~/trading_agents/.env`
  (WireGuard-encrypted over the tailnet). Confirm `chmod 600`. Never committed (gitignored).

### 4. Live generation (fresh, gen_002 behavior)
- `cd ~/trading_agents && .venv/bin/python scripts/run_generations.py start "gen_002 risk-discipline — VPS fresh start 2026-06-18"`.
- In that generation's worktree `default_config.py`, set `risk_discipline` = `{reentry_cooldown_days:7, short_conviction_threshold:0.45, regime_vix_stressed:20.0}` (mirrors how gen_002 was created on the Mac). State starts empty.
- `BLOCKED_TICKERS=GOOGL` preserved (compliance) via `.env`.

### 5. Scheduling (systemd)
- `trade.service` (oneshot, `User=<deploy user>`, `WorkingDirectory=~/trading_agents`, `ExecStart=scripts/daily_trading.sh`).
- `trade.timer`: `OnCalendar=Mon..Fri 10:00`, `Persistent=true`, with `Environment=TZ=America/New_York` (or `OnCalendar=... America/New_York`). Enable + start.
- `daily_trading.sh` runs as-is: its weekend-skip is a no-op safety net and the `caffeinate` guard no-ops on Linux.
- `trade-rerun.path` watches `~/trading_agents/.triggers/run-now`; on change → `systemctl start trade.service` (then clears the file).

### 6. Hermes integration
- Add bind-mounts to Hermes `~/hermes-deploy/docker-compose.yml`:
  `~/trading_agents/data:/opt/trading/data:ro`, `~/trading_agents/data/logs:/opt/trading/logs:ro`,
  `~/trading_agents/docs/reports:/opt/trading/reports:rw`, `~/trading_agents/.triggers:/opt/trading/triggers:rw`.
  `docker compose up -d` to apply.
- Add a Hermes **routine** (cron job, ~10:30 ET, pinned model per Hermes convention) that:
  1. reads `/opt/trading/data/generations/manifest.json` `run_history` for today + the day's log;
  2. if today's run is missing or `success:false` → **alert** the user (WA/Telegram) with the error;
  3. else → read `data/generations/gen_*/horizon_*/*.json`, **write** `docs/reports/YYYY-MM-DD-genNNN-daily-report.md` (existing format), and **send a summary**.
- Ad-hoc: document a short Hermes skill/prompt so DMs like "how's gen_002?" read the mounted state; "rerun today" → `touch /opt/trading/triggers/run-now`.

### 7. Memory safety
- `fallocate -l 4G /swapfile && chmod 600 && mkswap && swapon` + `/etc/fstab` entry.
- After first real run, check peak RSS (`systemd-cgtop` / log) to confirm headroom alongside Hermes.

### 8. Mac cutover
- After ≥1 verified green VPS run: `launchctl unload ~/Library/LaunchAgents/com.trading.daily.plist` on the Mac. Keep the plist + archived state for reference/rollback.

## Failure handling & monitoring
- **Run fails / errors:** non-zero exit + manifest `success:false`; Hermes routine alerts.
- **Run didn't happen (box down at 10:00):** `Persistent=true` fires on next boot; Hermes routine sees no today-row and alerts.
- **OOM:** swap added; first-run RSS watched; mem can be capped via systemd `MemoryMax=` if needed.
- **Hermes container down:** trading still runs (host systemd); only reporting pauses. (Hermes is `restart: unless-stopped`.)

## Verification
- VPS: a manual `run_generations.py run-daily --date <today>` completes, 16/16 cohorts, no NaN, state under the gen dir.
- Timer: `systemctl list-timers` shows next ET fire; a forced run via the timer succeeds.
- Trigger: `touch .triggers/run-now` from inside the Hermes container starts a run.
- Hermes: routine sends a real summary; a simulated failure (e.g. rename a key) produces an alert.

## Rollback
- Re-`launchctl load` the Mac plist (Mac resumes running). VPS state is independent and archived.
- Remove the systemd units / Hermes mounts to fully back out. No data loss (fresh-start, Mac archive intact).

## Open items / risks
- ~~Private clone auth~~ **RESOLVED** — host already has a working `potalora` HTTPS token in
  `~/.hermes/.git-credentials`; clone/pull work with no new credential. (Trade-off noted: it's a broad
  account-scoped PAT, but it is *already* on the box for Hermes regardless, so reusing it adds no new
  exposure. A repo-scoped read-only deploy key is an option if you later want least-privilege.)
- **Hermes routine authoring** — exact `jobs.json` schema + model pin to confirm against the live file.
- **First-run RSS** unknown — swap mitigates; confirm empirically.
- **Atomic-write change** touches the live write path — covered by tests before deploy.
```
