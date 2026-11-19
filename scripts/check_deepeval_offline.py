#!/usr/bin/env python3
"""Installed native DeepEval check with synthetic inputs and local HTTP mocks only."""
import asyncio
import importlib.metadata
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.deepeval_adapter import telemetry_off,SchemaClient,evaluate_native
from qa.campaign_io import Ledger
telemetry_off()

async def check(scenario):
    import httpx
    from pydantic import BaseModel
    class Output(BaseModel):score:float
    requests=[]
    def reply(request):
        body=json.loads(request.content);requests.append(body)
        if scenario=='http_failure':return httpx.Response(502,json={'error':{'message':'SYNTHETIC ONLY'}})
        if scenario=='quota':return httpx.Response(429,headers={'retry-after':'5'},json={'error':{'message':'SYNTHETIC ONLY'}})
        if scenario=='malformed':content='not JSON'
        elif scenario=='truncation':content='{"score":'
        else:content=json.dumps({'score':1.0,'statements':['SYNTHETIC ANSWER'],'verdicts':[{'verdict':'yes','reason':'SYNTHETIC ONLY'}],
            'reason':'SYNTHETIC ONLY','critical_fabrication_candidate':False,'instruction_hijack_candidate':False,'disclosure_candidate':False})
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'synthetic',
            'choices':[{'index':0,'finish_reason':'length' if scenario=='truncation' else 'stop','message':{'role':'assistant','content':content}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    profile={'model':'nvidia/nemotron-3.5-super-vl-120b-a12b','generation_parameters':{'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}}
    env={'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]',
         'MORPHEUS_EVAL_API_KEY':'SYNTHETIC_ONLY','MORPHEUS_EVAL_MODEL':profile['model']}
    with tempfile.TemporaryDirectory() as tmp:
        ledger=Ledger(Path(tmp)/'ledger.sqlite',{'judge':20},time.time()+60)
        adapter=SchemaClient(env,profile,ledger,'SYNTHETIC-JOB',job_limit=6)
        with patch('httpx.HTTPTransport',return_value=httpx.MockTransport(reply)),patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(reply)),patch('socket.create_connection',side_effect=AssertionError('Offline check prohibits external connections')):
            try:
                if scenario=='success':
                    assert adapter.generate('SYNTHETIC ONLY',Output).score==1.0
                    result=await evaluate_native('deepeval_answer_relevancy',{'user_input':'SYNTHETIC QUESTION','response':'SYNTHETIC ANSWER'},adapter,0.88)
                    assert result['score']==1.0 and result['method']=='native_deepeval:AnswerRelevancyMetric'
                    custom=await evaluate_native('custom_coverage_review',{'user_input':'SYNTHETIC QUESTION','response':'SYNTHETIC ANSWER'},adapter,None)
                    assert custom['score']==1.0
                else:
                    try:await adapter.a_generate('SYNTHETIC ONLY',Output)
                    except Exception:pass
                    else:raise AssertionError('Invalid completion became a score')
                assert ledger.counts()['judge']==len(requests)
                if scenario=='quota':assert ledger.provider_stop()[0]==429
                assert len(requests)<=6
            finally:await adapter.close()
    return {'scenario':scenario,'mock_http_requests':len(requests),'status':'PASS'}

async def main():
    assert importlib.metadata.version('deepeval')=='3.8.4','Install requirements-deepeval.txt'
    outcomes=[await check(s) for s in ('success','malformed','http_failure','truncation','quota')]
    print(json.dumps({'native_deepeval_mock':'PASS','package':importlib.metadata.version('deepeval'),
        'network_requests':0,'data':'SYNTHETIC ONLY','checks':outcomes}))

if __name__=='__main__':
    try:asyncio.run(main())
    except importlib.metadata.PackageNotFoundError:
        print('Native DeepEval not installed. Install requirements-deepeval.txt in .venv-deepeval; no live requests made.',file=sys.stderr);raise SystemExit(2)
