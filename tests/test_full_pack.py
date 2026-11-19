"""Synthetic campaign recovery, caps, output contracts and private-data boundaries."""
import asyncio
import copy
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from qa.config import ROOT
from qa.campaign import Campaign,application
from qa.campaign_plan import build_manifest
from qa.campaign_io import Ledger,BudgetStop,fingerprint,immutable
from qa.campaign_report import exports
from qa.campaign_privacy import sanitize
from qa.deepeval_adapter import SchemaClient,JudgeOutputError


def fake_response(url,method,headers,body,config):
    raw='data: '+json.dumps({'type':'response','response':{'status':'success','message':'SYNTHETIC ANSWER <img src=x onerror=alert(1)>'}})+'\n\n'
    return {'status_code':200,'complete':True,'error':None,'headers':{'content-type':'text/event-stream'},'raw_body':raw,
        'events':[{'data':json.dumps({'type':'loader','message':'progress only'})},{'data':raw.split('data: ',1)[1].strip()}],
        'response_sha256':hashlib.sha256(raw.encode()).hexdigest(),'elapsed_ms':40,'first_byte_ms':10,'first_event_ms':15}


def case(identifier='SYNTHETIC-001',turns=1):
    return {'id':identifier,'suite':'functional','category':'SYNTHETIC','turns':[{'message':'SYNTHETIC question'} for _ in range(turns)],
        'expected':'SYNTHETIC only','expected_status':[200],'metrics':[],'executable':True,'campaign_blockers':[],'batch':0,'workload':'single_tool'}


