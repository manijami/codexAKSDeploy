#!/usr/bin/env python3
"""Installed DeepEval + quota recovery with synthetic capture and mocked HTTP only."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
from test_quota_recovery import fixture,prepare
from qa.campaign_io import atomic
from qa.evaluator_recovery import execute,source_inventory
from qa.deepeval_adapter import telemetry_off
from scripts.campaign_judge import run_job
telemetry_off()
import httpx


def main():
    requests=[]
    def reply(request):
        body=json.loads(request.content);requests.append(body)
        assert body['max_tokens']==4096 and body['chat_template_kwargs']=={'enable_thinking':False}
        return httpx.Response(200,json={'id':'synthetic','object':'chat.completion','created':0,'model':'synthetic',
            'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps({
             'statements':['SYNTHETIC ANSWER'],'verdicts':[{'verdict':'yes','reason':'SYNTHETIC ONLY'}],'reason':'SYNTHETIC ONLY'})}}],
            'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}})
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp);source,config,env=fixture(root);out,plan,manifest,state,jobs=prepare(root,source)
        before=source_inventory(source)
        def worker(job,path,seconds):
            with patch('scripts.campaign_judge.load',return_value=(config,env)):
                result=asyncio.run(run_job(job))
            atomic(path,result);return result
        with patch('qa.evaluator_recovery.load',return_value=(config,env)),patch('httpx.HTTPTransport',return_value=httpx.MockTransport(reply)),patch('httpx.AsyncHTTPTransport',return_value=httpx.MockTransport(reply)),patch('qa.transport.request',side_effect=AssertionError('Morpheus is prohibited')),patch('socket.create_connection',side_effect=AssertionError('No external network')):
            assert execute(out,plan,manifest,state,jobs,worker)==0
            assert execute(out,plan,manifest,state,jobs,worker)==0
        result=json.loads((out/'recovery_summary.json').read_text())
        assert len(requests)==3 and result['judge_requests_added']==3 and result['morpheus_requests_added']==0
        assert len(result['recovered'])==1 and result['recovered'][0]['score']==1.0
        assert before==source_inventory(source)
        assert 'remaining original evaluator failures: 0' in (out/'report.html').read_text()
    print(json.dumps({'native_quota_recovery_mock':'PASS','data':'SYNTHETIC ONLY','external_network_requests':0,
        'mock_judge_requests':len(requests),'morpheus_requests':0,'repeat_execution_added_requests':0,
        'source_artifacts_unchanged':True,'native_score_and_report':'PASS'}))

if __name__=='__main__':main()
