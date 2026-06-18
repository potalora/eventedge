# EventEdge → `hermes` VPS Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the EventEdge daily paper-trading cycle on the always-on `hermes` VPS (sole runner), transfer API keys once over Tailscale, and have the Hermes agent watch/report/answer over WhatsApp/Telegram.

**Architecture:** Approach A — trading runs natively on the host (systemd timer + venv), independent of the Dockerized Hermes; Hermes reads trading state via read-only bind-mounts, writes the daily report to a rw-mounted reports dir, alerts on failure, and triggers reruns by touching a file watched by a host `systemd path` unit. Fresh state; live generation = gen_002 risk-discipline behavior; generation framework preserved.

**Tech Stack:** Python 3.12 venv, systemd (timer + path units), Docker Compose (Hermes), Tailscale (transport), bash.

## Global Constraints

- Project requires `python >=3.10`; VPS has 3.12 — OK, no pyenv.
- VPS is **UTC**; the daily run must fire at **10:00 America/New_York** (DST-aware). Hermes routine uses a fixed UTC time that is always *after* the run completes in both EDT and EST: **15:30 UTC**.
- VPS has **7.6 GB RAM, 0 swap**, shared with Hermes (~2.4 GB) — add swap; cap the trade run with `MemoryMax=4G`.
- Trading **state is gitignored** — fresh start on the VPS; no state migration.
- Secrets: the trading `.env` (15 keys) is copied over Tailscale only, `chmod 600`, never committed.
- Repo auth on the VPS already works via the host's stored `potalora` HTTPS token (`~/.hermes/.git-credentials`) — no new credential.
- gen_002 behavior = `risk_discipline = {reentry_cooldown_days:7, short_conviction_threshold:0.45, regime_vix_stressed:20.0}`.
- Host paths: repo `/home/hermes/trading_agents`, venv `/home/hermes/trading_agents/.venv`, Hermes compose `/home/hermes/hermes-deploy/docker-compose.yml`, Hermes data `/home/hermes/.hermes`.
- Reach the box: `ssh hermes@hermes` (Tailscale, passwordless). Container ops: `docker exec hermes …`.

---

## Phase 0 — Code prep (Mac, lands on `main` before the VPS clones)

### Task 1: Commit the already-done timeout/MTM fixes

**Files:**
- Modify (already changed in working tree): `scripts/daily_trading.sh`, `tradingagents/strategies/orchestration/generation_manager.py`, `tradingagents/strategies/orchestration/multi_strategy_engine.py`, `tradingagents/strategies/data_sources/yfinance_source.py`
- Add (already created): `tests/test_fetch_timeout.py`

**Interfaces:**
- Produces: `main` branch carrying `caffeinate` wrap, bounded pool fetch (`_gather_with_timeout`, `_fetch_timeout_s`), open-position pricing (`_positions_to_price`), `yf.download(timeout=30)`, and the 3600s timeout-string fix.

- [ ] **Step 1: Create a feature branch** (session is on `main`)

```bash
cd /Users/potalora/ai_workspace/trading_agents
git checkout -b feat/vps-deploy-hardening
```

- [ ] **Step 2: Run the full suite, confirm only the known pre-existing failure**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: `1 failed, 593 passed` — the only failure is `test_regulations_source.py::test_get_recent_proposed_rules_filters_each_agency_by_date` (pre-existing stale-date test, unrelated).

- [ ] **Step 3: Stage and commit the fixes**

```bash
git add scripts/daily_trading.sh \
  tradingagents/strategies/orchestration/generation_manager.py \
  tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/data_sources/yfinance_source.py \
  tests/test_fetch_timeout.py
git commit -m "fix(engine): bound data fetch, mark open positions to market, fix timeout msg

Adds caffeinate wrap (Mac), pool-level fetch deadline with non-blocking
shutdown, yfinance download timeout, open-position pricing so held shorts
are marked to market, and corrects the stale 600s timeout string.

Claude-Session: https://claude.ai/code/session_017Gv19EEzB21JNU22NiXuN2"
```

