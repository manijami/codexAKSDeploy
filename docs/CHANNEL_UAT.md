# Verify the actual WhatsApp channel

The Python API bypasses the actual WhatsApp transport. Complete these checks on a designated QA handset and test-agent queue before channel sign-off. `UAT-*` scenarios are BLOCKED in the HTTP runner until executed through the relevant channel/infrastructure; they are not silently simulated.

| Test ID | Execute and retain evidence |
|---|---|
| UAT-001 | Send a Meta-signed test webhook via the supported test channel. Verify valid signature acceptance, invalid signature rejection and trusted sender-to-MSISDN binding. Retain sanitized APIM and tool authorization decisions. |
| UAT-002 | Exercise each supported quick reply, list and CTA on WhatsApp mobile. Verify text truncation, selection payloads, ordering and correlation with the same conversation. |
| UAT-003 | Open the sandbox hosted payment page from WhatsApp; test redirect/return, cancellation and expiry. Verify the destination is non-production before sandbox authorization. |
| UAT-004 | Capture webhook ingress, typing event, first visible response and final delivery timestamps. Compute channel P95 with a representative sample and separate FAQ from multi-tool orchestration. |
| UAT-005 | Inspect APIM, application, Search, LLM telemetry and tool logs. Check MSISDN, ID, PUK, ICCID, billing addresses and payment data masking. Retain redacted audit evidence; the local report redactor does not prove backend privacy. |
| UAT-006 | Request a human, create strongly negative sentiment and inject two tool failures using fixtures. Verify a real queue/agent receives the correct context with no cross-subscriber data. |
| UAT-007 | Open the generated invoice link as intended, after expiry, and from an unauthorized test identity. Verify expiration and subject binding server-side. |
| UAT-008 | Exercise configured APIM rate limits and circuit-breaker recovery with the approved fixture. Record 429 behavior, retry guidance and recovery. Do not infer throttling capacity from sequential functional calls. |

Record run ID, requirement, handset/client version, test subscriber token, message/webhook IDs, UTC timestamps, expected/actual behavior, sanitized supporting evidence, evaluator, pass/fail and defects. Do not attach unredacted subscriber or payment screenshots to ordinary reports.

Channel and infrastructure cases can be tracked in the catalogue CSV and signed-off evidence worksheet. The current `review` command re-scores captured API attempts; it does not create an API response for a manual case. Keep channel sign-off as a separate reviewable record, and use `channel_evidence` if you add a backend/channel driver later.
