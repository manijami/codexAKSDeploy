"""Linked continuation: exact scope, shared ceilings, private binding and no replay."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qa.config import ROOT
from qa.campaign_io import Ledger,atomic,immutable,fingerprint
from qa.campaign_plan import build_manifest
from qa.faq_pilot import digest
from qa.evaluator_recovery import source_inventory,audit_source,execute as recover_execute,OWN_FILES as RECOVERY_FILES
from qa.linked_continuation import (audit_completed_recovery,pending_cases,make_manifest,
    execute,validate_new_ledger,validate_checkpoint,valid_records,own_fingerprints,PendingCampaign)
from test_quota_recovery import fixture as quota_fixture


def parents(root):
    source,config,env=quota_fixture(root)
    versions={'deepeval':'3.8.4','ragas':'0.4.3'}
    with patch('qa.campaign_plan.package_versions',return_value=versions):
        full=build_manifest(config,env,b'SYNTHETIC KEY','SYNTHETIC-CAMPAIGN','SYNTHETIC TIME',SimpleNamespace(mode='execute',max_api_requests=600,max_judge_requests=6000,max_seconds=86400))
    state=json.loads((source/'checkpoint.json').read_text());previous=state['cases']
    faq=[c for c in full['cases'] if c['suite']=='document_faq' and c['executable']]
    pending_ids={c['id'] for c in faq[48:]}|{'PACK-MEMORY-001','PACK-MEMORY-002'}
    assert len(pending_ids)==107
    for case in full['cases']:
        if case['id'] in previous:continue
        previous[case['id']]={'status':'NOT_RUN' if case['id'] in pending_ids else 'BLOCKED','responses':[],'payloads':[],'metrics':[], 'reasons':[],'missing_evidence':[]}
        previous[case['id']]['metrics']={}
    (source/'manifest.json').chmod(0o644);(source/'manifest.json').write_text(json.dumps(full));(source/'manifest.json').chmod(0o444)
    state['manifest_sha256']=digest(source/'manifest.json');atomic(source/'checkpoint.json',state)
    original,state,jobs,selected,_,caps,counts,valid=audit_source(source,check_runtime=False)
    recovery=root/'recovery';recovery.mkdir()
    plan={'source_campaign_id':original['campaign_id'],'original_manifest_sha256':digest(source/'manifest.json'),
        'recovery_code_sha256':{name:digest(ROOT/name) for name in RECOVERY_FILES},
        'source_directory':str(source),'source_files_sha256':source_inventory(source),'selected':selected,
        'original_ledger_counts':counts,'original_caps':caps,'expires_at':state['expires_at'],'evaluator':original['evaluator'],'max_additional_judge_requests':3}
    immutable(recovery/'recovery_manifest.json',plan)
    def worker(job,out,seconds):
        immutable(out.parent.parent/'jobs'/(job['job_id'].replace(':','_')+'.json'),job)
        ledger=Ledger(job['ledger'])
        for _ in range(3):call=ledger.claim('judge',job['job_id'],job_limit=3);ledger.finish(call,200)
        result={'score':1.0,'method':'SYNTHETIC NATIVE MOCK','job_id':job['job_id'],'material_sha256':job['material_sha256'],
            'profile_sha256':fingerprint(job['profile']),'generation_parameters':job['profile']['generation_parameters'],'evaluator_version':'3.8.4'}
        atomic(out,result);return result
    with patch('qa.evaluator_recovery.load',return_value=(config,env)):
        assert recover_execute(recovery,plan,original,state,jobs,worker)==0
    audit=audit_completed_recovery(source,recovery,check_runtime=False)
    return source,recovery,config,env,audit


def make(root,small=False):
    source,recovery,config,env,audit=parents(root)
    original,state,baseline,recovered,remaining,caps,counts,_=audit
    pending=pending_cases(original,state,source)
    if small:pending=pending[:2]+pending[-2:]
    out=root/'continuation';out.mkdir();key=b'SYNTHETIC PRIVATE CONTINUATION KEY';(out/'identity.private.key').write_bytes(key)
    manifest=make_manifest(original,state,baseline,recovered,remaining,caps,counts,pending,source,recovery,key,config,env)
    immutable(out/'manifest.json',manifest);(out/'dataset.snapshot.json').write_bytes((source/'dataset.snapshot.json').read_bytes())
    checkpoint={'manifest_sha256':digest(out/'manifest.json'),'expires_at':state['expires_at'],'run_status':'PLANNED_NOT_EXECUTED','updated_at':'SYNTHETIC',
        'ledger_counts':counts,'retry_evaluators':False,'cases':{c['id']:{'status':'NOT_RUN','responses':[],'payloads':[],'metrics':{},'reasons':[],'missing_evidence':[]} for c in pending}}
    atomic(out/'checkpoint.json',checkpoint)
    return out,manifest,original,baseline,recovered,remaining,config,env


def transport_factory(sent,manifest):
    lookup={c['turns'][0]['message']:c for c in manifest['cases']}
    def send(url,method,headers,body,config):
        payload=json.loads(body);sent.append(payload)
        answer='SYNTHETIC ANSWER <script>bad()</script>'
        if 'What synthetic word did I ask you to remember in this conversation?'==payload['message']:
            answer=next(c['canary_expected'] for c in manifest['cases'] if c.get('canary_expected'))
        raw='data: '+json.dumps({'type':'response','response':{'status':'success','message':answer}})+'\n\n'
        import hashlib
        return {'status_code':200,'complete':True,'error':None,'headers':{'content-type':'text/event-stream','x-conversation-id':payload['conversationId']},
            'events':[{'data':raw[6:].strip()}],'raw_body':raw,'response_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'first_byte_ms':1,'first_event_ms':2,'elapsed_ms':3}
    return send


def metric_mock(calls):
    def evaluate(job,out):
        calls.append(job)
        immutable(out.parent.parent/'jobs'/(job['job_id'].replace(':','_')+'.json'),job)
        ledger=Ledger(job['ledger'])
        for _ in range(job['request_limit']):call=ledger.claim('judge',job['job_id'],job_limit=job['request_limit']);ledger.finish(call,200)
        result={'score':1.0,'reason':'SYNTHETIC ONLY','method':'SYNTHETIC NATIVE MOCK','job_id':job['job_id'],
            'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile']),
            'generation_parameters':job['profile']['generation_parameters'],'evaluator_version':'0.4.3' if job['metric'].startswith('ragas_') else '3.8.4'}
        atomic(out,result);return result
    return evaluate


class LinkedContinuationTests(unittest.TestCase):
    def test_exact_107_pending_selection_and_complete_history_exclusion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,recovery,config,env,audit=parents(Path(tmp));m,s,b,rec,rem,caps,counts,valid=audit
            selected=pending_cases(m,s,source)
            self.assertEqual(len(selected),107)
            self.assertEqual(sum(c['suite']=='document_faq' for c in selected),105)
            self.assertNotIn('FAQ-DOC-21-03',[c['id'] for c in selected])
            self.assertEqual(counts,{'api':1,'judge':4})
            self.assertEqual(len(valid_records(b)),1)
            victim=selected[0]['id'];s['cases'][victim]['attempted']=True
            with self.assertRaises(ValueError):pending_cases(m,s,source)

    def test_serial_capture_fresh_uuid_retained_canary_and_repeat_skips_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True);sent=[];jobs=[]
            parents_before={k:source_inventory(Path(v['directory'])) for k,v in m['parents'].items()}
            with patch('qa.linked_continuation.load',return_value=(config,env)),patch('qa.campaign.time.sleep',return_value=None):
                self.assertEqual(execute(out,m,old,b,rec,rem,transport_factory(sent,m),metric_mock(jobs)),0)
                self.assertEqual(execute(out,m,old,b,rec,rem,transport_factory(sent,m),metric_mock(jobs)),0)
            self.assertEqual(len(sent),5);self.assertEqual(len(jobs),12)
            self.assertEqual(sent[2]['conversationId'],sent[3]['conversationId'])
            self.assertEqual(len({x['conversationId'] for x in sent}),4)
            self.assertNotEqual(sent[3]['conversationId'],sent[4]['conversationId'])
            for k,v in m['parents'].items():self.assertEqual(source_inventory(Path(v['directory'])),parents_before[k])
            summary=json.loads((out/'continuation_summary.json').read_text())
            self.assertEqual(summary['qa_requests_added'],5);self.assertEqual(summary['judge_requests_added'],24)
            self.assertEqual(summary['preserved_valid_metric_results'],1)
            html=(out/'report.html').read_text();self.assertNotIn('<script>bad()</script>',html);self.assertNotIn(env['QA_MSISDN_A'],html)
            self.assertIn('ORIGINAL + RECOVERED',html);self.assertIn('CONTINUATION',html)

    def test_expired_original_deadline_never_sends(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True)
            with patch('qa.linked_continuation.load',return_value=(config,env)),patch('qa.campaign_io.time.time',return_value=m['original_expires_at']+1):
                self.assertEqual(execute(out,m,old,b,rec,rem,lambda *a:self.fail('deadline bypass'),metric_mock([])),2)
            self.assertEqual(json.loads((out/'checkpoint.json').read_text())['run_status'],'BLOCKED_ORIGINAL_DEADLINE_EXPIRED')

    def test_rotated_again_or_changed_binding_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True);env['MORPHEUS_EVAL_API_KEY']='SYNTHETIC_DIFFERENT_AGAIN'
            with patch('qa.linked_continuation.load',return_value=(config,env)):
                with self.assertRaises(ValueError):execute(out,m,old,b,rec,rem,lambda *a:self.fail('wrong credential accepted'),metric_mock([]))

    def test_parent_manifest_or_recovery_score_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True)
            recovery=Path(m['parents']['recovery']['directory']);p=recovery/'derived_checkpoint.json';data=json.loads(p.read_text())
            data['cases']['FAQ-DOC-21-03']['metrics']['deepeval_answer_relevancy'][-1]['score']=0
            atomic(p,data)
            with patch('qa.linked_continuation.load',return_value=(config,env)):
                with self.assertRaises(ValueError):execute(out,m,old,b,rec,rem)

    def test_cap_changes_rejected_without_any_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True)
            with patch('qa.linked_continuation.load',return_value=(config,env)),patch('qa.campaign.time.sleep',return_value=None):
                execute(out,m,old,b,rec,rem,transport_factory([],m),metric_mock([]))
            with Ledger(out/'requests.sqlite').connect() as db:db.execute("UPDATE caps SET maximum=7000 WHERE kind='judge'")
            with self.assertRaises(ValueError):validate_new_ledger(out,m)

    def test_uncertain_reserved_post_not_resent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out,m,old,b,rec,rem,config,env=make(Path(tmp),small=True)
            import sqlite3
            with sqlite3.connect(Path(m['parents']['recovery']['directory'])/'requests.sqlite') as source,sqlite3.connect(out/'requests.sqlite') as destination:source.backup(destination)
            cid=m['selected_test_ids'][0];ledger=Ledger(out/'requests.sqlite');ledger.claim('api',m['campaign_id']+':'+cid+':1',batch='SYNTHETIC')
            state=json.loads((out/'checkpoint.json').read_text())
            engine=PendingCampaign(out,m,state,config,env,b'SYNTHETIC',old,b,rec,rem,lambda *a:self.fail('uncertain POST resent'),metric_mock([]))
            engine.capture_case(m['cases'][0]);self.assertTrue(state['cases'][cid]['uncertain_submission'])

    def test_original_failure_cases_cannot_enter_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            source,recovery,config,env,audit=parents(Path(tmp));m,s,*_=audit
            case=next(c for c in m['cases'] if c['id']=='FAQ-DOC-21-03');s['cases'][case['id']]['status']='NOT_RUN'
            with self.assertRaises(ValueError):pending_cases(m,s,source)
