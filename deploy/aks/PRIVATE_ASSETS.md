# Private assets for authorized deployment staff

This GitHub repository is public. Do not upload business documents, frozen answers/passages, subscriber captures, credentials, reports or local invoice PDFs. The synthetic safety catalogue contains authored test prompts only.

Core seed-case dry plans and the core image do not require business documents. The expanded synthetic safety runner needs the full evaluator image and authorized runtime configuration, but not the private FAQ corpus.

Before building a **private full document-FAQ image**, obtain the approved, unchanged inputs through internal storage:

- `sources/business_kb/*.docx`: the original authorized document corpus.
- `datasets/business_faq_v1.json`: the frozen question/reference catalogue with exact source hashes, passages and source-review exclusions. Do not synthesize replacements or remove issue flags.
- `datasets/faq_pilot.json`: only when using the separate pilot.

These paths are gitignored but permitted in the Docker build context. Copy them locally only on an approved build agent, verify the approved asset hashes, build/push solely to the approved private registry, then retain according to the internal data-handling process. Runtime credentials remain Kubernetes/Key Vault inputs, never build inputs.

Offline reference-builder tools optionally require `datasets/faq_pilot_specs.private.json` (the approved private pilot-spec tuple list) and `datasets/faq_source_issues.private.json` (the approved private curated-issue tuple list). Their embedded original private values were removed from public code. Builders fail if those inputs are missing; they do not silently discard exclusions or invent answers. Use already approved frozen datasets for deployment rather than rebuilding historical references.

Private corpus-dependent tests require the same approved assets. The public README lists the standalone core/CLI/AKS tests that run without them. Full native image compatibility, package resolution and real AKS execution must be validated by the deployment team. Historical local package versions/results are not claims of an executed Linux deployment.