### Task 2: NaN-guard + atomic write in the equity snapshot

**Files:**
- Modify: `tradingagents/strategies/state/equity_snapshot.py`
- Test: `tests/test_equity_snapshot_nan.py` (create)

**Interfaces:**
- Consumes: existing `_mark_to_market(trade, current_price)`, `_current_price_for(ticker, price_cache)`, `write_snapshot(...)`.
- Produces: `_mark_to_market` and `_current_price_for` reject NaN prices (fall back to entry / return None); `write_snapshot` writes atomically via a new module helper `_atomic_write_text(path, text)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_equity_snapshot_nan.py
import math
import pytest
import pandas as pd
from tradingagents.strategies.state import equity_snapshot as es


def test_mark_to_market_long_nan_falls_back_to_entry():
    trade = {"entry_price": 100.0, "shares": 10, "direction": "long"}
    pv, upnl = es._mark_to_market(trade, float("nan"))
    assert pv == 1000.0   # entry*shares, NOT nan
    assert upnl == 0.0


def test_mark_to_market_short_nan_falls_back_to_entry():
    trade = {"entry_price": 50.0, "shares": 4, "direction": "short"}
    pv, upnl = es._mark_to_market(trade, float("nan"))
    assert pv == -200.0   # -entry*shares liability
    assert upnl == 0.0


def test_current_price_for_returns_none_on_nan_last_close():
    df = pd.DataFrame({"Close": [101.0, float("nan")]})
    assert es._current_price_for("X", {"X": df}) is None


def test_write_snapshot_atomic_preserves_prior_on_failure(tmp_path, monkeypatch):
    sd = str(tmp_path)
    es.write_snapshot(sd, "2026-06-12", cash=5000, open_trades=[],
                      closed_trades=[], price_cache=None, total_capital=5000)

    def boom(*a, **k):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(es.os, "replace", boom)
    with pytest.raises(RuntimeError):
        es.write_snapshot(sd, "2026-06-18", cash=4000, open_trades=[],
                          closed_trades=[], price_cache=None, total_capital=5000)

    rows = es.load_snapshots(sd)
    assert [r["date"] for r in rows] == ["2026-06-12"]   # prior intact, not truncated
    assert list(tmp_path.glob("*.tmp")) == []            # no leftover temp files
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_equity_snapshot_nan.py -v`
Expected: FAIL — NaN tests fail (NaN propagates / value is nan), atomic test fails (prior file truncated or tmp leftover).

- [ ] **Step 3: Add the NaN guards + atomic helper**

In `tradingagents/strategies/state/equity_snapshot.py`, add `import math` and `import tempfile` near the top imports (it already imports `os`, `json`, `logging`).

Replace the guard in `_mark_to_market`:

```python
    if current_price is None or math.isnan(current_price) or current_price <= 0:
        current_price = entry
```

Replace the return in `_current_price_for`:

```python
    try:
        v = float(df["Close"].iloc[-1])
    except (KeyError, IndexError, ValueError):
        return None
    return None if math.isnan(v) else v
```

Add the atomic helper (module level, after the imports):

```python
def _atomic_write_text(path: str, text: str) -> None:
    """Write text via a temp file + os.replace so a crash can't truncate the
    existing file. Mirrors generation_manager._save_manifest."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
```

Replace the final write block in `write_snapshot` (the `with open(path, "w") as f: ...` loop):

```python
    text = "".join(json.dumps(by_date[d]) + "\n" for d in sorted(by_date))
    _atomic_write_text(path, text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_equity_snapshot_nan.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the broader state/engine suite for regressions**

Run: `.venv/bin/python -m pytest tests/test_multi_strategy.py tests/test_30day_simulation.py tests/test_fetch_timeout.py -q`
Expected: all pass.

- [ ] **Step 6: Commit, push, open PR, merge to main**

```bash
git add tradingagents/strategies/state/equity_snapshot.py tests/test_equity_snapshot_nan.py \
  docs/superpowers/specs/2026-06-18-vps-deploy-design.md docs/superpowers/plans/2026-06-18-vps-deploy.md
