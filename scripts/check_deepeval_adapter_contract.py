#!/usr/bin/env python3
"""Schema/HTTP contract mocks for DeepEval adapter when SDK isn't installed.

Uses installed OpenAI/httpx/Pydantic from .venv-ragas. Does not claim a native
DeepEval metric run; run check_deepeval_offline.py after installing that SDK.
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.deepeval_adapter import SchemaClient
from qa.campaign_io import Ledger,BudgetStop
import httpx
from pydantic import BaseModel

class Output(BaseModel):score:float

async def main():
    checks=[]
    for scenario in ('sync_success','async_success','malformed','length','http_error','quota','job_cap'):
        bodies=[]
        def reply(request):
            body=json.loads(request.content);bodies.append(body)
            if scenario=='http_error':return httpx.Response(502,json={'error':{'message':'SYNTHETIC ONLY'}})
            if scenario=='quota':return httpx.Response(429,headers={'retry-after':'5'},json={'error':{'message':'SYNTHETIC ONLY'}})
            content='not json' if scenario=='malformed' else '{"score":1.0}'
            return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'synthetic',
                'choices':[{'index':0,'finish_reason':'length' if scenario=='length' else 'stop','message':{'role':'assistant','content':content}}],
                'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        profile={'model':'nvidia/synthetic','generation_parameters':{'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}}
        env={'MORPHEUS_EVAL_MODEL':profile['model'],'MORPHEUS_EVAL_API_KEY':'SYNTHETIC ONLY',
             'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'budget.sqlite',{'judge':2},time.time()+60)
            client=SchemaClient(env,profile,ledger,'SYNTHETIC',job_limit=1)
            with patch('httpx.HTTPTransport',return_value=httpx.MockTransport(reply)),patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(reply)),patch('socket.create_connection',side_effect=AssertionError('No external network')):
                try:
                    if scenario=='sync_success':assert client.generate('SYNTHETIC',Output).score==1
                    elif scenario in ('async_success','job_cap'):
                        assert (await client.a_generate('SYNTHETIC',Output)).score==1
                        if scenario=='job_cap':
                            try:await client.a_generate('SYNTHETIC',Output)
                            except Exception as exc:
                                assert isinstance(exc,BudgetStop) or isinstance(exc.__cause__,BudgetStop)
                            else:raise AssertionError('Budget not enforced')
                    else:
                        try:await client.a_generate('SYNTHETIC',Output)
                        except Exception:pass
                        else:raise AssertionError('Invalid evaluator response became a score')
                    assert ledger.counts()['judge']==len(bodies)==1
                    assert bodies[0]['max_tokens']==4096 and bodies[0]['chat_template_kwargs']=={'enable_thinking':False}
                    if scenario=='quota':assert ledger.provider_stop()[0]==429
                finally:await client.close()
        checks.append({'scenario':scenario,'status':'PASS','mock_http_requests':len(bodies)})
    print(json.dumps({'adapter_contract':'PASS','actual_native_deepeval_sdk_executed':False,'network_requests':0,
        'data':'SYNTHETIC ONLY','checks':checks}))

if __name__=='__main__':asyncio.run(main())
