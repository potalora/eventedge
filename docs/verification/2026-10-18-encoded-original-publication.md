# Encoded-original publication content check

The unchanged stabilization candidate `7813e2e` could publish an encoded-original
lease after a one-shot same-size rewrite following its initial output readback.
On the observed Linux filesystem, the rewrite sometimes retained both mtime and
ctime, so the final metadata comparison accepted bytes that no longer matched
the lease hash. Five isolated runs of the original regression produced four
failures and one pass. An instrumented failure confirmed equal complete metadata
identities, a mismatched lease/file SHA-256 and an invalid gzip header. The source
and test were unchanged by the Finnhub and NOAA diagnostic repairs. Neither file
exists on the older `origin/main` base `5611ad8`.

Publication now repeats the bounded full-content check after the raw reader has
closed successfully. Both readbacks use the same pinned-inode verification
helper and compare against the original encoded write digest. The helper reads
at most 64 KiB per operation, checks the existing inherited/owner deadline before
and after each read, and preserves directory, inode, owner, mode, size and link
checks. It neither creates another payload nor reserves extra physical bytes.
The additional pass consumes the existing deadline; no deadline or cap increases.

The read descriptor remains in the existing owner ledger while verification runs.
Reads run outside the owner lock, so concurrent close/cancellation can proceed.
Confirmed and uncertain close errors retain the existing failure, cleanup and
byte-accounting behavior; an uncertain descriptor remains charged until explicit
cleanup confirms closure. A failed verification never publishes a lease.

This is a cooperative owner/capture-lock contract. The caller holds its capture
lock throughout retention, and cooperating writers must honor that ownership.
The late check catches completed corruption between the earlier readback and the
final publication verification. It does not establish atomic filesystem
immutability or protection from arbitrary same-UID writes after the final
verified read. The downstream original store independently checks canonical gzip
framing plus stored and decoded hashes before exposing original bytes.

The strengthened regression performs the actual EOF-triggered rewrite while
coalescing only timestamp identity fields, asserts that the mutation ran and that
the effective metadata identity stayed equal, and still requires rejection.
Another regression corrupts bytes during raw-reader close. Final-read failure,
deadline expiry, confirmed/uncertain close failures and concurrent owner close
also require failure without a published lease. These seven cases failed before
the repair; the existing healthy roundtrips remain covered.

Offline validation ran with Python 3.11.16 in a fresh candidate archive, a private
pytest base directory outside the source tree, and a network namespace:

```bash
sudo -n unshare -n /home/pedro/Projects/eventedge/.venv/bin/python -B \
  -m pytest -q -p no:cacheprovider --basetemp /var/tmp/PRIVATE_BASE \
  tests/test_filing_encoded_spool.py tests/test_filing_spool.py \
  tests/test_filing_spool_dispatch.py tests/test_filing_original_store.py \
  tests/test_filing_acquisition.py tests/test_filing_acquisition_review.py \
  tests/test_filing_acquisition_wiring.py
```

The focused suite passed 191 tests. This evidence does not establish live source
availability, full native acceptance, strategy performance or deployment.
