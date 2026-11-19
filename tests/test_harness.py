"""Meaningful offline regression tests. No traffic or subscriber credentials used."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from qa.config import ROOT, request_headers, target
from tests.support import IsolatedTestCase, load_fixture as load
from qa.evaluation import evaluate, evidence_scores, ranked_precision
from qa.privacy import redact
from qa.runner import Sessions, blockers, cases_from, expand, make_result, prepare
from qa.reporting import write_report
from qa.transport import SSEParser, extract_answer, request
from scripts.import_request import parse_command, save_env


class ParserTests(IsolatedTestCase):
    def test_fragmented_crlf_and_multiline(self):
        p=SSEParser()
        for s in [': ping\r', '\ndata: one\r', '\ndata: two\r\n', '\r', '\n']:
            p.feed(s)
        self.assertEqual(p.finish(),[{'data':'one\ntwo'}])

    def test_incomplete_event_not_dispatched(self):
        p=SSEParser();p.feed('data: incomplete\n')
        self.assertEqual(p.finish(),[])

    def test_bare_cr(self):
        p=SSEParser();p.feed('event: token\rdata: abc\r\r');p.finish()
        self.assertEqual(p.events,[{'event':'token','data':'abc'}])

    def test_deltas_and_snapshot(self):
        config,_=load()
        events=[{'data':'{"delta":{"content":"hel"}}'}, {'data':'{"delta":{"content":"lo"}}'}, {'data':'{"text":"hello!"}'}, {'data':'[DONE]'}]
        self.assertEqual(extract_answer(events,config),'hello!')

    def test_metadata_not_answer(self):
        self.assertEqual(extract_answer([{'data':'{"conversationId":"ABC"}'}],load()[0]),'')

    def test_invalid_json_preserved_only_explicit_text_events(self):
        self.assertEqual(extract_answer([{'data':'not-json'}],load()[0]),'')
        self.assertEqual(extract_answer([{'event':'delta','data':'text'}],load()[0]),'text')


class FakeResponse:
    status=200
    def __init__(self,chunks,headers=None):
        self.chunks=list(chunks)
        self.headers=headers or [('Content-Type','text/event-stream')]
    def getheaders(self):return self.headers
    def read1(self,n):return self.chunks.pop(0) if self.chunks else b''

class FakeConnection:
    def __init__(self,response):self.response=response;self.sock=None;self.calls=[];self.closed=False
    def request(self,*args,**kwargs):self.calls.append((args,kwargs))
    def getresponse(self):return self.response
    def close(self):self.closed=True

class TransportTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config=load()[0]
    def test_utf8_and_done(self):
        raw='data: {"text":"Sawubona 🌍"}\n\ndata: [DONE]\n\n'.encode()
        cut=raw.index('🌍'.encode())+1
        conn=FakeConnection(FakeResponse([raw[:cut],raw[cut:]]))
        with patch('qa.transport.http.client.HTTPSConnection',return_value=conn):
            r=request('https://qa-serviceauth.mtn.co.za/test','POST',{},b'{}',self.config)
        self.assertEqual(r['answer'],'Sawubona 🌍')
        self.assertTrue(r['done_received']);self.assertTrue(conn.closed)
        self.assertEqual(r['response_sha256'],hashlib.sha256(raw).hexdigest())
    def test_non_json_error_not_swallowed(self):
        conn=FakeConnection(FakeResponse([b'<html>gateway failure</html>'],[('Content-Type','text/html')]))
        conn.response.status=502
        with patch('qa.transport.http.client.HTTPSConnection',return_value=conn):
            r=request('https://qa-serviceauth.mtn.co.za/test','POST',{},b'{}',self.config)
        self.assertEqual(r['status_code'],502);self.assertIn('gateway failure',r['raw_body'])
    def test_failure_is_not_retried(self):
        conn=FakeConnection(FakeResponse([]))
        conn.getresponse=lambda: (_ for _ in ()).throw(TimeoutError('timed out'))
        with patch('qa.transport.http.client.HTTPSConnection',return_value=conn):
            r=request('https://qa-serviceauth.mtn.co.za/test','POST',{},b'{}',self.config)
        self.assertEqual(len(conn.calls),1);self.assertFalse(r['complete']);self.assertTrue(r['error'])
    def test_host_and_tls_allowlist(self):
        for url in ['http://qa-serviceauth.mtn.co.za/test','https://example.com','https://qa-serviceauth.mtn.co.za.evil.test','https://u:p@qa-serviceauth.mtn.co.za']:
            with self.assertRaises(ValueError):request(url,'POST',{},b'{}',self.config)
    def test_size_limit(self):
        conn=FakeConnection(FakeResponse([b'x'*8]))
        with patch('qa.transport.http.client.HTTPSConnection',return_value=conn):
            r=request('https://qa-serviceauth.mtn.co.za/test','POST',{},b'{}',{**self.config,'max_response_bytes':8})
        self.assertEqual(r['error'],'response_size_limit');self.assertFalse(r['complete'])


class EvidenceTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config=load()[0];self.config['response_mapping_confirmed']=True
        self.case={'id':'X','suite':'rag','metrics':['groundedness'],'expected_status':[200]}
        self.response={'attempt_id':'run:X:1','status_code':200,'complete':True,'headers':{'content-type':'text/event-stream'},'events':[{'data':'ok'}],'answer':'ok','response_sha256':'abc','raw_body':'data: ok\n\n'}
        self.evidence={'response_sha256':'abc','evaluator':'reviewer','evaluated_at':'2026-11-14T00:00:00Z','rationale':'Compared claims with actual source.','contexts':[{'id':'doc','text':'Approved evidence'}],'labels':{'grounding_claims_supported':[True]}}
    def test_missing_evidence_inconclusive(self):
        self.assertEqual(evaluate(self.case,[self.response],self.config)[1],'INCONCLUSIVE')
    def test_matching_evidence_passes(self):
        self.assertEqual(evaluate(self.case,[self.response],self.config,{'run:X:1':self.evidence})[1],'PASS')
    def test_mismatched_digest_rejected(self):
        self.evidence['response_sha256']='wrong'
        self.assertEqual(evidence_scores(self.evidence,'abc')[0],{})
    def test_sources_required_for_grounding(self):
        self.evidence['contexts']=[]
        self.assertEqual(evidence_scores(self.evidence,'abc')[0],{})
    def test_critical_fabrication_overrides_aggregate(self):
        self.evidence['critical_fabrication_observed']=True
        self.assertEqual(evaluate(self.case,[self.response],self.config,{'run:X:1':self.evidence})[1],'FAIL')
    def test_disclosure_fails_unrelated_case(self):
        self.evidence['unauthorized_disclosure_observed']=True
        self.assertEqual(evaluate(self.case,[self.response],self.config,{'run:X:1':self.evidence})[1],'FAIL')
    def test_one_turn_does_not_cover_second(self):
        second={**self.response,'attempt_id':'run:X:2'}
        self.assertEqual(evaluate(self.case,[self.response,second],self.config,{'run:X:1':self.evidence})[1],'INCONCLUSIVE')
    def test_authorization_trace_needed(self):
        self.evidence['assessments']={'session_isolation':{'score':1,'method':'manual','reason':'checked'}}
        self.assertNotIn('session_isolation',evidence_scores(self.evidence,'abc')[0])
        self.evidence['authorization_trace']=[{'decision':'deny'}]
        self.assertIn('session_isolation',evidence_scores(self.evidence,'abc')[0])
    def test_nonfinite_and_bool_scores_rejected(self):
        for value in [float('nan'),float('inf'),True,2,-1]:
            self.evidence['assessments']={'answer_relevancy':{'score':value,'method':'judge','reason':'test'}}
            self.assertNotIn('answer_relevancy',evidence_scores(self.evidence,'abc')[0])
    def test_ranked_precision(self):
        self.assertAlmostEqual(ranked_precision([True,False,True]),(1+2/3)/2)
        self.assertEqual(ranked_precision([False,False]),0)
        self.assertIsNone(ranked_precision([]))
    def test_error_sse_json_contract_observation(self):
        r={**self.response,'status_code':422,'raw_body':'{"detail":[]}'}
        case={'id':'API-X','suite':'smoke','expected_status':[422]}
        self.assertIn('plain JSON',evaluate(case,[r],self.config)[3][0])
    def test_sse_error_event_fails(self):
        self.response['events']=[{'event':'error','data':'failure'}]
        self.assertEqual(evaluate(self.case,[self.response],self.config)[1],'FAIL')


class ConfigAndSecurityTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        self.config=load()[0]
        self.env={'QA_MSISDN_A':'TEST_A','QA_MSISDN_B':'TEST_B','QA_CONVERSATION_ID_A':'session-a', 'QA_CONVERSATION_ID_B':'session-b', 'QA_SUBSCRIBER_TYPE_A':'POSTPAID'}
    def test_payload_tampering_retains_authentication(self):
        case=next(c for c in cases_from() if c['id']=='SAFE-PII-005')
        self.env['QA_HEADERS_A_JSON']='{"Authorization":"Bearer actor-a"}'
        steps=prepare(case,self.env,self.config,Sessions(self.env,self.config),json.loads((ROOT/'config/payload.template.json').read_text()))
        self.assertEqual(steps[0]['body']['msisdn'],'TEST_A');self.assertEqual(steps[1]['body']['msisdn'],'TEST_B')
        self.assertEqual(steps[0]['body']['conversationId'],steps[1]['body']['conversationId'])
        self.assertEqual(steps[0]['headers'],steps[1]['headers'])
    def test_session_ids_not_reused_across_cases(self):
        sessions=Sessions(self.env,self.config)
        self.assertEqual(sessions.take('A'),'session-a')
        with self.assertRaises(ValueError):sessions.take('A')
    def test_missing_other_account_blocks(self):
        case=next(c for c in cases_from() if c['id']=='SAFE-PII-001')
        self.env.pop('QA_MSISDN_B')
        self.assertTrue(blockers(case,self.env,self.config,False))
    def test_same_auth_blocks_distinct_principals(self):
        case=next(c for c in cases_from() if c['id']=='SAFE-PII-006')
        self.assertTrue(blockers(case,self.env,self.config,False))
        self.env['QA_API_KEY_B']='actor-b-key'
        self.assertFalse(blockers(case,self.env,self.config,False))
    def test_write_needs_both_fixture_and_explicit_flag(self):
        c={'suite':'transactions','writes':True}
        self.assertTrue(blockers(c,self.env,self.config,True))
        self.config['writes_fixture_ready']=True
        self.assertTrue(blockers(c,self.env,self.config,False))
        self.assertFalse(blockers(c,self.env,self.config,True))
    def test_header_casing_overrides_not_duplicates(self):
        headers=request_headers({'MORPHEUS_COOKIE':'old','QA_HEADERS_A_JSON':'{"cookie":"new"}'})
        self.assertEqual(headers['cookie'],'new');self.assertNotIn('Cookie',headers)
    def test_key_url_encoded(self):
        u=target(self.config,{'MORPHEUS_API_KEY':'a+b/&'})
        self.assertIn('key=a%2Bb%2F%26',u)
    def test_unresolved_template_rejected(self):
        with self.assertRaises(ValueError):expand('{{missing}}',{})
    def test_catalogue_covers_all_modules(self):
        cases=cases_from();self.assertGreaterEqual(len(cases),150)
        covered={r for c in cases for r in c.get('requirements',[])}
        self.assertTrue({f'FR-{i:02d}' for i in range(1,19)} <= covered)
    def test_import_command_keeps_last_duplicate_cookie(self):
        cmd="postman request POST 'https://qa-serviceauth.mtn.co.za/morpheus-v5/api/prepaid/text?platform=whatsapp&key=secret-test' --header 'Cookie: cookie=old; cookie=new; analytics=x' --body '{\"conversationId\":\"conv\",\"msisdn\":\"TEST_A\",\"subscriberType\":\"POSTPAID\",\"message\":\"My balances\"}'"
        env=parse_command(cmd)
        self.assertEqual(env['MORPHEUS_COOKIE'],'cookie=new; analytics=x')
        self.assertEqual(env['QA_SUBSCRIBER_TYPE_A'],'POSTPAID')
        self.assertEqual(env['MORPHEUS_API_KEY'],'secret-test')
    def test_env_literal_never_executes_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            save_env({'MORPHEUS_API_KEY':'$(echo hello) $HOME `id`'},root)
            self.assertIn('$(echo hello)',(root/'.env').read_text())
            self.assertEqual((root/'.env').stat().st_mode & 0o777,0o600)
    def test_redaction_masks_secrets_in_raw_response(self):
        r=redact({'raw_body':'PUK: 12345678; number +27831234567; key=token-secret; bob@example.com','Cookie':'cookie','msisdn':'TEST_A'},['token-secret'])
        for secret in ['12345678','27831234567','token-secret','bob@example.com']:
            self.assertNotIn(secret,json.dumps(r))
    def test_report_html_is_escaped_and_secrets_redacted(self):
        c={'id':'X','suite':'baseline','category':'x'}
        response={'raw_body':'<script>alert(1)</script> secret_value','status_code':200}
        with tempfile.TemporaryDirectory() as directory:
            write_report(directory,[make_result(c,responses=[response])],{'mode':'demo'},['secret_value'])
            text=(Path(directory)/'report.html').read_text()
            self.assertNotIn('<script>',text);self.assertNotIn('secret_value',text)
            self.assertIn('&lt;script&gt;',text)

if __name__=='__main__':
    unittest.main()
