# Real workflow coverage follow-up

The existing campaign passed one real-source/live-model workflow. Component
faults and accepted-state replay are separate diagnostic observations. On November
1 the user clarified that testing must establish operation with real sources and
real data. The active completion plan follows that requirement:

- Finish the ongoing isolated native run with real providers, real source data,
  the accepted runtime/profile and configured live models. Preserve natural
  provider/model failures and verify the native sixteen-book report independently.
- Keep offline CI as code/regression evidence. Preserve every existing test while
  measuring improvements on the existing runners without sharding.
- Report ordinary sessions, actual timer/observer behavior, continuity and fills
  only after they are observed in real operation. Natural empty data and zero
  fills are valid but leave unexecuted paths unqualified.
- Use the separate ordinary-session command with real-calendar admission, one
  durable attempt per date, exact next-session continuity and independently bound
  reports. Its observations stay separate from the prospective campaign; failed
  or skipped session sequences do not qualify.
- Stop the planned cold baseline/fault campaigns and the additional capture
  intended only to validate replay. Keep existing artifacts and failed attempts.
  Move replay/recorder/mutation limitations out of live acceptance gaps into
  explicitly separate diagnostic coverage.

The following diagnostic machinery was implemented before this clarification.
It remains available for investigation and is not an acceptance requirement:

- Preserve a complete pristine campaign outside its working directory. Bind the
  contract, all file bytes and modes, coherent SQLite contents and runtime links.
  Restore a fresh private copy for each scenario at the original absolute path.
- Reconstruct recorded curl responses with the installed SDK. Reject response
  forms that the tape cannot faithfully represent. Keep exact phase, request and
  occurrence identity and sticky failures even if application code catches them.
- Run the actual prospective controller, shell preflights, daily worker and
  independent report builder in an offline OS boundary. Use the real candidate,
  frozen profile and dependencies. No model auth, production state or network is
  available. Keep original evidence read-only and record the separate tool overlay.
- Run a baseline before attributing counterfactual results to mutations. Faults
  change one identified recorded response. Missing downstream requests are blocked
  coverage, not successful fault handling; never reuse an answer for a new prompt.
- Preserve the first blocked cold attempt. New captures retain sanitized redirect
  metadata and genuine source/model clock inputs. Replay the clock trace only in
  the offline lane, with exact actor/call-site/context identity; live calls return
  unchanged native values and runtime deadlines stay native. A missing historical
  header or clock value is not reconstructed. Validate the hook with actual
  candidate/SDK subprocesses before a fresh live-source/model capture.

Implementation ownership is split between transport replay, baseline preservation,
the native workflow worker and its external supervisor. Verification includes
corrupt bodies/state, wrong identities, unsupported transports, swallowed errors,
private restoration, actual native replay and independent review. CI improvements
are a separate branch based on the application candidate; neither branch deploys
or merges anything.
