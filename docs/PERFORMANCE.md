# Measure direct API response timings

The source requires P95 end-to-end FAQ latency no greater than 3500 ms, multi-tool latency no greater than 5000 ms, and streaming/typing indicators within 1000 ms. The harness captures the direct QA API only. Meta transport, channel queuing, delivery and WhatsApp rendering are outside this measurement.

The fields `first_byte_ms`, `first_event_ms` and `elapsed_ms` measure from the start of each request. First event is a proxy for streaming responsiveness, not the time of the first useful answer token or a WhatsApp typing indicator. Empty stream completion, timeout and HTTP errors are retained; they do not improve latency by being treated as successes.

For a controlled small sample, obtain enough fresh conversations and run repeated variants:

```bash
python run.py run --suite functional --id FR-01-V01,FR-01-V02,FR-01-V03 --live --out reports/timing_pilot
python run.py performance --results reports/timing_pilot/results.json
```

The three-case pilot is deliberately INCONCLUSIVE for P95. By default the analyzer requires at least 20 successful complete samples **per class** (`rag` and `tool`) and uses nearest-rank P95. Twenty samples is a minimal reporting guard, not a statistical reliability claim. Select more independent cases and expand request budgets only to agreed QA capacity. Balance variants are single-tool timings; use `TURN-003` and suitable independent repetitions for the multi-tool target, and do not claim a single-tool benchmark validates multi-tool latency.

Build a dedicated performance dataset for release benchmarking with representative concurrency, warmed/cold services, realistic payload sizes and recorded deployment versions. This runner intentionally executes sequentially. It is not a capacity or stress-load generator. `max_concurrency` in settings is reserved; it does not currently launch parallel traffic. Throughput/load and recovery need the separate scoped test harness and QA capacity agreement.

A performance report with only one populated class leaves the missing class INCONCLUSIVE. Any captured latency-class failure prevents an overall clean pass. Record failure counts and full distributions alongside P95. Use the channel UAT timestamps for the actual end-to-end SLA and typing measurement.
