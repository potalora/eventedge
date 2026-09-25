# Historical SIP recovery for governed P0 bars

The 2026-09-22 Yahoo daily BRC and ICE bars had opens above their highs. The
existing Yahoo 60-minute reconstruction correctly rejected both because its
last close differed from Yahoo's daily close. Historical Alpaca `sip`/`raw`
`1Day` queries returned one coherent bar for each exact symbol and session.
This is a new P0 data-source behavior (`alpaca-sip-1d-raw-v1`), so it requires
a reviewed release and a new immutable generation. The failed gen_014 session
is not replayed or rewritten.

Only an incoherent Yahoo daily bar with a failed strict hourly recovery is
eligible. The adapter makes one bounded historical request per ticker with
explicit `feed=sip`, `adjustment=raw`, `timeframe=1Day`, and `asof=-`. It
requires close plus 15 minutes, a single exact-session bar, exact response
symbol, no pagination, coherent positive OHLC, and no redirect. IEX, adjusted
data, missing credentials, provider errors, and malformed responses leave the
original P0 failure in place. Yahoo's hourly contract stays unchanged.

An accepted alternate is stored as a versioned governed recovery record with
the original Yahoo daily and hourly failure, full SIP observation, cohort set,
and canonical digest. Every cohort uses the same bound bar. Replay reads the
record without network access; changed, missing, or conflicting evidence
fails closed. Preflight evaluates the same resolver without writing generation
state. Accepted recovery remains a degraded, alertable run under the existing
policy, rather than a clean session for promotion.

Release verification includes the observed BRC and ICE provider payloads,
malformed/ambiguous/unauthorized provider fixtures, immutable-store and
offline-replay tests, preflight and execution binding checks, and the full
non-live suite. After the new generation starts, the continuity gate must see
complete, clean sessions in the actual frozen worktree and epoch before the
older generation can be retired. A passing simulated suite alone does not
establish live continuity or trading performance.
