#!/usr/bin/env python3
"""Actual installed native SDK + expanded capture adapter, synthetic data, no network."""
import asyncio
import importlib.metadata
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['RAGAS_DO_NOT_TRACK']='true'
import httpx
from qa.config import ROOT
from qa.faq_catalogue_evaluation import load_catalogue_run
from scripts.evaluate_faq_ragas import native_evaluate,JudgeBudget
sys.path.insert(0,str(ROOT/'tests'))
from test_catalogue_evaluation import synthetic_capture


async def check(failure=None):
    requests=[]
    async def handler(request):
        body=json.loads(request.content);requests.append(body)
        assert body['max_tokens']==4096 and body['chat_template_kwargs']['enable_thinking'] is False
        if failure=='502' and len(requests)==1:return httpx.Response(502,json={'error':{'message':'SYNTHETIC GATEWAY FAILURE'}})
        finish='length' if failure=='length' and len(requests)==1 else 'stop'
        content='{"rating":' if finish=='length' else '{"rating": 4}'
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'synthetic',
            'choices':[{'index':0,'finish_reason':finish,'message':{'role':'assistant','content':content}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    with tempfile.TemporaryDirectory() as tmp:
        path=Path(tmp);synthetic_capture(path);_,rows=load_catalogue_run(path)
        rows=rows[:1]
        # Keep SDK input/output explicitly synthetic; only the adapter shape and
        # frozen bindings come from the expanded fixture.
        rows[0].update(user_input='SYNTHETIC QUESTION',response='SYNTHETIC ANSWER',reference='SYNTHETIC REFERENCE')
        args=SimpleNamespace(limit=1,mode='answer-accuracy',allow_draft_references=True,max_tokens=4096,thinking='off')
        env={'MORPHEUS_EVAL_MODEL':'nvidia/nemotron-3.5-super-vl-120b-a12b',
             'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_API_KEY':'SYNTHETIC'}
        budget=JudgeBudget(['synthetic.invalid'],2,60)
        with patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(handler)):
            await native_evaluate(rows,env,args,budget)
        assert len(requests)==2 and budget.count==2
        score=rows[0]['metric_scores']['answer_accuracy']
        if failure:
            assert score is None and rows[0]['evaluation_errors']
        else:assert score['score']==1.0
        assert all(v is None for k,v in rows[0]['metric_scores'].items() if k!='answer_accuracy')
        return {'scenario':failure or 'both_judges_complete','mock_requests':len(requests),'expected_guard':'PASS'}

async def main():
    checks=[]
    for scenario in (None,'502','length'):checks.append(await check(scenario))
    print(json.dumps({'native_sdk_offline_check':'PASS','network_requests':0,'data':'SYNTHETIC ONLY',
        'packages':{n:importlib.metadata.version(n) for n in ('ragas','openai','instructor','httpx')},
        'max_tokens':4096,'thinking':False,'scenarios':checks}))

if __name__=='__main__':asyncio.run(main())