git commit -m "fix(snapshot): guard NaN prices + atomic write; add VPS deploy spec/plan

Claude-Session: https://claude.ai/code/session_017Gv19EEzB21JNU22NiXuN2"
git push -u private feat/vps-deploy-hardening
gh pr create --base main --repo potalora/eventedge --title "VPS deploy hardening: fetch timeouts, MTM, NaN guard" \
  --body "Timeout/MTM/NaN fixes + VPS deployment spec & plan.

https://claude.ai/code/session_017Gv19EEzB21JNU22NiXuN2"
# After review:
gh pr merge --merge --repo potalora/eventedge
```

Expected: PR merged; `private/main` now carries all fixes. **STOP — do not start Phase 1 until main is merged.**

---

## Phase 1 — VPS base (runnable checkout)

### Task 3: Add swap on the VPS

**Files:** none in-repo (host config).

- [ ] **Step 1: Create and enable a 4 GB swapfile**

```bash
ssh hermes@hermes 'sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile && \
  sudo mkswap /swapfile && sudo swapon /swapfile && \
  echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab'
```

- [ ] **Step 2: Verify swap is active**

Run: `ssh hermes@hermes 'free -h | grep -i swap'`
Expected: `Swap: 4.0Gi 0B 4.0Gi`.

### Task 4: Clone repo, build venv, transfer secrets, smoke-test

**Files:** none in-repo (host setup); copies the Mac `.env`.

- [ ] **Step 1: Clone the repo on the VPS (auth already works)**

```bash
ssh hermes@hermes 'git clone https://github.com/potalora/eventedge.git ~/trading_agents'
```
Expected: clone succeeds with no credential prompt (uses the stored `potalora` token).

- [ ] **Step 2: Create venv and install**

```bash
ssh hermes@hermes 'cd ~/trading_agents && python3 -m venv .venv && \
  .venv/bin/pip install --upgrade pip && .venv/bin/pip install ".[openbb]"'
```
Expected: install completes (this is the heaviest step; allow several minutes).

- [ ] **Step 3: Copy the `.env` over Tailscale, lock perms**

```bash
rsync -av --chmod=600 /Users/potalora/ai_workspace/trading_agents/.env hermes:~/trading_agents/.env
ssh hermes@hermes 'ls -l ~/trading_agents/.env'
```
Expected: file present, `-rw-------` (600).

- [ ] **Step 4: Smoke-test the suite on the VPS**

Run: `ssh hermes@hermes 'cd ~/trading_agents && .venv/bin/python -m pytest tests/ -q'`
Expected: green except the known pre-existing `test_regulations_source` stale-date failure.

### Task 5: Create the live generation (gen_002 behavior) + verify a manual run

**Files:** edits the generation's worktree `default_config.py` on the VPS.

- [ ] **Step 1: Start the first generation (fresh state)**

```bash
ssh hermes@hermes 'cd ~/trading_agents && \
  .venv/bin/python scripts/run_generations.py start "gen_002 risk-discipline — VPS fresh start 2026-06-18"'
```
Expected: prints a new `gen_001` (first on this box) with a worktree under `.worktrees/gen_001` and empty state under `data/generations/gen_001`.

- [ ] **Step 2: Activate gen_002 risk-discipline values in that generation's worktree config**

```bash
ssh hermes@hermes 'cd ~/trading_agents && \
  sed -i "s/\"reentry_cooldown_days\": 0,/\"reentry_cooldown_days\": 7,/; \
          s/\"short_conviction_threshold\": 0.60,/\"short_conviction_threshold\": 0.45,/; \
          s/\"regime_vix_stressed\": 25.0,/\"regime_vix_stressed\": 20.0,/" \
    .worktrees/gen_001/tradingagents/default_config.py && \
  grep -nE "reentry_cooldown_days|short_conviction_threshold|regime_vix_stressed" \
    .worktrees/gen_001/tradingagents/default_config.py'
