# EventEdge Autoresearch Architecture

## Daily session lifecycle

The systemd timer source in `deploy/systemd/trade.timer` is configured for
18:00 America/New_York on Monday through Friday. This schedule is only a wakeup:
both daily CLIs parse the requested ISO date and require an exact XNYS session.
They never roll a holiday or weekend to another trading date.

For each valid session, every cohort follows the same execution-first order:

1. Validate the complete exact-session market-data set before economic mutation.
2. Apply corporate actions, execute exits, then execute entries at the session open.
3. Accrue explicit borrow and financing costs, mark positions, record SPY/BIL
   total-return benchmarks, and snapshot the account.
4. Fetch shared event evidence and screen the four horizons after marking.
5. Persist close-observed signals and stage eligible intents for the next exact
   XNYS session open. A close signal cannot fill in the same session.
6. Project read-compatible JSON from the ledger for existing reports and readers.

## State authority

Each cohort owns `<state-dir>/<cohort-name>/portfolio.db`. SQLite is authoritative
for signals, intents, fills, lots, marks, benchmarks, snapshots, cash movements,
slippage, commissions, other fees, borrow expense, and financing expense. JSON
files such as `paper_trades.json` and `equity_snapshots.jsonl` are deterministic
compatibility projections and must not be used to reconstruct accounting state.

Legacy JSON can be inventoried with `scripts/migrate_ledger_state.py`. The tool
hashes every regular input before and after inspection and never imports legacy
positions or performance. `--initialize-clean` creates only 16 empty ledger
schemas with configured opening cash; it is a readiness operation, not a
promotion or deployment.

## Portfolio scenarios

EventEdge runs 16 dependent scenario portfolios. Headline performance shows
four separate $100k horizon books plus an equal-weighted scenario panel; the
panel is not investable fund AUM. Smaller books are concentration stress tests.
Metrics use XNYS sessions, next-session-open signal outcomes, persisted SPY/BIL
benchmarks, explicit costs, and immutable schema-v2 epochs. A bounded policy
audit counts each attributed recommendation once and separately reports signal
ingress blocks and committee non-selection; it is not alpha validation.
Production learning is disabled. Promotion output is advisory and requires
Pedro's manual review against precommitted 30/60/90-session gates and complete
benchmark, cost, and provenance evidence. Covered-call execution remains
inactive until authoritative premium, assignment, expiry, and contract-mark
accounting exists. The scenarios share source
fetches and horizon screens, then apply different sizing and eligibility rules;
they are not independent statistical experiments. Equity shorts are active only
where the size/horizon policy allows them.

Generations are frozen code snapshots with isolated state directories. A branch,
PR, clean-ledger readiness run, or timer file in this repository is not a live
generation and is not a production deployment.
