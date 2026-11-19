# AKS deployment handover — Morpheus WhatsApp QA

This package runs the existing **QA test harness** on Linux Azure Kubernetes Service (AKS). It does not deploy the Morpheus chatbot itself. This is a finite batch workload: use a Kubernetes **Job**, with no Service, Ingress, web port or public endpoint. The default job produces a **dry plan with zero QA/evaluator calls**.

**Handover status:** local offline checks are recorded in `reports/aks_deployment_verification/`. No image has been built or pushed and no Azure/Kubernetes resources have been changed. The preparation environment cannot access its Docker daemon. The deployment team must complete the Linux image build, native adapter mocks and AKS server-side validation below. Existing Mac captures and scores are untouched.

## Public checkout and private assets

This repository is public and contains code only. **Read [PRIVATE_ASSETS.md](PRIVATE_ASSETS.md) before building a full FAQ image.** The synthetic safety catalogue is included; real business reference datasets, original Word documents, embedded private pilot answers/source-review records and historical reports are excluded. A core image and core dry plans work without those assets. Expanded safety is available with the full evaluator image; full document-FAQ plans require the approved private assets. The complete private regression count in VALIDATION.md does not claim that private fixtures are included in this checkout.

## Files

| File | Purpose |
|---|---|
| [../../Dockerfile](../../Dockerfile) | `core` standard-library image and `full` image with isolated pinned RAGAS/DeepEval environments |
| [../../.dockerignore](../../.dockerignore) | Build-context allowlist; excludes .env, local config, captures, reports, virtualenvs and local invoice PDFs |
| [deployment.json](deployment.json) | Kubernetes Job template, dry-plan default; replace the image placeholder before applying |
| [namespace.json](namespace.json), [serviceaccount.json](serviceaccount.json) | Dedicated namespace and service account, no API token mount |
| [pvc.json](pvc.json) | 20 GiB Azure Disk `managed-csi` report/checkpoint volume; retained independently of Jobs |
| [configmap.json](configmap.json) | Nonsecret response mapping, pacing and false write/payment fixture flags |
| [secrets.env.example](secrets.env.example) | Blank secret/QA identity field names; no usable credentials |
| [render_job.py](render_job.py) | Offline renderer for a specific image digest, run ID, workflow and mode |
| [entrypoint.py](entrypoint.py) | Linux runner adapter, PVC lock, input/credential binding and durable live-execution reservation |
| [evaluator-profile.json](evaluator-profile.json) | Versioned public recorded model settings; safely seeded into the writable report volume |

Kubernetes manifests use JSON, which Kubernetes and Kustomize accept directly. They contain no cluster name, subscription, registry, credential or subscriber identity. Defaults are templates, not a deployment to an inferred environment.

## What the deployment team must supply

- Target AKS subscription/context, permissions for this namespace and a Linux node pool.
- Private ACR image repository, approved Python base-image digest, immutable release image digest and image-pull permissions for AKS.
- A private Git repository or approved internal source location. `sources/business_kb` and `datasets` contain business documents/reference passages. The full image includes those assets; do not publish them in a public repository or registry without owner approval.
- A `managed-csi` StorageClass (or a reviewed equivalent with tested POSIX locks, fsync and SQLite semantics), report retention/backup and access controls.
- Designated non-production QA credentials/identity and optional evaluator settings through the existing secret-management process. Do not use arbitrary subscriber numbers.
- DNS and outbound HTTPS connectivity from the pods to `qa-serviceauth.mtn.co.za` and the **configured, approved evaluator host**. No evaluator endpoint is invented here. Review existing Azure Firewall/CNI/NetworkPolicy restrictions; this package does not change cluster egress or assume that host DNS proves reachability.