```
Expected: the three lines now read `7`, `0.45`, `20.0`.

- [ ] **Step 3: Run one daily cycle manually (set compliance block)**

```bash
ssh hermes@hermes 'cd ~/trading_agents && BLOCKED_TICKERS=GOOGL \
  .venv/bin/python scripts/run_generations.py run-daily --date $(TZ=America/New_York date +%F)'
```
Expected: completes; manifest shows `gen_001` run `success=true`.

- [ ] **Step 4: Verify clean state (16 cohorts, no NaN)**

```bash
ssh hermes@hermes 'cd ~/trading_agents && .venv/bin/python - <<PY
import json, glob
n=nan=0
for d in glob.glob("data/generations/gen_001/horizon_*"):
    for l in open(f"{d}/equity_snapshots.jsonl"):
        n+=1
        if "NaN" in l: nan+=1
print("cohorts:", len(glob.glob("data/generations/gen_001/horizon_*")), "rows:", n, "NaN:", nan)
PY'
```
Expected: `cohorts: 16`, `NaN: 0`.

---

## Phase 2 — Scheduling (systemd)

### Task 6: Install the daily timer (10:00 ET, weekdays, persistent)

**Files:** Create on host: `/etc/systemd/system/trade.service`, `/etc/systemd/system/trade.timer`.

- [ ] **Step 1: Write the service + timer units**

```bash
ssh hermes@hermes 'sudo tee /etc/systemd/system/trade.service >/dev/null <<UNIT
[Unit]
Description=EventEdge daily paper-trading cycle
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=hermes
Group=hermes
Environment=HOME=/home/hermes
WorkingDirectory=/home/hermes/trading_agents
Environment=BLOCKED_TICKERS=GOOGL
ExecStart=/home/hermes/trading_agents/scripts/daily_trading.sh
MemoryMax=4G
UNIT'

ssh hermes@hermes 'sudo tee /etc/systemd/system/trade.timer >/dev/null <<UNIT
[Unit]
Description=Run EventEdge daily cycle weekdays at 10:00 ET

[Timer]
OnCalendar=Mon..Fri 10:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
UNIT'
```

- [ ] **Step 2: Enable + verify the next fire time**

```bash
ssh hermes@hermes 'sudo systemctl daemon-reload && sudo systemctl enable --now trade.timer && \
  systemctl list-timers trade.timer --no-pager'
```
Expected: `trade.timer` listed with NEXT at the upcoming weekday 10:00 ET (shown in box-local UTC).

- [ ] **Step 3: Force a one-off run through the unit, confirm success**

```bash
ssh hermes@hermes 'sudo systemctl start trade.service && sleep 2 && \
  systemctl is-active trade.service; journalctl -u trade.service -n 5 --no-pager'
```
Expected: the service runs (oneshot → `inactive (dead)` after completion); journal shows the script output, exit 0.

### Task 7: Trigger-file rerun (host path unit)

**Files:** Create on host: `/etc/systemd/system/trade-rerun.path`, `/etc/systemd/system/trade-rerun.service`; create `~/trading_agents/.triggers/`.

**Interfaces:**
- Produces: touching `/home/hermes/trading_agents/.triggers/run-now` starts `trade.service`. (Hermes touches it via the rw mount in Task 8.)

- [ ] **Step 1: Create the trigger dir (writable by the hermes user/container UID)**

```bash
ssh hermes@hermes 'mkdir -p ~/trading_agents/.triggers'
```

- [ ] **Step 2: Write the path + handler units**

```bash
ssh hermes@hermes 'sudo tee /etc/systemd/system/trade-rerun.path >/dev/null <<UNIT
[Unit]
Description=Watch for trading rerun trigger

[Path]
PathChanged=/home/hermes/trading_agents/.triggers/run-now
Unit=trade-rerun.service

