# Connect semantic evaluators

For the new ten-question local-business-document pilot, see
[FAQ_PILOT.md](FAQ_PILOT.md). It includes an optional native RAGAS 0.4.3 script
with separate answer-accuracy and trace-dependent retrieval evaluation. Its SDK
and model calls have not been executed here. The original bridge and custom
judge below remain available and retain their distinct metric names.

The core project runs without evaluator SDKs or paid LLM access. It implements annotated-evidence scoring plus a bounded optional NVIDIA-compatible RAG judge client. Native RAGAS, DeepEval, Garak and Azure AI Evaluation SDK executions are not installed or configured in this package. Their outputs can be imported through the working normalized-score interface below. Do not label the local rubric judge as RAGAS or Azure Groundedness.

## Option 1 Human or external annotations

Use `make_evidence.py`, add actual sources and assessments, then run `run.py review`. This works entirely offline and implements the source-supported label calculations in EVALUATION.md. Missing traces remain INCONCLUSIVE.

## Option 2 Approved NVIDIA-compatible judge

The optional `scripts/judge_rag.py` calls the configured `/v1/chat/completions` endpoint. NVIDIA NIM documents this API shape. There is no default public endpoint or model. Configure an approved judge in `.env`:

```text
MORPHEUS_EVAL_BASE_URL=https://YOUR_APPROVED_JUDGE_HOST/v1
MORPHEUS_EVAL_ALLOWED_HOSTS_JSON=["YOUR_APPROVED_JUDGE_HOST"]
MORPHEUS_EVAL_MODEL=YOUR_APPROVED_MODEL_ID
MORPHEUS_EVAL_API_KEY=
```

Add actual retrieved chunks, tool outputs and the human-approved reference to `evidence/balance.json` first. The judge receives redacted question/answer/source material. Review the data handling requirements of that endpoint before enabling it; no public service is automatically chosen.

```bash
python scripts/judge_rag.py \
  --results reports/balance/results.json \
  --evidence evidence/balance.json \
  --out evidence/balance.judged.json
```

Without `--execute` this is a dry plan. To execute up to five supported evidence attempts:

```bash
python scripts/judge_rag.py \
  --results reports/balance/results.json \
  --evidence evidence/balance.json \
  --out evidence/balance.judged.json \
  --limit 5 --execute
python run.py review \
  --results reports/balance/results.json \
  --evidence evidence/balance.judged.json
```

This is a custom claim-labeling and answer-relevancy rubric. The client restricts accepted fields, requires matching digests, ignores unsupported security grades, limits request count and applies TLS/host checks. It does not provide cosine/reranker Retrieval Relevancy and does not certify security controls. Model-based labels require calibration and human review, especially for critical fabrication or adversarial content. The live judge integration was not exercised during packaging because an approved endpoint/model was not provided.

## Option 3 Import framework scores

The following bridge is implemented and tested:

```bash
python scripts/import_metric_scores.py \
  --evidence evidence/balance.json \
  --scores external.scores.json \
  --out evidence/balance.merged.json
python run.py review \
  --results reports/balance/results.json \
  --evidence evidence/balance.merged.json
```

`external.scores.json` is an array of records with `attempt_id`, `response_sha256`, `metric`, numeric `score` in [0,1], `method`, `reason`, `evaluator` and `evaluated_at`. Use the exact IDs/digests from the captured run. Every original context/tool/authorization dependency still applies after import. Different frameworks' scores are not automatically interchangeable; record model, framework/version, normalization and rubric.

| Source | Inputs to evaluate | Output handling |
|---|---|---|
| RAGAS | User question, final answer, retrieved chunks and approved reference | Export relevant 0..1 metrics to the bridge; preserve metric version and judge/embedding model. |
| DeepEval | `input`, `actual_output`, `retrieval_context`, plus expected output for reference-based metrics | Export metric score/reason; do not replace authoritative tool checks with faithfulness. |
| Garak | A controlled generator adapter that targets this authenticated SSE API and preserves sessions | Export observed attack outcomes tied to actual captures; do not run a generic public-model preset against subscriber APIs. The supplied safety cases are local seed attacks, not a Garak run. |
| Azure AI Evaluation / Content Safety | Sanitized query/response/context plus approved Azure endpoint, deployment and credentials | Normalize only using the selected metric's documented scale. Preserve native scores and conversion in provenance; never assume a 1..5 score is already 0..1. |

Native SDK wrappers and deployed backend trace connectors need environment-specific configuration and version pinning. They are extension points rather than silently simulated integrations. Install them in a separate evaluation environment if their dependency versions conflict with the transport project. No dependency installer in this package sends MTN data to an external evaluator.

## Primary references checked for this handover

- OpenAI CLI setup: https://learn.chatgpt.com/docs/codex/cli
- NVIDIA NIM API: https://docs.nvidia.com/nim/large-language-models/latest/api-reference.html
- RAGAS Faithfulness: https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/faithfulness/
- RAGAS Context Precision: https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/
- DeepEval Faithfulness: https://deepeval.com/docs/metrics-faithfulness

These references explain method/API shapes, not evidence that any external evaluation ran against MTN.
