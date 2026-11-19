# WhatsApp QA harness — AKS deployment

Python QA harness, bounded test runners and Azure Kubernetes Service deployment templates. This deploys the test harness, not the chatbot service.

**Deployment team: start with [the AKS handover](deploy/aks/README.md).**

## Included

- Standard-library HTTP/SSE capture, seed functional/safety scenarios and evidence-aware reporting.
- Optional pinned native RAGAS/DeepEval adapters and a synthetic expanded guardrail catalogue.
- Non-root Docker targets, persistent Kubernetes Jobs, dry-plan defaults, request ceilings and supported resume controls.

## Public repository boundary

Credentials, subscriber identifiers, captured conversations, historical results, invoices, business documents and frozen business reference datasets are excluded. Approved private assets must be supplied separately before the full FAQ workflow or private full-suite regression can run. See [PRIVATE_ASSETS.md](deploy/aks/PRIVATE_ASSETS.md). No example is a real subscriber identity.

## Offline checks

```bash
python3 -m unittest discover -s tests -p test_harness.py -v
python3 -m unittest discover -s tests -p test_cli.py -v
python3 -m unittest discover -s tests -p test_aks_deployment.py -v
kubectl kustomize deploy/aks
```

Core dry plan (no API calls):

```bash
python3 run.py run --id BASELINE-001 --out reports/core-plan
```

Live execution requires configured designated QA credentials. Never equate HTTP 200, polite refusals or model flags with verified grounding, authorization or safety. Unsupported metrics stay unassessed and draft judgments remain provisional.

No AKS resources are deployed by cloning this repository. Image build, private registry publication, cluster configuration and an explicit live Job are deployment-team actions described in the handover.
