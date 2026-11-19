# Use the Postman collection

Import `postman/Morpheus_WhatsApp_QA.postman_collection.json` and `postman/Morpheus_QA.postman_environment.json`. Select the environment and populate local values from your working QA request. The collection and environment contain no live credentials.

Set `base_url`, `key_A`, `cookie_header_A`, `msisdn_A`, `conversation_id_A` and `subscriber_type_A=POSTPAID`. An optional disabled Authorization header is supplied per actor; enable it only if your QA request uses that authentication. For account B tests use B's independent values and verify the real identity relationship.

Run **baseline / BASELINE-001 turn 1** first. It sends:

```http
POST {{base_url}}/morpheus-v5/api/prepaid/text?platform=whatsapp&key={{key_A}}
Accept: text/event-stream
Content-Type: application/json
```

Body fields are conversationId, msisdn, subscriberType and message. The Referer preserves the supplied empty `?key=` value; the actual gateway key is in the request query. The importer for the Python harness retains the complete supplied Cookie header, reduces duplicate names to the last value, and does not execute the copied command. Postman environment values must be entered separately; importing the collection does not read your `.env`.

Collection tests verify HTTP status and SSE content type. They do not calculate RAG, safety or authorization scores. The Postman response panel may wait for stream closure; use the Python capture to measure progressive event timing and retain partial streams on failure.

Do not run all folders automatically with one shared conversation ID: that contaminates independent tests. Use fresh IDs and the Python harness for repeatable multi-turn isolation. Write, payment, injected-failure and manual/channel cases are excluded from the Postman auto-run catalogue; use the guarded Python catalogue with real fixtures instead. Complete transactional UAT is not implemented by a textual prompt alone.