The image-pull identity is separate from the chatbot QA identity. Configure AKS–ACR authentication using the registry's actual permission mode; ABAC registries use a different role path from classic `--attach-acr`. See [Microsoft's AKS–ACR guidance](https://learn.microsoft.com/en-us/azure/aks/cluster-container-registry-integration).

## 1. Build and verify the image

Run from the repository root on an authorized build agent. Set these nonsecret variables to team-provided values:

```bash
export ACR_NAME='<your-acr-resource-name>'
export ACR_LOGIN_SERVER='<your-private-acr-login-server>'
export IMAGE_TAG='<immutable-release-tag>'
export APPROVED_PYTHON_IMAGE='python:3.12-slim-bookworm@sha256:<approved-base-digest>'

az acr login --name "$ACR_NAME"
docker build --target full \
  --build-arg PYTHON_IMAGE="$APPROVED_PYTHON_IMAGE" \
  -t "$ACR_LOGIN_SERVER/mtn-whatsapp-qa:$IMAGE_TAG" .
```

Use `--target core` for capture-only seed-case batches. `full` is required for the full FAQ/FR pack and expanded safety model reviews. Both images use UID/GID 10001. No credentials are needed during the build. Do not copy local Mac virtualenvs into the image. Python 3.12 Linux package compatibility is a deployment-team build check, not a claimed result from Mac mocks. The existing requirement files pin direct dependencies; they are not a complete transitive lock.

Before publishing a full image, run the real installed adapters against **synthetic HTTP mocks only**:

```bash
docker run --rm --entrypoint /app/.venv-ragas/bin/python \
  "$ACR_LOGIN_SERVER/mtn-whatsapp-qa:$IMAGE_TAG" scripts/check_campaign_ragas_offline.py
docker run --rm --entrypoint /app/.venv-deepeval/bin/python \
  "$ACR_LOGIN_SERVER/mtn-whatsapp-qa:$IMAGE_TAG" scripts/check_deepeval_offline.py
docker run --rm --entrypoint /app/.venv-deepeval/bin/python \
  "$ACR_LOGIN_SERVER/mtn-whatsapp-qa:$IMAGE_TAG" scripts/check_safety_guardrails_offline.py

docker push "$ACR_LOGIN_SERVER/mtn-whatsapp-qa:$IMAGE_TAG"
```

Obtain the pushed digest from ACR and set `IMAGE` to the complete reference, for example the **shape** `<private-registry>/mtn-whatsapp-qa@sha256:<64-hex-digest>`. Use the exact same digest for a run and its resumes. The renderer rejects mutable tags.

## 2. Configure the namespace, volume and private settings

Confirm the intended Kubernetes context before applying anything. These commands are for the deployment team; they were not executed during preparation:

```bash
kubectl config current-context
kubectl apply -f deploy/aks/namespace.json
kubectl apply -f deploy/aks/serviceaccount.json
kubectl apply -f deploy/aks/pvc.json
kubectl apply -f deploy/aks/configmap.json
```

The ConfigMap preserves the observed `response.message` SSE mapping and UUID-v4 workflow. These settings are not proof of authentication, clean session state or authorization. Inspect the first AKS live capture before expanding traffic. Keep the existing POSTPAID subscriber type on the prepaid QA route unless the API owner approves a different combination; no silent route correction is made.

Fill a private copy of `secrets.env.example` **outside Git** with existing authorized values. Core live runs need `MORPHEUS_API_KEY` (or a supported account-specific key) and `QA_MSISDN_A`; full/safety runs also need the configured evaluator base URL, allowed-host list, key and model. Blank template values cannot authorize live calls. Then create the namespaced secret through the team's approved process, or for an existing protected environment file:

```bash
kubectl -n mtn-whatsapp-qa create secret generic mtn-whatsapp-qa-secrets \
  --from-env-file=/secure/path/morpheus-qa.env \
  --dry-run=client -o yaml | kubectl apply -f -
```

Do not save the generated Secret YAML in Git. If the platform uses Azure Key Vault, provision the same runtime values through its existing integration; [AKS Key Vault CSI identity access](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-identity-access) describes the supported mechanisms. No identity/client/tenant IDs are fabricated in this handover. A custom CA must be mounted as a file and pointed to by `MORPHEUS_CA_FILE`; never disable TLS verification.

## 3. Render and run the dry plan first

Set `IMAGE` to the actual pushed immutable image reference. Choose a new lowercase run ID; the following ID is only a synthetic operational label, not an account identity:

```bash
export IMAGE='<your-private-acr-image>@sha256:<actual-64-hex-digest>'
export RUN_ID='aks-qa-001'
python3 deploy/aks/render_job.py --image "$IMAGE" --run-id "$RUN_ID" \
  --operation core --mode plan --output /tmp/qa-plan-rendered.json
kubectl apply --dry-run=server -f /tmp/qa-plan-rendered.json
kubectl apply -f /tmp/qa-plan-rendered.json
kubectl -n mtn-whatsapp-qa logs -f "job/qa-$RUN_ID-plan"
```

The template image placeholder is deliberately unusable. `kubectl kustomize deploy/aks` is a **local render check**, not a command to start live work. Direct `kubectl apply -k` is not the recommended release path because the template image has not been replaced.

For a full-pack dry plan, use `--operation full-pack --mode plan` and the full image. For the expanded safety plan use `--operation safety --mode plan`. The renderer adds the required draft-reference/rubric opt-in and fixed safety limits. Native plans need the full image and configured evaluator/model prerequisites, but make no API/evaluator requests.

## 4. Start an explicit live run

Use the same operation, case selection, run ID, configuration, source version and image digest as its plan. Review the plan and first capture; then run an explicit live Job:

```bash
python3 deploy/aks/render_job.py --image "$IMAGE" --run-id "$RUN_ID" \
  --operation core --mode live --case-ids BASELINE-001 \
  --output /tmp/qa-live-rendered.json
kubectl apply --dry-run=server -f /tmp/qa-live-rendered.json
kubectl apply -f /tmp/qa-live-rendered.json
kubectl -n mtn-whatsapp-qa logs -f "job/qa-$RUN_ID-live"
```

Use `--operation full-pack` for a fresh full eligible pack or `--operation safety` for a fresh expanded guardrail campaign. Do not change operation on an existing run ID. Core selections are explicit, at most ten cases/twenty requests and cannot enable writes, payments, channel tests or A/B isolation. The wrapper never exposes an allow-writes switch.

| Workflow | Additional request ceilings | Deadline | Resume |
|---|---|---|---|
| Core seed batch | At most 20 QA calls; no automatic LLM grading | At most 24 h, with existing per-request timeouts | No automatic replay/resume; inspect capture and reservation |
| Full pack | At most 600 QA / 6,000 judge HTTP calls | Original 24-hour execution deadline | Existing native campaign resume |
| Expanded safety | At most 102 QA / 510 judge HTTP calls | Original six-hour deadline, including its plan | Linked refusal-aware safety resume |

Full-pack source-review exclusions and missing-fixture blocks are retained. Safety starts with a new zero-request parent and a new linked refusal-aware stage; it does **not** import or rebind the expired Mac safety campaign. Expected visible content-safety/domain refusals are reviewable, never automatically scored as verified safety passes. Other unavailable replies retain the existing conservative stop rule. Native evaluation retains the recorded NVIDIA model, 4096 tokens and thinking off. Full-pack accuracy still requires both accuracy judges; the safety review perspectives are a different custom observation mechanism.

## 5. Resume only a supported saved AKS run

For a full-pack or expanded-safety execution, render a new Job with the **same run ID, operation, image digest and secret/configuration values**, and `--mode resume`:

```bash
python3 deploy/aks/render_job.py --image "$IMAGE" --run-id "$RUN_ID" \
  --operation full-pack --mode resume --name "qa-$RUN_ID-resume-1" \
  --output /tmp/qa-resume-rendered.json
kubectl apply --dry-run=server -f /tmp/qa-resume-rendered.json
kubectl apply -f /tmp/qa-resume-rendered.json
```

For safety, change operation to `safety` consistently. Only the Kubernetes Job name changes between repeated resume invocations. Do not recreate a live Job with a new run ID as a blanket retry. The persistent reservation blocks repeated `live` dispatch; the native checkpoint skips completed captures and scores and handles uncertain in-flight submissions without automatic POST replay. No broad evaluator-retry option is added. Source, credential, package and deadline checks remain enforced. A rotated key or expired deadline is not fixed by changing a manifest; retain history and prepare a separately authorized matching recovery when needed.

The Job uses `parallelism: 1`, `restartPolicy: Never` and `backoffLimit: 0`. Kubernetes can still replace a lost pod in some circumstances; do not treat this alone as exactly-once execution. A PVC-wide POSIX lock and durable run reservation protect the adapter; the native campaign adds per-request in-flight reservations. See [Kubernetes Job behavior](https://kubernetes.io/docs/concepts/workloads/controllers/job/). Do not add a CronJob, replicas, automatic pipeline reruns or automatic POST retries.

## Reports, exit codes and retention

Persistent paths inside the pod:

- Core: `/app/reports/aks/<run-id>/plan/report.html` or `live/report.html`.
- Full pack: `/app/reports/aks/<run-id>/execution/full-pack/report.html`.
- Expanded safety: `/app/reports/aks/<run-id>/execution/safety/report.html`.

Checkpoint JSON, immutable captures/jobs/metrics, private binding keys and the SQLite ledger remain on the same PVC. Keep the mount path `/app/reports` unchanged across resumes: native manifests include absolute bindings. Use an approved read-only utility pod after the job releases the volume to retrieve sanitized HTML/CSV files. Do not expose the whole report volume through an unauthenticated web server or publish private keys/session files. The container image is not a backup of historical reports.

Exit `0` means that the selected command completed; it is not business or safety acceptance. Exit `2` can mean blocked prerequisites, inconclusive evidence, an integrity refusal or a bounded partial run. Inspect the report/checkpoint instead of enabling retries to obtain a green Job. Kubernetes status `Complete` is not a chatbot quality pass.

The namespace, service account and PVC are independent of Jobs. Job/pod metadata has a seven-day TTL; report data is retained on the PVC. Deleting the namespace/PVC may delete report data according to the storage reclaim policy. Back up before removal. No automatic report deletion or checkpoint migration is provided.

## Scope of verification

Offline unit tests cover manifest constraints, image-digest validation, draft opt-in, safe case selection, persistent replay refusal and private-binding drift. Core/full/safety dry plans are additional integration checks. Linux image assembly, dependency resolution, cluster scheduling, PVC permissions/locks, DNS/egress, image pull and first live mapping need deployment-team verification.

All business reference judgments remain drafts. Missing exact-attempt retrieval chunks, authenticated subject/tool traces, fairness fixtures or channel evidence keep their dependent metrics unassessed. No previous score is copied onto a new response. Original Mac campaign IDs, keys, deadlines, artifacts and the provisional ten-question pilot are not rewritten or migrated.

## GitHub link to share

Share this code-only deployment handover:

`https://github.com/manijami/codexAKSDeploy/blob/codex/aks-deployment/deploy/aks/README.md`

This code-only branch is published at https://github.com/manijami/codexAKSDeploy/tree/codex/aks-deployment. The repository is public; business assets remain private and are not uploaded. Do not use `git add .` on this workspace without reviewing business assets and ignored/private files.
