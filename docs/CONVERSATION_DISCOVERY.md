# Find the supported conversation lifecycle

## Frontend finding after source collection

The collected application bundle establishes the intended text workflow:
`newSession()` clears activeSessionId, `getOrCreateActiveSessionId()` calls
`generateUuid()` when no local session is active, and `sendMessage()` sends
that UUID in conversationId on the first text POST. The generator uses
crypto.randomUUID with a UUID-v4 fallback. There is no separate creation
request in this path. See reports/conversation_discovery/conversation_evidence.md
for exact excerpts, saved-source offsets and original response digest.

Use the new `--frontend-fresh` run option to reproduce this workflow per
invocation. It overrides conversation_mode in memory only, generates a new
UUID v4 per case and retains it within multi-turn cases. Existing settings,
credentials and original ID are preserved. Live transport, budgets, timeouts,
write guards, redaction and no-POST-retry behavior remain in effect.

One verification case is prepared as a dry plan at
reports/fresh_conversation_verification_plan/report.html. Run once from
ordinary Mac Terminal:

```bash
.venv/bin/python run.py run --id BASELINE-001 --frontend-fresh --live --out reports/fresh_conversation_verification
```

This sends one `My balances` request with the existing QA account/subscriber
type and a newly generated ID, without a preliminary chat request. Inspect
HTTP/SSE completion, response.message and
actual_api_response[].frontend_conversation_verification. The latter records
response ID presence and equality as booleans before redaction; actual IDs
remain private. A mismatch or absent header needs investigation rather than
assuming the new ID was retained. These are observations in addition to the
baseline's protocol result, not extra semantic scores or an isolation gate.
fresh_state_isolation stays null even if the response header matches.

Backend acceptance with the current credentials is not verified until this
live request succeeds. Persistence and fresh state require supported history
or backend evidence, and authorization requires APIM/tool traces. Balance
accuracy, grounding and safety remain unassessed. No creation POST is invented
and no local credential/configuration file is changed.

## Earlier findings before frontend collection

Current finding: the local material does not establish how to create fresh
Morpheus conversations. Keep conversation_mode=provided. Do not enable
client_uuid or guess creation/reset routes.

Reviewed evidence:

- START_HERE_MAC.md explicitly says no creation endpoint was supplied.
- docs/ACCOUNTS_AND_FIXTURES.md says ID creation and authentication binding
  are unspecified.
- sources/requirements_extracted.txt describes state and subject binding,
  without an executable creation contract.
- The Postman collection contains only the prepaid text route.
- API-002 returned 422 for a missing conversationId. This does not test
  arbitrary IDs, an empty ID, or a creation endpoint.
- Successful baseline and balance captures have x-conversation-id, but its
  saved value is redacted. They cannot establish issuance versus echo, or
  fresh-session isolation. Their SSE events contain loader and final response
  messages, not a documented creation response.

Next discovery step, from ordinary Mac Terminal in the project directory:

```bash
.venv/bin/python scripts/discover_conversations.py
```

This reads the existing configured QA frontend Referer and at most eight
same-origin script URLs explicitly referenced by its HTML, subject to the
configured request budget. It uses GET only, existing headers and TLS settings,
bounded response sizes/timeouts and sequential spacing. It does not follow
redirects, execute JavaScript, guess API routes, send chat messages, create
conversations or modify .env/config/local.json. It saves redacted source text
and capture metadata under gitignored reports/conversation_discovery. Existing
discovery output is preserved by refusing to overwrite it. Known configured
secrets are redacted; do not publish the collected source files.

Review these sources locally for the actual frontend initialization code,
network calls, storage and ID generation. A JavaScript UUID generator alone
does not prove backend acceptance or isolation. A missing search hit does not
prove that no creation API exists: lazy modules and runtime calls may require
an actual browser network capture or API-owner documentation. If the page
redirects or needs an interactive sign-in, use its normal supported UI and
capture its initialization traffic; do not invent login or creation endpoints.

Implement a reusable creation step only after observing/documenting its exact
method, route, fields, authentication and ID response path. Verify that the
result is a new supported conversation for the designated account, with no
prior context. If client-generated IDs are the supported contract, obtain
explicit confirmation or controlled evidence of that contract and fresh state
before enabling that mode. A successful chat response alone is insufficient.
Creation POSTs must not be automatically retried. Keep issued IDs private and
pass them to each case through process-local configuration; do not overwrite
the original configured ID.

No conversation creator has been implemented because no supported creation
contract has yet been found. Backend quality/safety evidence remains separate
and unassessed.
