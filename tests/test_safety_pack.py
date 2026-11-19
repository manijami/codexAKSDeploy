"""Synthetic no-network checks for separate safety campaign and no replay."""
import tempfile,json,time,copy,unittest
from pathlib import Path
from unittest.mock import patch
from qa.config import ROOT
from qa.evaluator_recovery import configured_settings
from qa.campaign_io import atomic,immutable,Ledger,fingerprint
from qa.safety_pack import make_plan,execute,validate,read,DATA,SafetyCampaign

VERSIONS={'deepeval':'3.8.4','openai':'2.54.0'}
def fixture(root):
    config=configured_settings();config['response_mapping_confirmed']=True
    env={'QA_MSISDN_A':'SYNTHETIC_SUBSCRIBER','MORPHEUS_API_KEY':'SYNTHETIC_API_ONLY','MORPHEUS_EVAL_MODEL':'nvidia/nemotron-3.5-super-vl-120b-a12b','MORPHEUS_EVAL_API_KEY':'SYNTHETIC_JUDGE_ONLY','MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
    directory=root/'safety'
    with patch('qa.safety_pack.package_versions',return_value=VERSIONS):m,s=make_plan(directory,config,env)
    return directory,m,s,config,env,(directory/'identity.private.key').read_bytes()

def transport(sent,fail=False):
    def send(url,method,headers,body,config):
        sent.append(json.loads(body))
        if fail:return {'status_code':None,'complete':False,'error':'SYNTHETIC connectivity','events':[],'headers':{},'raw_body':''}
        event={'type':'response','response':{'status':'success','message':'SYNTHETIC ANSWER <script>bad()</script>'}}
        return {'status_code':200,'complete':True,'error':None,'events':[{'data':json.dumps(event)}],'headers':{'content-type':'text/event-stream'},'raw_body':json.dumps(event),'first_event_ms':1,'elapsed_ms':3}
    return send

def worker(calls,error=False):
    def run(job,out):
        calls.append(job);ledger=Ledger(job['ledger'])
        for _ in range(job['request_limit']):call=ledger.claim('judge',job['job_id'],job_limit=job['request_limit']);ledger.finish(call,200)
        result={'job_id':job['job_id'],'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile']),'evaluator_version':'3.8.4','generation_parameters':job['profile']['generation_parameters'],'score':None,'completed':False}
        if error:result.update(error='SYNTHETIC failure',exception_type='ValidationError')
        elif job['metric']=='guardrail_review':result.update(completed=True,reviews=[{'decision':'NO_VISIBLE_CONCERN','risk':'none','observation_quote':'SYNTHETIC ANSWER','reason':'SYNTHETIC ONLY'}]*2)
        else:result['score']=0.0
        atomic(out,result);return result
    return run

class SafetyPackTests(unittest.TestCase):
    def test_catalogue_has_controls_multiturn_and_explicit_fixture_gaps(self):
        d=read(DATA);cases=d['cases'];live=[c for c in cases if c['executable']]
        self.assertEqual((len(cases),len(live),sum(len(c['turns']) for c in live)),(114,96,102))
        self.assertEqual(sum(c['category']=='Benign safety controls' for c in live),6)
        self.assertEqual(sum(len(c['turns'])==2 for c in live),6)
        self.assertEqual(len({c['id'] for c in cases}),114)
        self.assertTrue(all(c['scope']=='VISIBLE_RESPONSE_ONLY' and not c.get('writes') and not c.get('payment') for c in live))
        self.assertTrue(all(c['campaign_blockers'] for c in cases if not c['executable']))
    def test_bounded_batch_and_repeat_never_resends_completed_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));sent=[];calls=[]
            with patch('qa.campaign.time.sleep',return_value=None):
                self.assertEqual(execute(d,m,s,c,e,k,1,transport(sent),worker(calls)),2)
                self.assertEqual(execute(d,m,s,c,e,k,1,transport(sent),worker(calls)),2)
            self.assertEqual(len(sent),2);self.assertEqual(len(calls),4)
            self.assertNotEqual(sent[0]['conversationId'],sent[1]['conversationId'])
            self.assertEqual(s['ledger_counts'],{'api':2,'judge':10})
            with patch('qa.safety_pack.package_versions',return_value=VERSIONS):validate(d,m,s,c,e)
            html=(d/'report.html').read_text();self.assertNotIn('<script>bad()</script>',html);self.assertNotIn(e['QA_MSISDN_A'],html)
            results=read(d/'results.json');self.assertIsNone(results['safety_score']);self.assertIsNone(results['safety_pass_rate'])
    def test_evaluator_errors_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));sent=[];calls=[]
            with patch('qa.campaign.time.sleep',return_value=None):
                execute(d,m,s,c,e,k,1,transport(sent),worker(calls,True));execute(d,m,s,c,e,k,1,transport(sent),worker(calls,True))
            self.assertEqual(len(calls),4);self.assertEqual(len(read(d/'results.json')['errors']),4)
    def test_connectivity_stops_after_one_post_and_explicit_resume_does_not_retry_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));sent=[];calls=[]
            execute(d,m,s,c,e,k,96,transport(sent,True),worker(calls))
            self.assertEqual(len(sent),1);self.assertFalse(calls);self.assertEqual(s['cases']['GR-INJ-001']['status'],'BLOCKED')
            with patch('qa.campaign.time.sleep',return_value=None):execute(d,m,s,c,e,k,1,transport(sent),worker(calls))
            self.assertEqual(len(sent),2);self.assertEqual(len(s['cases']['GR-INJ-001']['responses']),1)
    def test_changed_private_binding_and_caps_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));e2={**e,'MORPHEUS_EVAL_API_KEY':'SYNTHETIC_CHANGED'}
            with patch('qa.safety_pack.package_versions',return_value=VERSIONS):
                with self.assertRaises(ValueError):validate(d,m,s,c,e2)
                ledger=Ledger(d/'requests.sqlite',{'api':103,'judge':510},m['expires_at'])
                with self.assertRaises(ValueError):validate(d,m,s,c,e)
    def test_expired_deadline_sends_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));sent=[]
            with patch('qa.campaign.time.time',return_value=m['expires_at']+1):execute(d,m,s,c,e,k,96,transport(sent),worker([]))
            self.assertFalse(sent);self.assertEqual(s['run_status'],'PARTIAL')
    def test_reserved_post_without_marker_is_never_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp));ledger=Ledger(d/'requests.sqlite',{'api':102,'judge':510},m['expires_at']);case=m['cases'][0]
            ledger.claim('api',m['campaign_id']+':'+case['id']+':1')
            engine=SafetyCampaign(d,m,s,c,e,k,lambda *a:self.fail('reserved POST resent'),worker([]));engine.capture_case(case)
            self.assertTrue(s['cases'][case['id']]['uncertain_submission'])
    def test_captured_answer_or_job_tampering_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            d,m,s,c,e,k=fixture(Path(tmp))
            with patch('qa.campaign.time.sleep',return_value=None):execute(d,m,s,c,e,k,1,transport([]),worker([]))
            s['cases']['GR-INJ-001']['responses'][0]['answer']='SYNTHETIC ALTERED'
            with patch('qa.safety_pack.package_versions',return_value=VERSIONS):
                with self.assertRaises(ValueError):validate(d,m,s,c,e)
