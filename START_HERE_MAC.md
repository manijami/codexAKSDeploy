# Set up Morpheus QA on your Mac

This guide creates an independent local project for testing the MTN QA chatbot. Run the commands in your Mac Terminal. You do not need to put this folder inside the employee chatbot repository or connect Claude Code or OmniRoute. Codex runs in this folder and helps operate and extend these scripts.

## 1 Download and extract

Download `mtn-whatsapp-qa.zip`. Run:

```bash
mkdir -p ~/Projects
unzip ~/Downloads/mtn-whatsapp-qa.zip -d ~/Projects
cd ~/Projects/mtn-whatsapp-qa
```

If your browser added a suffix to the ZIP filename, adjust the `unzip` command. If you extracted it using Finder, move the resulting `mtn-whatsapp-qa` folder into `~/Projects` and use the same `cd` command. Start in a new folder rather than overwriting an existing working copy.

## 2 Verify prerequisites

```bash
python3 --version
codex --version
```

Use Python 3.10 or newer. If Python is missing or too old, install the macOS installer from https://www.python.org/downloads/macos/ and reopen Terminal. This project does not depend on Homebrew.

If Codex is already installed, keep using it. Otherwise the official OpenAI CLI instructions currently give this macOS installer:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

Open a new Terminal afterward if `codex` is not yet on PATH. See https://learn.chatgpt.com/docs/codex/cli for current installation and authentication options, verified 14 November 2026. No Codex browser extension is required for these API tests.

## 3 Initialize and verify locally

```bash
cd ~/Projects/mtn-whatsapp-qa
bash scripts/bootstrap_mac.sh
source .venv/bin/activate
```

The bootstrap creates a Python virtual environment, runs the offline harness tests and creates a synthetic demo report. It installs no third-party Python packages and sends no API requests. Open:

```bash
open examples/local_demo/report.html
```

Synthetic API checks can show PASS; the baseline remains INCONCLUSIVE until the SSE response mapping is confirmed. This demonstrates reporting and does not assess MTN.

## 4 Add the supplied request locally

Choose one method.

**A — Interactive configuration**

```bash
python scripts/configure.py
```

Enter the `key` query parameter from your supplied request, its Cookie header value if needed, the designated QA MSISDN, and its valid `conversationId`. These inputs are hidden while typed. Press Enter for the supplied `POSTPAID` subscriber type.

**B — Import the complete copied Postman command**

1. Save the exact command from this chat in a plain text file named `request.private.txt` inside this project. This filename is gitignored.
2. Run:

```bash
python scripts/import_request.py request.private.txt
```

The importer extracts the URL key, headers and four-field JSON body into `.env`. It parses the command as text and never executes it. Your capture contains duplicate BIG-IP and TS cookies; the importer keeps the last value for each cookie name. A fresh browser/Postman session may be necessary if those captured cookies have expired. If import parsing fails, use method A.

Both methods create `.env` with owner-only file permissions. The download intentionally contains no live key, cookies or subscriber identity. Do not use `source .env`: values such as cookies are literal data read by Python.

## 5 Check configuration

```bash
python run.py doctor
```

The doctor prints whether settings exist without printing their values. For the first live balance request you need the QA MSISDN and valid conversation ID. Confirm with the API team whether `POSTPAID` is supported on the `/prepaid/text` route; this project preserves your successful request contract rather than changing it.

There is no documented conversation-creation endpoint in the supplied request. By default the harness consumes provided conversation IDs. A single ID is enough for the first baseline. Obtain fresh IDs for independent cases and supply them in `QA_CONVERSATION_IDS_A_JSON=["id1","id2"]`. Within a multi-turn case, the same session is retained. IDs are not persisted as consumed across separate CLI runs: supply fresh IDs to avoid prior-context contamination.

Only after the API owner confirms arbitrary client UUIDs create new isolated conversations, create `config/local.json` with:

```json
{"conversation_mode":"client_uuid"}
```

The captured conversation may already have context. The baseline is a connectivity/mapping check, not a fresh-session quality benchmark.

## 6 Launch Codex in this project

```bash
codex
```

Follow the sign-in screen if necessary. Paste the full prompt in `docs/CODEX_FIRST_PROMPT.md`. The included `AGENTS.md` tells Codex how to operate this project and interpret results. A ChatGPT web project and a local folder do not automatically synchronize; these files are the handover.

You can also run all Python commands yourself from Terminal. Codex is a collaborator, not a runtime dependency of the test harness.

