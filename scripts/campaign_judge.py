#!/usr/bin/env python3
"""One bounded native metric worker; no Morpheus calls, no implicit model fallback."""
import argparse
import asyncio
import importlib.metadata
import json
import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.config import load,secrets
from qa.campaign_io import Ledger,atomic,fingerprint
from qa.campaign_privacy import sanitize
from qa.deepeval_adapter import telemetry_off,SchemaClient,evaluate_native


async def ragas_metric(job,env,ledger):
    from scripts.evaluate_faq_ragas import native_evaluate
    class Budget:
        def __init__(self):
            self.count=0;self.maximum=job['request_limit'];self.deadline=time.monotonic()+min(60,ledger.remaining_seconds());self.last=None
        def inspect_request(self,request):
            body=json.loads(request.content)
            if body.get('model')!=job['profile']['model'] or body.get('max_tokens')!=4096 or body.get('chat_template_kwargs')!={'enable_thinking':False} or body.get('temperature')!=0.01 or body.get('top_p')!=0.1:raise ValueError('Generation settings did not reach the wire')
        def claim(self,url):
            p=urlsplit(str(url))
            if p.scheme!='https' or p.hostname not in json.loads(env['MORPHEUS_EVAL_ALLOWED_HOSTS_JSON']):raise ValueError('Judge HTTPS allowlist mismatch')
            self.last=ledger.claim('judge',job['job_id'],job_limit=self.maximum)
            self.count+=1
        def record_response(self,status,retry_after):ledger.finish(self.last,status,retry_after)
    name=job['metric'].removeprefix('ragas_');m=job['material']
    row={**m,'eligible_response':True,'reference_review_status':m.get('reference_review_status','REQUIRES_BUSINESS_REVIEW'),
        'retrieved_contexts':m.get('retrieved_contexts'),'metric_scores':{n:None for n in ('answer_accuracy','context_precision','context_recall','faithfulness')}}
    args=SimpleNamespace(limit=1,mode='answer-accuracy' if name=='answer_accuracy' else 'all',
        allow_draft_references=True,max_tokens=4096,thinking='off',temperature=0.01,top_p=0.1,
        material_sanitizer=lambda x:sanitize(x,secrets(env)))
    # Run only the requested native metric; other conditional metrics are separate checkpoints.
    from scripts import evaluate_faq_ragas as module
    original=module.metric_inputs
    module.metric_inputs=lambda r,mode,drafts:{k:v for k,v in original(r,mode,drafts).items() if k==name}
    try:await native_evaluate([row],env,args,Budget())
    finally:module.metric_inputs=original
    score=row['metric_scores'][name]
    if score is None:return {'score':None,'error':'native metric incomplete','diagnostics':row.get('evaluation_diagnostics',[])}
    return score


async def run_job(job):
    telemetry_off();_,env=load()
    ledger=Ledger(job['ledger'])
    profile=job['profile']
    if profile['model']!=env.get('MORPHEUS_EVAL_MODEL'):raise ValueError('Configured model drift')
    if profile['generation_parameters']!={'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}:raise ValueError('Generation parameter drift')
    job['material']=sanitize(job['material'],secrets(env))
    if job['metric'].startswith('ragas_'):
        if importlib.metadata.version('ragas')!='0.4.3':raise ValueError('Pinned RAGAS required')
        result=await ragas_metric(job,env,ledger);package='ragas'
    else:
        if importlib.metadata.version('deepeval')!='3.8.4':raise ValueError('Pinned DeepEval required')
        adapter=SchemaClient(env,profile,ledger,job['job_id'],job['request_limit'])
        try:result=await evaluate_native(job['metric'],job['material'],adapter,job['threshold'])
        finally:await adapter.close()
        result['diagnostics']=adapter.events;package='deepeval'
    result.update(job_id=job['job_id'],material_sha256=job['material_sha256'],
        profile_sha256=fingerprint(profile),evaluator_version=importlib.metadata.version(package),
        generation_parameters=profile['generation_parameters'])
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--job',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);args=parser.parse_args()
    if args.out.exists():raise ValueError('Metric output already exists; preserve history')
    job=json.loads(args.job.read_text())
    try:result=asyncio.run(run_job(job))
    except Exception as exc:
        result={'score':None,'error':'Evaluator incomplete','exception_type':type(exc).__name__,'job_id':job['job_id'],
                'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
    _,env=load();atomic(args.out,sanitize(result,secrets(env)))
    return 0 if result.get('score') is not None else 2

if __name__=='__main__':raise SystemExit(main())
