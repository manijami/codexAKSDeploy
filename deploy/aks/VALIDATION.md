# Preparation validation

Prepared on 19 November 2026 in the local WhatsApp QA project.

- Full offline regression: 136 tests passed, including seven AKS adapter/manifest tests.
- Core dry-plan CLI integration: passed; no live requests.
- Full-pack dry-plan integration: 654 planned cases; zero QA/evaluator requests.
- Fresh expanded-safety dry-plan integration: 114 cases, zero-request parent and linked refusal-aware stage; zero QA/evaluator requests.
- Kubernetes JSON parsing and local `kubectl kustomize deploy/aks`: passed.
- Immutable image-digest renderer: passed offline with a clearly synthetic reference, not a real registry push.
- Historical full-pack and safety source fingerprints: unchanged. Credentials and historical captures/results were not edited.

Not executed: Docker build/push, Linux dependency installation/native-adapter execution inside an image, AKS server-side validation, cluster deployment or live QA calls. The local Docker daemon socket was inaccessible. The deployment team must perform the README's build, mock and cluster checks; no Azure resource or repository destination has been assumed.

Local detailed logs are in gitignored `reports/aks_deployment_verification/`. They are not included in the image or handover archive. The original preparation did not publish a repository. The subsequent public code-only branch is codex/aks-deployment in manijami/codexAKSDeploy; private assets and historical artifacts are excluded.

Public-export note: the 136-test run used the original authorized local workspace with private assets. The public checkout intentionally excludes those assets; the separately documented public core/CLI/AKS tests were verified from the sanitized copy. See PRIVATE_ASSETS.md.