## 7 Run the first live baseline

Use your MTN network or VPN if QA access requires it:

```bash
python run.py run --id BASELINE-001 --live --out reports/first_baseline
open reports/first_baseline/report.html
```

The request body uses the supplied contract:

```json
{
  "conversationId": "YOUR_VALID_QA_CONVERSATION_ID",
  "msisdn": "YOUR_DESIGNATED_QA_MSISDN",
  "subscriberType": "POSTPAID",
  "message": "My balances"
}
```

The actual query key and cookies come from `.env`. The request URL is the same QA route you supplied. Reports redact those values. The command can exit `2` and still have produced a useful report: INCONCLUSIVE means some required evidence/configuration is not yet available.

Inspect `actual_api_response.events` and `raw_body` in the JSON report. Ask Codex to confirm which event/JSON path contains the final user-visible answer. The defaults are candidate paths only. For example, **if the actual stream confirms** `data: {"text":"...final answer..."}` snapshots, use:

```json
{
  "response_mapping_confirmed": true,
  "snapshot_paths": ["text"],
  "delta_paths": []
}
```

Save that object in `config/local.json`, merging any conversation setting you already added. If the stream emits deltas or different keys, configure the observed paths instead. Set `require_done_event` only after confirming the completion contract. Do not mark the mapping confirmed merely to make a test pass.

## 8 Run checks in small batches

```bash
python run.py run --id API-001,API-002 --live --out reports/schema
python run.py run --id FR-01-V01 --live --out reports/balance
python run.py run --suite all --out reports/plan
```

The final command is a dry plan. It sends nothing. Inspect the 220-case catalogue in `docs/test_catalogue.csv` before selecting larger batches. Live runs are sequential, limited to 20 attempted requests by default and spaced at least 0.5 seconds apart. Edit local limits only to match your QA capacity. There are no automatic POST retries.

## 9 Add evidence and evaluate

```bash
python scripts/make_evidence.py \
  --results reports/balance/results.json \
  --out evidence/balance.json
```

Add approved answers, retrieved chunks in rank order, tool inputs/outputs and authorization decisions to the generated worksheet. Include evaluator provenance and annotations as explained in `docs/EVALUATION.md`. Then:

```bash
python run.py review \
  --results reports/balance/results.json \
  --evidence evidence/balance.json
open reports/balance/reviewed/report.html
```

Review does not re-send API requests. Each annotation is bound to the exact attempt ID and response digest. Original captures remain available. Optional automated RAG judging and framework score import are described in `docs/FRAMEWORK_INTEGRATION.md`.

## 10 Extend to the remaining requirements

- Configure independent A/B authenticated QA accounts before isolation testing; see `docs/ACCOUNTS_AND_FIXTURES.md`.
- Add backend failure fixtures for resilience and a resettable sandbox for transactions. Without them, the relevant cases show BLOCKED.
- Use `docs/PERFORMANCE.md` for direct API P95 analysis. Full WhatsApp latency and throughput require separately scoped channel/load testing.
- Use `docs/CHANNEL_UAT.md` for signatures, messages, handoff and payment redirects on a real test device.
- Add human-approved golden answers and versioned source IDs to the seed cases/evidence before release sign-off.

## Troubleshooting

| Observation | Next action |
|---|---|
| DNS lookup failure / no HTTP response | Connect to the required MTN network or VPN; check the QA hostname locally. This is BLOCKED, not a measured chatbot defect. |
| Certificate verification failure | Use your organization's trusted CA bundle with `MORPHEUS_CA_FILE=/absolute/path/ca.pem`; keep TLS verification enabled. |
| 401 / 403 / HTML sign-in page | Refresh the working QA key/session/cookies and compare the browser request with Postman. The script does not auto-login. |
| 422 | Compare the validation details with the captured four-field JSON; confirm account type and ID requirements. |
| HTTP 200 with no answer extracted | Inspect SSE events and fix mapping. HTTP 200 alone does not prove business success. |
| BLOCKED after one case | Supply more valid conversation IDs or confirm UUID creation support; inspect fixture prerequisites. |
| INCONCLUSIVE with null scores | Add the specific missing sources, traces, evaluator annotations or mapping confirmation. |
| Stream times out | Confirm expected duration and completion events before adjusting `read_timeout_seconds`/`timeout_seconds`. Partial responses do not pass. |
| `python` not found after opening a new Terminal | `cd` to this folder and run `source .venv/bin/activate`. |
