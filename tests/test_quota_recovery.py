"""Synthetic-only quota selection, immutable source recovery and strict budgets."""
import copy
import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qa.config import ROOT
from qa.campaign_io import Ledger,immutable,atomic,fingerprint,BudgetStop
from qa.campaign_plan import build_manifest
from qa.campaign_privacy import sanitize
from qa.faq_pilot import digest
from qa.evaluator_recovery import audit_source,source_inventory,execute,render,configured_settings,validate_result


def fixture(root,status=429,chatbot='INCONCLUSIVE',score=None):
    source=root/'source';source.mkdir()
    config=configured_settings();env={'QA_MSISDN_A':'SYNTHETIC_ONLY','MORPHEUS_API_KEY':'SYNTHETIC_ONLY',
      'MORPHEUS_EVAL_MODEL':'nvidia/nemotron-3.5-super-vl-120b-a12b','MORPHEUS_EVAL_API_KEY':'SYNTHETIC_ROTATED',
      'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
    versions={'deepeval':'3.8.4','ragas':'0.4.3'}
    with patch('qa.campaign_plan.package_versions',return_value=versions):
        m=build_manifest(config,env,b'SYNTHETIC KEY','SYNTHETIC-CAMPAIGN','SYNTHETIC TIME',SimpleNamespace(mode='execute',max_api_requests=600,max_judge_requests=6000,max_seconds=86400))
    case=next(c for c in m['cases'] if c['id']=='FAQ-DOC-21-03');m['cases']=[case]
    immutable(source/'manifest.json',m);(source/'campaign.lock').touch()
    (source/'dataset.snapshot.json').write_bytes((ROOT/'datasets/business_faq_v1.json').read_bytes())
    attempt='SYNTHETIC-CAMPAIGN:'+case['id']+':1';sha=hashlib.sha256(b'SYNTHETIC ANSWER').hexdigest()
    response={'attempt_id':attempt,'response_sha256':sha,'status_code':200,'application_status':'success',
      'complete':True,'error':None,'answer':'SYNTHETIC ANSWER'}
    payload={'message':case['turns'][0]['message'],'msisdn':'[REDACTED]'}
    response_path='responses/'+case['id']+'/turn_01.json';immutable(source/response_path,{'response':response,'payload':payload})
    metric='deepeval_answer_relevancy';material={'test_id':case['id'],'attempt_id':attempt,'response_sha256':sha,
      'user_input':case['turns'][0]['message'],'response':'SYNTHETIC ANSWER','expected':sanitize(case['expected']),
      'reference':sanitize(case['faq_reference']['reference_answer']),'reference_review_status':'REQUIRES_BUSINESS_REVIEW'}
    material_sha=fingerprint(material);binding=fingerprint({'metric':metric,'material':material_sha,'evaluator':m['evaluator'],'code':m['code_sha256']});job_id=binding+':1'
    result={'score':score,'error':'SYNTHETIC evaluator incomplete' if score is None else None,'exception_type':'RateLimitError' if status==429 else 'ValidationError','job_id':job_id,'material_sha256':material_sha,'profile_sha256':fingerprint(m['evaluator'])}
    metric_path='metrics/'+job_id.replace(':','_')+'.json';immutable(source/metric_path,result)
    job={'job_id':job_id,'metric':metric,'material':material,'material_sha256':material_sha,'profile':m['evaluator'],
      'request_limit':3,'threshold':0.88,'ledger':str(source/'requests.sqlite')}
    immutable(source/'jobs'/(job_id.replace(':','_')+'.json'),job)
    expires=time.time()+3600;ledger=Ledger(source/'requests.sqlite',{'api':600,'judge':6000},expires)
    call=ledger.claim('api',attempt);ledger.finish(call,200);call=ledger.claim('judge',job_id);ledger.finish(call,status)
    grade={**result,'binding':binding,'attempt_id':attempt,'response_sha256':sha,'evaluation_attempt':1,'path':metric_path,
       'artifact_sha256':digest(source/metric_path)}
    state={'manifest_sha256':digest(source/'manifest.json'),'expires_at':expires,'ledger_counts':ledger.counts(),'run_status':'PARTIAL',
      'updated_at':'SYNTHETIC TIME','stop_reason':'SYNTHETIC quota stop','cases':{case['id']:{'status':chatbot,'attempted':True,'responses':[response],
      'payloads':[payload],'response_artifact_sha256':{response_path:digest(source/response_path)},'metrics':{metric:[grade]},'reasons':[],'missing_evidence':['No actual chunks; faithfulness unassessed']}}}
    atomic(source/'checkpoint.json',state)
    return source,config,env


def prepare(root,source):
    m,s,jobs,selected,excluded,caps,counts,valid=audit_source(source,check_runtime=False)
    out=root/'recovery';out.mkdir()
    plan={'source_directory':str(source),'source_files_sha256':source_inventory(source),'selected':selected,'original_ledger_counts':counts,
      'expires_at':s['expires_at'],'original_caps':caps,'evaluator':m['evaluator']}
    immutable(out/'recovery_manifest.json',plan)
    return out,plan,m,s,jobs


class QuotaRecoveryTests(unittest.TestCase):
    def test_select_only_quota_job_not_validation_or_valid_or_chatbot_failure(self):
        for status,chatbot,score,expected in ((429,'INCONCLUSIVE',None,1),(200,'INCONCLUSIVE',None,0),(429,'FAIL',None,0),(429,'INCONCLUSIVE',1.0,0)):
            with self.subTest(status=status,chatbot=chatbot,score=score),tempfile.TemporaryDirectory() as tmp:
                source,_,_=fixture(Path(tmp),status,chatbot,score)
                result=audit_source(source,check_runtime=False);self.assertEqual(len(result[3]),expected)

    def test_success_and_repeat_execution_do_not_resend_or_change_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,config,env=fixture(root);out,plan,m,s,jobs=prepare(root,source)
            before=source_inventory(source);calls=[]
            def worker(job,path,seconds):
                calls.append(job);ledger=Ledger(job['ledger'])
                for i in range(3):call=ledger.claim('judge',job['job_id'],job_limit=3);ledger.finish(call,200)
                result={'score':1.0,'method':'SYNTHETIC native mock','job_id':job['job_id'],'material_sha256':job['material_sha256'],
                  'profile_sha256':fingerprint(job['profile']),'generation_parameters':job['profile']['generation_parameters'],'evaluator_version':'3.8.4'}
                atomic(path,result);return result
            with patch('qa.evaluator_recovery.load',return_value=(config,env)),patch('qa.transport.request',side_effect=AssertionError('Morpheus prohibited')):
                self.assertEqual(execute(out,plan,m,s,jobs,worker),0)
                self.assertEqual(execute(out,plan,m,s,jobs,worker),0)
            self.assertEqual(len(calls),1);self.assertEqual(before,source_inventory(source))
            summary=json.loads((out/'recovery_summary.json').read_text())
            self.assertEqual(summary['morpheus_requests_added'],0);self.assertEqual(summary['judge_requests_added'],3)
            self.assertEqual(len(summary['recovered']),1);self.assertEqual(len(summary['remaining_original_failures']),0)
            self.assertIn('Recovered scores',(out/'report.html').read_text())
            self.assertEqual(len(s['cases']['FAQ-DOC-21-03']['metrics']['deepeval_answer_relevancy']),1)

    def test_original_deadline_is_never_extended(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,config,env=fixture(root);out,plan,m,s,jobs=prepare(root,source)
            with patch('qa.evaluator_recovery.load',return_value=(config,env)),patch('qa.campaign_io.time.time',return_value=s['expires_at']+1):
                self.assertEqual(execute(out,plan,m,s,jobs,lambda *a:self.fail('expired work sent')),2)
            self.assertEqual(json.loads((out/'recovery_checkpoint.json').read_text())['status'],'BLOCKED_ORIGINAL_DEADLINE_EXPIRED')

    def test_modified_capture_reference_or_job_material_rejected(self):
        for kind in ('manifest','answer','job'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);source,_,_=fixture(root)
                if kind=='manifest':p=source/'manifest.json';p.chmod(0o644);d=json.loads(p.read_text());d['campaign_id']='changed'
                elif kind=='answer':p=next((source/'responses').rglob('*.json'));p.chmod(0o644);d=json.loads(p.read_text());d['response']['answer']='changed'
                else:p=next((source/'jobs').glob('*.json'));p.chmod(0o644);d=json.loads(p.read_text());d['material']['reference']='invented reference'
                p.write_text(json.dumps(d))
                with self.assertRaises(ValueError):audit_source(source,check_runtime=False)

    def test_model_generation_profile_drift_never_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,config,env=fixture(root);out,plan,m,s,jobs=prepare(root,source)
            env['MORPHEUS_EVAL_MODEL']='nvidia/DIFFERENT'
            with patch('qa.evaluator_recovery.load',return_value=(config,env)):
                with self.assertRaises(ValueError):execute(out,plan,m,s,jobs,lambda *a:self.fail('drifted model sent'))

    def test_uncertain_recovery_does_not_get_third_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,config,env=fixture(root);out,plan,m,s,jobs=prepare(root,source)
            def interrupt(*a):raise KeyboardInterrupt
            with patch('qa.evaluator_recovery.load',return_value=(config,env)):
                self.assertEqual(execute(out,plan,m,s,jobs,interrupt),2)
                self.assertEqual(execute(out,plan,m,s,jobs,lambda *a:self.fail('uncertain retry repeated')),2)
            entry=next(iter(json.loads((out/'recovery_checkpoint.json').read_text())['jobs'].values()))
            self.assertIsNone(entry['result']['score']);self.assertIn('uncertain',entry['result']['error'])

    def test_result_binding_and_raw_nonfinite_scores_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source,_,_=fixture(root);out,plan,m,s,jobs=prepare(root,source);job=next(iter(jobs.values()))['job']
            for value in ({'score':1.0,'job_id':'wrong'}, {'score':float('nan'),'job_id':job['job_id'],'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(m['evaluator'])}):
                with self.assertRaises(ValueError):validate_result(value,job,m)

    def test_real_configuration_loader_still_rejects_bare_secret_lines(self):
        from qa.config import load
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'config').mkdir();(root/'config/settings.json').write_text('{}')
            (root/'.env').write_text('MORPHEUS_EVAL_API_KEY=SYNTHETIC\nBARE_SYNTHETIC_INVALID_LINE\n')
            with self.assertRaises(ValueError):load(root)
