"""CLI/adapter integration checks against synthetic fixtures only."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from qa.config import ROOT
from tests.support import IsolatedTestCase, load_fixture as load
from qa.evaluation import evaluate
from qa.runner import main
from scripts.import_metric_scores import merge_scores
from scripts import judge_rag


class CliTests(IsolatedTestCase):
    def cli(self,*args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return main(list(args))
    def test_dry_plan_all_cases_no_http(self):
        with tempfile.TemporaryDirectory() as directory,patch('qa.runner.request') as transport:
            self.assertEqual(self.cli('run','--suite','all','--out',directory),0)
            transport.assert_not_called()
            data=json.loads((Path(directory)/'results.json').read_text())
            self.assertEqual(len(data['results']),220)
            self.assertTrue(all(x['status']=='NOT_RUN' for x in data['results']))
    def test_demo_and_evidence_review(self):
        with tempfile.TemporaryDirectory() as directory,patch('qa.runner.request') as transport:
            self.assertEqual(self.cli('run','--id','BASELINE-001,API-001,API-002','--demo','--out',directory),0)
            transport.assert_not_called()
            data=json.loads((Path(directory)/'results.json').read_text())
            self.assertEqual(data['summary'],{'INCONCLUSIVE':1,'PASS':2})
            worksheet=Path(directory)/'evidence.json'
            subprocess.run([sys.executable,str(ROOT/'scripts/make_evidence.py'),'--results',str(Path(directory)/'results.json'),'--out',str(worksheet)],check=True,capture_output=True)
            content=json.loads(worksheet.read_text())
            self.assertEqual(len(content['attempts']),1)
            self.assertEqual(self.cli('review','--results',str(Path(directory)/'results.json'),'--evidence',str(worksheet),'--out',str(Path(directory)/'reviewed')),2)
    def test_no_http_is_blocked_not_api_failure(self):
        config,_=load()
        r={'attempt_id':'x','status_code':None,'error':'gaierror: DNS unavailable'}
        self.assertEqual(evaluate({'suite':'smoke'},[r],config)[1],'BLOCKED')
    def test_performance_rejects_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cli('run','--id','BASELINE-001','--demo','--out',directory)
            self.assertEqual(self.cli('performance','--results',str(Path(directory)/'results.json')),2)
    def test_request_budget_prevents_sends(self):
        config,_=load();config['conversation_mode']='client_uuid';config['max_live_requests']=0
        with tempfile.TemporaryDirectory() as directory,patch('qa.runner.load',return_value=(config,{'QA_MSISDN_A':'TEST_A'})),patch('qa.runner.request') as transport:
            self.assertEqual(self.cli('run','--id','BASELINE-001','--live','--out',directory),2)
            transport.assert_not_called()
    def test_postman_has_no_live_values_and_no_write_cases(self):
        collection=json.loads((ROOT/'postman/Morpheus_WhatsApp_QA.postman_collection.json').read_text())
        variables={x['key']:x['value'] for x in collection['variable']}
        self.assertEqual(variables['key_A'],'')
        for folder in collection['item']:
            for item in folder['item']:
                self.assertNotIn('FR-09-',item['name']);self.assertNotIn('TXN-',item['name'])
    def test_external_scores_require_digest_and_trace(self):
        record={'attempt_id':'x','response_sha256':'hash','contexts':[]}
        score={'attempt_id':'x','response_sha256':'hash','metric':'groundedness','score':1,'method':'external/version','reason':'reviewed','evaluator':'reviewer','evaluated_at':'now'}
        result=merge_scores({'attempts':[record]},[score])
        self.assertEqual(result['attempts'][0]['assessments']['groundedness']['score'],1)
        with self.assertRaises(ValueError):merge_scores({'attempts':[record]},[{**score,'response_sha256':'wrong'}])
    def test_judge_is_bounded_and_does_not_grade_authorization(self):
        config,_=load()
        env={'MORPHEUS_EVAL_BASE_URL':'https://judge.example/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["judge.example"]','MORPHEUS_EVAL_MODEL':'TEST_MODEL'}
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            results={'results':[{'request_payload':[{'message':'A question'}],'actual_api_response':[{'attempt_id':'x','response_sha256':'hash','answer':'An answer'}]}]}
            evidence={'attempts':[{'attempt_id':'x','response_sha256':'hash','contexts':[{'text':'A source'}],'critical_fabrication_observed':True}]}
            (directory/'results.json').write_text(json.dumps(results));(directory/'evidence.json').write_text(json.dumps(evidence))
            annotation={'rationale':'Checked source','labels':{'claims_supported':[True],'ranked_context_relevant':[True]},'assessments':{'session_isolation':{'score':1},'answer_relevancy':{'score':.9,'reason':'direct'}},'critical_fabrication_observed':False}
            fake={'status_code':200,'error':None,'raw_body':json.dumps({'choices':[{'message':{'content':json.dumps(annotation)}}]})}
            argv=['judge_rag.py','--results',str(directory/'results.json'),'--evidence',str(directory/'evidence.json'),'--out',str(directory/'out.json'),'--execute','--limit','1']
            with patch.object(sys,'argv',argv),patch('scripts.judge_rag.load',return_value=(config,env)),patch('scripts.judge_rag.request',return_value=fake) as call,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(judge_rag.main(),0)
                self.assertEqual(call.call_count,1)
            result=json.loads((directory/'out.json').read_text())['attempts'][0]
            self.assertTrue(result['critical_fabrication_observed'])
            self.assertNotIn('session_isolation',result['assessments'])

if __name__=='__main__':unittest.main()