[Install]
WantedBy=paths.target
UNIT'

ssh hermes@hermes 'sudo tee /etc/systemd/system/trade-rerun.service >/dev/null <<UNIT
[Unit]
Description=Handle trading rerun trigger

[Service]
Type=oneshot
ExecStartPre=-/bin/rm -f /home/hermes/trading_agents/.triggers/run-now
ExecStart=/bin/systemctl start trade.service
UNIT'
```
Note: `trade-rerun.service` runs as root (no `User=`) so it can start the system `trade.service`; it first clears the trigger file.

- [ ] **Step 3: Enable + test the trigger**

```bash
ssh hermes@hermes 'sudo systemctl daemon-reload && sudo systemctl enable --now trade-rerun.path && \
  touch ~/trading_agents/.triggers/run-now && sleep 3 && \
  journalctl -u trade.service -n 3 --no-pager && ls ~/trading_agents/.triggers/'
```
Expected: `trade.service` started by the trigger; `run-now` removed (dir empty).

---

## Phase 3 — Hermes integration

### Task 8: Mount trading state into the Hermes container

**Files:** Modify on host: `/home/hermes/hermes-deploy/docker-compose.yml`.

- [ ] **Step 1: Back up the compose file**

```bash
ssh hermes@hermes 'cp ~/hermes-deploy/docker-compose.yml ~/hermes-deploy/docker-compose.yml.bak'
```

- [ ] **Step 2: Add the three bind-mounts under the existing `volumes:` block**

Add these lines under `volumes:` (alongside the existing `${HOME}/.hermes:/opt/data`):

```yaml
      - ${HOME}/trading_agents/data:/opt/trading/data:ro
      - ${HOME}/trading_agents/docs/reports:/opt/trading/reports:rw
      - ${HOME}/trading_agents/.triggers:/opt/trading/triggers:rw
```
(`data` ro covers state + `data/logs`; `reports` rw for the report Hermes writes; `triggers` rw for reruns.)

- [ ] **Step 3: Recreate the container and verify the mounts**

```bash
ssh hermes@hermes 'cd ~/hermes-deploy && docker compose up -d && \
  docker exec hermes sh -lc "ls /opt/trading/data/generations/manifest.json && \
    head -c 80 /opt/trading/data/generations/manifest.json && echo && \
    touch /opt/trading/triggers/.probe && ls /opt/trading/triggers/ && rm /opt/trading/triggers/.probe"'
```
Expected: manifest readable from inside the container; trigger dir writable.

### Task 9: Hermes monitoring + report routine

**Files:** Hermes runtime config `~/.hermes/cron/jobs.json` (or via the `hermes` CLI).

**Interfaces:**
- Consumes: mounted `/opt/trading/data` (ro), `/opt/trading/reports` (rw). Messaging via the already-paired WhatsApp/Telegram.

- [ ] **Step 1: Inspect the existing cron schema (so the new job matches it)**

```bash
ssh hermes@hermes 'docker exec hermes sh -lc "command -v hermes && hermes cron list 2>/dev/null | head -30" || \
  python3 -c "import json;d=json.load(open(\"/home/hermes/.hermes/cron/jobs.json\"));print(json.dumps(d[:1] if isinstance(d,list) else d,indent=2)[:1200])"'
```
Expected: reveals the job object shape (id, schedule/cron expression, prompt, model pin). Use the `hermes cron` CLI if present; otherwise mirror this JSON shape.

- [ ] **Step 2: Add the routine** (cron `30 15 * * 1-5` = 15:30 UTC, always after the 10:00 ET run completes in both EDT/EST; pin model per the existing jobs, e.g. gemini-3.5-flash)

Routine prompt (use as the job's prompt):

```
You monitor the EventEdge trading system, mounted read-only at /opt/trading
(state under /opt/trading/data, write reports to /opt/trading/reports).

1. Read /opt/trading/data/generations/manifest.json. For each generation with
   status "active", find its run_history entry whose date == today (America/New_York).
