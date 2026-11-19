"""Expected visible refusals are reviewable, never automatically a safety pass."""
import tempfile,json,unittest
from pathlib import Path
from unittest.mock import patch
from qa.safety_response_continuation import (disposition,prepare,validate,execute,CONTENT_BLOCK,DOMAIN_BLOCK,parent_files)
from qa.safety_pack import SafetyCampaign,read
from qa.campaign_io import Ledger
from test_safety_pack import fixture,worker,VERSIONS


def response_transport(sent,status='error',answer=CONTENT_BLOCK):
    def send(url,method,headers,body,config):
        sent.append(json.loads(body));event={'type':'response','response':{'status':status,'message':answer}}
        return {'status_code':200,'complete':True,'error':None,'events':[{'data':json.dumps(event)}],'headers':{},'raw_body':json.dumps(event),'first_event_ms':1,'elapsed_ms':3}
    return send


def linked(root):
    parent,m,s,c,e,k=fixture(root);ledger=Ledger(parent/'requests.sqlite',{'api':102,'judge':510},m['expires_at']);sent=[]
    engine=SafetyCampaign(parent,m,s,c,e,k,response_transport(sent),worker([]))
    with patch('qa.campaign.time.sleep',return_value=None):
        for case in m['cases'][:3]:engine.capture_case(case)
    child=root/'linked'
    with patch('qa.safety_pack.package_versions',return_value=VERSIONS):new,state=prepare(child,parent,c,e)
    return parent,child,new,state,c,e,(child/'identity.private.key').read_bytes()

class RefusalContinuationTests(unittest.TestCase):
    def test_dispositions_are_observations_not_safety_scores(self):
        r={'status_code':200,'complete':True,'error':None,'application_status':'error','answer':CONTENT_BLOCK}
        self.assertEqual(disposition(r),'VISIBLE_CONTENT_SAFETY_BLOCK')
        self.assertEqual(disposition({**r,'application_status':'unavailable','answer':DOMAIN_BLOCK}),'VISIBLE_MTN_SCOPE_REFUSAL')
        self.assertEqual(disposition({**r,'answer':'SYNTHETIC database failure'}),'APPLICATION_FAILURE_OR_UNAVAILABLE')
        self.assertEqual(disposition({**r,'complete':False}),'UNUSABLE_RESPONSE')
        self.assertEqual(disposition({**r,'status_code':None}),'UNUSABLE_RESPONSE')
    def test_saved_refusals_reviewed_without_resending_and_new_refusals_do_not_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent,d,m,s,c,e,k=linked(Path(tmp));before=parent_files(parent);sent=[];jobs=[]
            self.assertEqual(len(m['review_only_saved_attempts']),3)
            with patch('qa.campaign.time.sleep',return_value=None):
                self.assertEqual(execute(d,m,s,c,e,k,4,response_transport(sent,'unavailable',DOMAIN_BLOCK),worker(jobs)),2)
            self.assertEqual(len(sent),4);self.assertEqual(len(jobs),14)
            self.assertEqual(s['consecutive_application_failures'],0)
            self.assertEqual(parent_files(parent),before)
            with patch('qa.safety_pack.package_versions',return_value=VERSIONS):validate(d,m,s,c,e)
            result=read(d/'results.json');self.assertIsNone(result['safety_pass_rate']);self.assertIsNone(result['safety_score'])
            self.assertEqual(len(result['evaluations']),14)
            self.assertTrue(all(r['status']=='INCONCLUSIVE' for r in list(s['cases'].values())[:7]))
            self.assertEqual(s['ledger_counts'],{'api':7,'judge':35})
    def test_genuine_application_errors_still_stop_after_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent,d,m,s,c,e,k=linked(Path(tmp));sent=[];jobs=[]
            with patch('qa.campaign.time.sleep',return_value=None):execute(d,m,s,c,e,k,96,response_transport(sent,'error','SYNTHETIC backend unavailable'),worker(jobs))
            self.assertEqual(len(sent),3);self.assertEqual(s['consecutive_application_failures'],3)
            self.assertIn('Three genuine',s['stop_reason']);self.assertEqual(len(jobs),6)
    def test_resume_skips_reviewed_saved_attempts_and_completed_captures(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent,d,m,s,c,e,k=linked(Path(tmp));sent=[];jobs=[]
            with patch('qa.campaign.time.sleep',return_value=None):
                execute(d,m,s,c,e,k,1,response_transport(sent),worker(jobs));execute(d,m,s,c,e,k,1,response_transport(sent),worker(jobs))
            self.assertEqual(len(sent),2);self.assertEqual(len(jobs),10)
            self.assertEqual(s['ledger_counts'],{'api':5,'judge':25})
    def test_parent_changes_and_credential_drift_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent,d,m,s,c,e,k=linked(Path(tmp))
            with patch('qa.safety_pack.package_versions',return_value=VERSIONS):
                with self.assertRaises(ValueError):validate(d,m,s,c,{**e,'MORPHEUS_EVAL_API_KEY':'SYNTHETIC_CHANGED'})
                p=parent/'checkpoint.json';p.write_text(p.read_text()+' ')
                with self.assertRaises(ValueError):validate(d,m,s,c,e)
