#!/usr/bin/env python3
"""Traceability IDs for unnumbered RAG/RAI rows are local catalogue IDs."""
import csv
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT
from qa.runner import cases_from


def main():
    cases=cases_from()
    reqs=[]
    for i in range(1,19):
        matching=[c for c in cases if f'FR-{i:02d}' in c.get('requirements',[])]
        reqs.append((f'FR-{i:02d}',matching[0]['category'],'API scenarios; source/tool and fixture evidence required for business correctness'))
    reqs += [(f'NFR-{i:02d}',name,gap) for i,name,gap in [
      (1,'Privacy and log masking','Manual backend telemetry audit; local report redaction is verified separately'),
      (2,'Session binding and zero trust','A/B authentication and APIM/tool authorization export required'),
      (3,'Latency and throughput','Direct API P95 analyzer implemented; actual channel latency, concurrency and throughput require scoped channel/load execution'),
      (4,'Graceful degradation and circuit breaking','Controlled backend failure fixtures required; throttling/recovery is infrastructure UAT'),
      (5,'Localization','Seed prompts provided; native language review and tool evidence needed'),
      (6,'Human handoff','Actual handoff queue/tool evidence needed, including two consecutive failure fixture')]]
    for i,name in enumerate(['Context recall','Context precision','Retrieval relevancy','Faithfulness','Groundedness','Answer relevancy'],1):
        reqs.append((f'RAG-{i:02d}',name,'Local ID assigned to source table 5 row; evidence scorer and normalized external score import, with optional custom judge for supported metrics'))
    for i,name in enumerate(['Prompt injection','Jailbreak resistance','Cross-account PII leakage','Harmful content and toxicity','Bias and fairness','Policy compliance'],1):
        reqs.append((f'RAI-{i:02d}',name,'Local ID assigned to source table 6 row; adversarial seeds and evidence-backed review; representative calibrated suite needed for sign-off'))
    reqs += [
      ('STAGE-1','Golden dataset','220 editable seeds and coverage mapping; human-approved answers, fixture states and source versions must be curated'),
      ('STAGE-2','Automated API execution','Standard-library HTTPS/SSE runner implemented in place of a pytest/httpx dependency; offline tests use unittest'),
      ('STAGE-3','Continuous evaluation gates','Offline scoring, optional approved judge and CI template; native SDK suites, trace exports and scheduled jobs require configuration'),
      ('STAGE-4','Channel UAT sign-off','Manual test checklist and catalogue; no live WhatsApp sender/webhook/device credentials supplied'),
    ]
    # Some source requirements are cross-cutting rather than explicitly on each seed.
    broad={
      'NFR-03':[c['id'] for c in cases if c.get('latency_class')]+['UAT-004'],
      'NFR-01':['UAT-005'],
      'STAGE-1':['docs/test_catalogue.csv'],
      'STAGE-2':['qa/runner.py','tests/'],
      'STAGE-3':['qa/evaluation.py','scripts/judge_rag.py','scripts/import_metric_scores.py','ci/gitlab-ci.example.yml'],
    }
    with (ROOT/'docs/REQUIREMENT_TRACEABILITY.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['Requirement ID','Source requirement','Mapped test IDs or implementation','Execution scope and remaining dependencies'])
        for identifier,name,gap in reqs:
            ids=broad.get(identifier) or [c['id'] for c in cases if identifier in c.get('requirements',[])]
            writer.writerow([identifier,name,';'.join(ids),gap])
    print('Wrote '+str(len(reqs))+' requirement mappings.')

if __name__=='__main__':main()