2. If any active generation has NO entry for today, OR its today entry has
   success=false: message me (the owner) on WhatsApp/Telegram an ALERT with the
   gen_id, the error field, and the last 20 lines of
   /opt/trading/data/logs/daily_<today>.log. Then stop.
3. Otherwise (all active gens ran success=true today): read the per-cohort JSON
   under /opt/trading/data/generations/<gen>/horizon_*/ for today, write
   /opt/trading/reports/<today>-<gen>-daily-report.md following the format of the
   newest existing file in that directory, and send me a concise summary: regime,
   signal count + direction, trades, % capital deployed, and any silent strategy
   with its likely cause.
```

Apply via `docker exec -it hermes hermes cron add …` if the CLI supports it, else edit `~/.hermes/cron/jobs.json` to append a job with the shape from Step 1, then `cd ~/hermes-deploy && docker compose restart`.

- [ ] **Step 3: Verify success path**

```bash
ssh hermes@hermes 'docker exec hermes sh -lc "hermes cron run <job-id> 2>/dev/null" || echo "trigger via your phone: DM Hermes: run the trading monitor now"'
```
Expected: you receive a summary message; a `*-daily-report.md` appears in `~/trading_agents/docs/reports/`.

- [ ] **Step 4: Verify failure-alert path (non-destructive)**

Run the routine while there is legitimately **no run for the target day** — e.g. invoke it manually
*before* the next day's 10:00 ET run, or DM Hermes: "run the trading monitor as if today were
<next weekday>". With no `run_history` entry for that date, the routine must take the alert branch.

```bash
ssh hermes@hermes 'docker exec hermes sh -lc "hermes cron run <job-id>"'   # run before today's cycle
```
Expected: you receive an ALERT message ("no run recorded for <date>") rather than a summary. This
exercises the alert path without writing any synthetic/failed state into the real manifest.

---

## Phase 4 — Cutover + final verification

### Task 10: Retire the Mac launchd job + end-to-end check

**Files:** Mac `~/Library/LaunchAgents/com.trading.daily.plist` (unload, keep file).

- [ ] **Step 1: Confirm the VPS has ≥1 green run and the timer is armed**

```bash
ssh hermes@hermes 'cd ~/trading_agents && \
  .venv/bin/python -c "import json;m=json.load(open(\"data/generations/manifest.json\"));print([(g[\"gen_id\"],g[\"run_history\"][-1][\"success\"]) for g in m[\"generations\"]])" && \
  systemctl list-timers trade.timer --no-pager'
```
Expected: last run `True`; timer NEXT shows the upcoming weekday.

- [ ] **Step 2: Stop the Mac from running the job**

```bash
launchctl unload ~/Library/LaunchAgents/com.trading.daily.plist
launchctl list | grep com.trading.daily || echo "Mac job retired (no longer loaded)"
```
Expected: the job no longer appears. (Plist file kept for rollback.)

- [ ] **Step 3: Final end-to-end sign-off (next morning or forced)**

```bash
# Force the whole chain once and confirm Hermes reports it:
ssh hermes@hermes 'touch ~/trading_agents/.triggers/run-now'   # triggers trade.service
# then run the Hermes monitor and confirm you get a summary on your phone.
```
Expected: run executes via the trigger; Hermes sends a summary; report file written.

---

## Self-review notes (coverage)

- Spec §1 prep → Tasks 1–2. §2 code/venv/secrets → Task 4. §4 generation → Task 5. §5 scheduling → Tasks 6–7. §6 Hermes → Tasks 8–9. §7 swap → Task 3. §8 Mac cutover → Task 10. §1b NaN/atomic → Task 2.
- Rollback (spec): Task 10 keeps the plist; VPS state independent.
- Open item (Hermes jobs.json schema) handled explicitly in Task 9 Step 1 (inspect-then-mirror) and the `<job-id>` placeholders are resolved at runtime from that inspection.
```
