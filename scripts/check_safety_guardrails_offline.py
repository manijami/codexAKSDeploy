#!/usr/bin/env python3
"""Real installed adapter, synthetic HTTP only, no Morpheus or evaluator network."""
import sys,asyncio,json,tempfile,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from unittest.mock import patch
import httpx
from qa.campaign_io import Ledger,fingerprint
from scripts.safety_guardrail_judge import review

async def main():
    checks=[]
    for scenario in ('success','truncated','missing_field','fabricated_quote','quota'):
        bodies=[]
        def reply(request):
            body=json.loads(request.content);bodies.append(body)
            assert body['max_tokens']==4096 and body['chat_template_kwargs']=={'enable_thinking':False}
            assert body['model']=='nvidia/nemotron-3.5-super-vl-120b-a12b'
            assert body['temperature']==.01 and body['top_p']==.1
            if scenario=='quota':return httpx.Response(429,json={'error':{'message':'SYNTHETIC quota','type':'rate_limit_error'}})
            content={'decision':'CONCERN','risk':'high','observation_quote':'SYNTHETIC ANSWER','reason':'SYNTHETIC ONLY'}
            if scenario=='missing_field':del content['decision']
            if scenario=='fabricated_quote':content['observation_quote']='NOT IN THE ANSWER'
            return httpx.Response(200,json={'id':'SYNTHETIC','object':'chat.completion','created':1,'model':body['model'],'choices':[{'index':0,'finish_reason':'length' if scenario=='truncated' else 'stop','message':{'role':'assistant','content':json.dumps(content)}}],'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'requests.sqlite',{'judge':2},time.time()+60)
            profile={'model':'nvidia/nemotron-3.5-super-vl-120b-a12b','generation_parameters':{'max_tokens':4096,'temperature':.01,'top_p':.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}}
            env={'MORPHEUS_EVAL_MODEL':profile['model'],'MORPHEUS_EVAL_API_KEY':'SYNTHETIC_ONLY','MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
            material={'user_input':'SYNTHETIC QUESTION','response':'SYNTHETIC ANSWER','expected':'SYNTHETIC DRAFT RUBRIC'}
            job={'job_id':'SYNTHETIC_JOB','metric':'guardrail_review','material':material,'material_sha256':fingerprint(material),'profile':profile,'request_limit':2,'ledger':str(ledger.path)}
            with patch('scripts.safety_guardrail_judge.load',return_value=({},env)),patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(reply)),patch('socket.create_connection',side_effect=AssertionError('External network forbidden')):
                try:result=await review(job);error=None
                except Exception as exc:result=None;error=type(exc).__name__
            assert len(bodies)==(2 if scenario=='success' else 1)
            assert ledger.counts()['judge']==len(bodies)
            if scenario=='success':assert result['completed'] and len(result['reviews'])==2 and result['score'] is None
            else:assert error and result is None
            checks.append({'scenario':scenario,'mock_http_calls':len(bodies),'status':'PASS'})
    print(json.dumps({'native_schema_adapter_safety_review_mock':'PASS','external_network_requests':0,'data':'SYNTHETIC ONLY','checks':checks}))
if __name__=='__main__':asyncio.run(main())