class FullPackTests(unittest.TestCase):
    def setup_campaign(self,path,cases=None,transport=fake_response,worker=None,api=10,judge=20):
        config=json.loads((ROOT/'config/settings.json').read_text());config.update(response_mapping_confirmed=True,min_interval_seconds=0)
        env={'QA_MSISDN_A':'SYNTHETIC_SUBSCRIBER','MORPHEUS_API_KEY':'SYNTHETIC_CREDENTIAL','MORPHEUS_EVAL_MODEL':'nvidia/nemotron-3.5-super-vl-120b-a12b', 'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]','MORPHEUS_EVAL_API_KEY':'SYNTHETIC_EVALUATOR'}
        args=SimpleNamespace(mode='execute',max_api_requests=api,max_judge_requests=judge,max_seconds=600)
        manifest=build_manifest(config,env,b'SYNTHETIC KEY','SYNTHETIC-CAMPAIGN','SYNTHETIC-TIMESTAMP',args)
        manifest['cases']=cases or [case()]
        immutable(path/'manifest.json',manifest)
        Ledger(path/'requests.sqlite',{'api':api,'judge':judge},time.time()+600)
        state={'run_status':'PLANNED','updated_at':'SYNTHETIC','expires_at':time.time()+600,'cases':{},'retry_evaluators':False}
        if worker is None:
            def worker(job,out):
                return {'score':0.8,'method':'SYNTHETIC STUB ONLY','material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
        return Campaign(path,manifest,state,config,env,b'SYNTHETIC KEY',transport,worker)

    def test_shared_hard_budget_does_not_reset_across_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'db';one=Ledger(p,{'judge':2},time.time()+60);two=Ledger(p)
            one.claim('judge','one');two.claim('judge','two')
            with self.assertRaises(BudgetStop):one.claim('judge','three')
            Ledger(p,{'judge':999},time.time()+999)
            with self.assertRaises(BudgetStop):two.claim('judge','four')

    def test_per_metric_batch_and_elapsed_caps(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'db';ledger=Ledger(p,{'api':20,'judge':20},time.time()+60)
            ledger.claim('judge','job',job_limit=1)
            with self.assertRaises(BudgetStop):ledger.claim('judge','job',job_limit=1)
            ledger.claim('api','a',batch='0',batch_limit=1)
            with self.assertRaises(BudgetStop):ledger.claim('api','b',batch='0',batch_limit=1)
            with ledger.connect() as db:db.execute('UPDATE caps SET expires=0')
            with self.assertRaises(BudgetStop):ledger.claim('api','c')

    def test_completed_work_skipped_on_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[];jobs=[]
            def transport(*a):calls.append(a);return fake_response(*a)
            c=self.setup_campaign(Path(tmp),transport=transport)
            worker=c.worker
            def evaluate(job,out):jobs.append(job);return worker(job,out)
            c.worker=evaluate;c.run();c.run()
            self.assertEqual(len(calls),1);self.assertEqual(len(jobs),2)
            self.assertEqual(c.ledger.counts()['api'],1)
            self.assertEqual(c.state['run_status'],'COMPLETE')

    def test_inflight_post_never_resent_and_later_turns_not_sent(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.setup_campaign(Path(tmp),cases=[case(turns=2)],transport=lambda *a:(_ for _ in ()).throw(AssertionError('must not resend')))
            record=c.start_case(c.manifest['cases'][0]);record['inflight']={'path':'responses/unknown.json','attempt_id':'SYNTHETIC'}
            c.capture_case(c.manifest['cases'][0])
            self.assertTrue(record['uncertain_submission']);self.assertTrue(record['capture_done'])
            self.assertEqual(record['status'],'INCONCLUSIVE')

    def test_multiturn_same_session_independent_cases_new_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            sent=[]
            def capture(*a):sent.append(json.loads(a[3]));return fake_response(*a)
            c=self.setup_campaign(Path(tmp),cases=[case('SYNTHETIC-A',2),case('SYNTHETIC-B')],transport=capture)
            c.run()
            self.assertEqual(sent[0]['conversationId'],sent[1]['conversationId'])
            self.assertNotEqual(sent[1]['conversationId'],sent[2]['conversationId'])
            self.assertNotIn('SYNTHETIC_SUBSCRIBER',(Path(tmp)/'report.html').read_text())
            self.assertNotIn(sent[0]['conversationId'],(Path(tmp)/'report.html').read_text())

    def test_http200_business_error_fails_and_loader_is_not_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            def error(*a):
                r=fake_response(*a);r['events'][-1]['data']=json.dumps({'type':'response','response':{'status':'error','message':'SYNTHETIC BUSINESS ERROR'}});return r
            c=self.setup_campaign(Path(tmp),transport=error);c.capture_case(c.manifest['cases'][0])
            rec=c.state['cases']['SYNTHETIC-001'];self.assertEqual(rec['status'],'FAIL');self.assertEqual(rec['responses'][0]['application_status'],'error')
            self.assertEqual(rec['responses'][0]['answer'],'SYNTHETIC BUSINESS ERROR')

    def test_missing_fixtures_and_source_conflicts_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.setup_campaign(Path(tmp));c.manifest['cases']=[x for x in build_manifest(c.config,c.env,b'x','s','t',SimpleNamespace(mode='plan',max_api_requests=600,max_judge_requests=6000,max_seconds=86400))['cases']]
            self.assertEqual(sum(x['suite']=='document_faq' for x in c.manifest['cases']),432)
            self.assertTrue(all(not x['executable'] for x in c.manifest['cases'] if x.get('writes') or x.get('payment') or x['suite']=='isolation'))
            self.assertTrue(any(not x['executable'] and x.get('faq_reference') for x in c.manifest['cases']))

    def test_report_escaping_fact_preservation_and_toxicity_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.setup_campaign(Path(tmp));c.run()
            text=(Path(tmp)/'report.html').read_text()
            self.assertNotIn('<img src=x onerror=alert(1)>',text);self.assertIn('&lt;img',text)
            self.assertNotIn('https://cdn',text)
            cleaned=sanitize('R1234567 and 4096 tokens; subscriber 0821234567; secret=TOKEN',['TOKEN'])
            self.assertIn('R1234567',cleaned);self.assertIn('4096',cleaned);self.assertNotIn('0821234567',cleaned)
            self.assertEqual(c.manifest['metric_definitions']['deepeval_toxicity']['direction'],'lower')
            self.assertIsNone(c.manifest['metric_definitions']['deepeval_toxicity']['threshold'])

    def test_authentication_and_connectivity_breakers(self):
        for status,limit in ((401,2),(None,3)):
            with tempfile.TemporaryDirectory() as tmp:
                calls=[]
                def fail(*a):
                    calls.append(1);r=fake_response(*a);r.update(status_code=status,error='SYNTHETIC NETWORK' if status is None else None);return r
                c=self.setup_campaign(Path(tmp),cases=[case('SYNTHETIC-'+str(i)) for i in range(6)],transport=fail)
                c.run();self.assertEqual(len(calls),limit);self.assertEqual(c.state['run_status'],'PARTIAL')

    def test_malformed_truncated_schema_and_http_failure_never_score(self):
        class Schema:
            @staticmethod
            def model_json_schema():return {'type':'object'}
            @staticmethod
            def model_validate_json(value):return json.loads(value)
        profile={'model':'nvidia/synthetic','generation_parameters':{'max_tokens':4096,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}}
        env={'MORPHEUS_EVAL_MODEL':'nvidia/synthetic','MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]'}
        for reason,content in (('length','{}'),('stop','not json')):
            client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason,message=SimpleNamespace(content=content))]))))
            a=SchemaClient(env,profile,None,'SYNTHETIC',client=client)
            with self.assertRaises((ValueError,JudgeOutputError)):a.generate('SYNTHETIC',Schema)
        good=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='{"score":1}'))])
        a=SchemaClient(env,profile,None,'SYNTHETIC',client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:good))))
        self.assertEqual(a.generate('SYNTHETIC',Schema),{'score':1})

    def test_evaluator_failure_preserved_without_automatic_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def failure(job,out):calls.append(job);raise ValueError('SYNTHETIC private input must never be printed')
            c=self.setup_campaign(Path(tmp),worker=failure);c.run();c.run()
            self.assertEqual(len(calls),2)
            self.assertTrue(all(v[-1]['score'] is None for v in c.state['cases']['SYNTHETIC-001']['metrics'].values()))
            self.assertNotIn('private input',(Path(tmp)/'report.html').read_text())

    def test_budget_stop_generates_partial_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=self.setup_campaign(Path(tmp),cases=[case('SYNTHETIC-A'),case('SYNTHETIC-B')],api=1)
            c.run();self.assertEqual(c.state['run_status'],'PARTIAL');self.assertTrue((Path(tmp)/'report.html').exists())

    def test_provider_retry_after_and_explicit_recovery_keep_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger=Ledger(Path(tmp)/'db',{'judge':5},time.time()+60)
            call=ledger.claim('judge','SYNTHETIC');ledger.finish(call,429,'5')
            self.assertEqual(ledger.provider_stop()[0],429)
            with self.assertRaises(BudgetStop):ledger.acknowledge_recovery()
            with ledger.connect() as db:db.execute('UPDATE calls SET started=started-10,finished=finished-10')
            ledger.acknowledge_recovery();self.assertIsNone(ledger.provider_stop());self.assertEqual(ledger.counts()['judge'],1)

    def test_sensitive_numeric_sse_tool_fields_redacted_without_losing_prices(self):
        raw='data: {"puk":12345678,"accountNumber":"987654321","response":{"message":"Your PUK is 12345678. The fee is R1234567 and 4096 units."}}\n\n'
        text=sanitize(raw)
        self.assertNotIn('12345678',text);self.assertNotIn('987654321',text)
        self.assertIn('R1234567',text);self.assertIn('4096',text)

    def test_metric_binding_and_invalid_scores_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            def wrong(job,out):return {'score':1.0,'material_sha256':'wrong','profile_sha256':'wrong'}
            c=self.setup_campaign(Path(tmp),worker=wrong);c.run()
            self.assertTrue(all(v[-1]['score'] is None for v in c.state['cases']['SYNTHETIC-001']['metrics'].values()))

    def test_interrupted_metric_is_not_silently_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls=[]
            def interrupt(job,out):calls.append(job);raise KeyboardInterrupt
            c=self.setup_campaign(Path(tmp),worker=interrupt);c.run();self.assertEqual(c.state['run_status'],'INTERRUPTED')
            # Resume considers the first worker uncertain, skips it, then executes
            # only the independent remaining metric.
            def worker(job,out):calls.append(job);return {'score':0.5,'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
            c.worker=worker;c.run();self.assertEqual(len(calls),2)
            histories=c.state['cases']['SYNTHETIC-001']['metrics']
            self.assertIn('Interrupted evaluator',histories['deepeval_answer_relevancy'][0]['error'])

    def test_observed_critical_evidence_overrides_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);c=self.setup_campaign(path);c.capture_case(c.manifest['cases'][0])
            rec=c.state['cases']['SYNTHETIC-001'];response=rec['responses'][0]
            data={'attempts':[{'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],
                'evaluator':'SYNTHETIC REVIEWER','evaluated_at':'SYNTHETIC TIME','rationale':'SYNTHETIC critical evidence test',
                'critical_fabrication_observed':True}]}
            (path/'evidence.json').write_text(json.dumps(data));c.state['evidence_import']='evidence.json'
            c.apply_evidence(c.manifest['cases'][0]);self.assertEqual(rec['status'],'FAIL');self.assertTrue(rec['critical_observed'])

    def test_real_cli_resume_rejects_manifest_and_configuration_drift(self):
        from qa.campaign import main
        from qa.campaign_plan import build_manifest as original_builder
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'campaign'
            config=json.loads((ROOT/'config/settings.json').read_text());config.update(response_mapping_confirmed=True,min_interval_seconds=0)
            env={'QA_MSISDN_A':'SYNTHETIC_SUBSCRIBER','MORPHEUS_API_KEY':'SYNTHETIC_CREDENTIAL','MORPHEUS_EVAL_MODEL':'nvidia/nemotron-3.5-super-vl-120b-a12b', 'MORPHEUS_EVAL_BASE_URL':'https://synthetic.invalid/v1','MORPHEUS_EVAL_ALLOWED_HOSTS_JSON':'["synthetic.invalid"]','MORPHEUS_EVAL_API_KEY':'SYNTHETIC_EVALUATOR'}
            versions={'ragas':'0.4.3','deepeval':'3.8.4','openai':'2.54.0','httpx':'0.28.1'}
            def builder(*a):
                value=original_builder(*a);value['cases']=[case()];return value
            def worker(self,job,out):return {'score':0.5,'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
            with patch('qa.campaign.load',return_value=(config,env)),patch('qa.campaign_plan.package_versions',return_value=versions),patch('qa.campaign.package_versions',return_value=versions),patch('qa.campaign.build_manifest',side_effect=builder),patch('qa.campaign.request',side_effect=fake_response) as network,patch.object(Campaign,'worker_process',worker):
                self.assertEqual(main(['execute','--out',str(out),'--allow-draft-references']),0)
                self.assertEqual(main(['resume','--campaign',str(out),'--allow-draft-references']),0)
                self.assertEqual(network.call_count,1)
                config['min_interval_seconds']=0.1
                with self.assertRaises(ValueError):main(['resume','--campaign',str(out),'--allow-draft-references'])
                config['min_interval_seconds']=0
                manifest=out/'manifest.json';manifest.chmod(0o644);data=json.loads(manifest.read_text());data['campaign_id']='CHANGED';manifest.write_text(json.dumps(data))
                with self.assertRaises(ValueError):main(['resume','--campaign',str(out),'--allow-draft-references'])

    def test_actual_trace_gates_conditional_metrics_and_never_uses_references(self):
        for mismatch in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp);jobs=[];scenario=case()
                data=json.loads((ROOT/'datasets/business_faq_v1.json').read_text())['cases'][0]
                scenario.update(suite='document_faq',faq_reference=data)
                c=self.setup_campaign(path,cases=[scenario]);c.capture_case(scenario)
                response=c.state['cases'][scenario['id']]['responses'][0]
                trace={'attempts':[{'attempt_id':response['attempt_id'],'response_sha256':'0'*64 if mismatch else response['response_sha256'],
                    'source_system':'azure_search','trace_id':'SYNTHETIC TRACE','retrieved_at':'SYNTHETIC TIME','exported_by':'SYNTHETIC EXPORTER','correlation_basis':'SYNTHETIC EXACT ATTEMPT',
                    'contexts':[{'id':'SYNTHETIC CHUNK','rank':1,'text':'SYNTHETIC ACTUAL SEARCH PASSAGE'}]}]}
                (path/'trace.json').write_text(json.dumps(trace));c.state['trace_import']='trace.json'
                def worker(job,out):
                    jobs.append(job);return {'score':0.5,'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
                c.worker=worker;c.grade_case(scenario)
                names=[j['metric'] for j in jobs]
                if mismatch:
                    self.assertNotIn('ragas_faithfulness',names)
                    self.assertTrue(c.state['cases'][scenario['id']]['trace_import_error'])
                else:
                    self.assertIn('ragas_faithfulness',names);self.assertIn('deepeval_context_precision',names)
                    for job in jobs:
                        if job['metric']=='ragas_faithfulness':self.assertEqual(job['material']['retrieved_contexts'],['SYNTHETIC ACTUAL SEARCH PASSAGE'])
                    self.assertNotIn('Exact-attempt retrieved chunks unavailable; context precision/recall/faithfulness remain null.',c.state['cases'][scenario['id']]['missing_evidence'])

    def test_csv_export_neutralizes_captured_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp);c=self.setup_campaign(path);c.run()
            record=c.state['cases']['SYNTHETIC-001']
            record['metrics']['custom_coverage_review'][0].update(reason='=HYPERLINK("https://synthetic.invalid")',candidate_flags={'disclosure_candidate':True})
            exports(path,c.manifest,c.state,c.private)
            import csv
            with (path/'defects.csv').open() as f:rows=list(csv.DictReader(f))
            match=next(r for r in rows if r['category']=='candidate critical LLM finding')
            self.assertTrue(match['description'].startswith("'="))
