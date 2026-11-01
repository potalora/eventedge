# Real workflow coverage follow-up

The existing campaign passed the real source/model workflow, component faults and
accepted-state replay. This follow-up adds the missing machinery without treating
those observations as cold replay or future-session evidence.

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
- Add a separate ordinary-session command with real-calendar admission, one
  durable attempt per date, exact next-session continuity and independently bound
  reports. Keep its observations separate from the prospective campaign and
  reject failed or skipped session sequences.
- Keep ordinary timer dispatch, naturally aligned observer delivery, later-session
  continuity and actual fills as separately observed gates. A future schedule or
  empty fill set cannot certify a nonzero execution path.

Implementation ownership is split between transport replay, baseline preservation,
the native workflow worker and its external supervisor. Verification includes
corrupt bodies/state, wrong identities, unsupported transports, swallowed errors,
private restoration, actual native replay and independent review. CI improvements
are a separate branch based on the application candidate; neither branch deploys
or merges anything.
