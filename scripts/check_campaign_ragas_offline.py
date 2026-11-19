#!/usr/bin/env python3
"""Installed native RAGAS campaign-worker/ledger integration, entirely mocked HTTP."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from qa.campaign_io import Ledger,fingerprint
from scripts.campaign_judge import run_job

async def main():
    scenarios=[]
    for scenario in ('success','http_failure','length','quota'):
        bodies=[]
        def reply(request):
            bodies.append(json.loads(request.content))
            if scenario in ('http_failure','quota') and len(bodies)==1:return httpx.Response(502 if scenario=='http_failure' else 429,json={'error':{'message':'SYNTHETIC ONLY'}})
            return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'synthetic',
                'choices':[{'index':0,'finish_reason':'length' if scenario=='length' and len(bodies)==1 else 'stop','message':{'role':'assistant','content':'{"rating":4}'}}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        profile={'model':'nvidia/nemotron-3.5-super-vl-120b-a12b','generation_parameters':{'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}}
        env={'MORPHEUS_EVAL_MODEL':profile['model'],'MORPHEUS_EVAL_API_KEY':'SYNTHETIC ONLY','MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'budget.sqlite',{'judge':10},time.time()+60)
            material={'user_input':'SYNTHETIC QUESTION','response':'SYNTHETIC ANSWER','reference':'SYNTHETIC REFERENCE','reference_review_status':'REQUIRES_BUSINESS_REVIEW'}
            job={'job_id':'SYNTHETIC','metric':'ragas_answer_accuracy','material':material,'material_sha256':fingerprint(material),
                'profile':profile,'request_limit':2,'threshold':None,'ledger':str(ledger.path)}
            with patch('scripts.campaign_judge.load',return_value=({},env)),patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(reply)),patch('socket.create_connection',side_effect=AssertionError('No external network')):
                result=await run_job(job)
            assert ledger.counts()['judge']==len(bodies)
            assert len(bodies)==(1 if scenario=='quota' else 2)
            assert all(b['max_tokens']==4096 and b['chat_template_kwargs']=={'enable_thinking':False} for b in bodies)
            if scenario=='success':assert result['score']==1.0
            else:assert result['score'] is None
            scenarios.append({'scenario':scenario,'mock_http_requests':len(bodies),'status':'PASS'})
    print(json.dumps({'native_campaign_ragas_mock':'PASS','network_requests':0,'data':'SYNTHETIC ONLY','scenarios':scenarios}))

if __name__=='__main__':asyncio.run(main())
