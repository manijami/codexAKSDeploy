"""Synthetic expanded-capture bindings and native evaluator CLI guards."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.config import ROOT
from qa.faq_pilot import digest
from qa.faq_catalogue_evaluation import load_catalogue_run
from scripts import evaluate_faq_ragas as judge

PROFILE=ROOT/'reports/faq_expansion/first_live_selection/evaluator_profile.json'
DATA=ROOT/'datasets/business_faq_v1.json'


def synthetic_capture(path):
    """Explicit synthetic response; never impersonates a saved Morpheus attempt."""
    (path/'dataset.snapshot.json').write_bytes(DATA.read_bytes())
    data=json.loads(DATA.read_text());cases=data['cases'][:10]
    (path/'selection.json').write_text(json.dumps({'test_ids':[c['test_id'] for c in cases]}))
    results={'metadata':{'mode':'live','config':{'response_mapping_confirmed':True},
        'faq_catalogue_dataset_sha256':digest(path/'dataset.snapshot.json'),
        'faq_catalogue_selection_sha256':digest(path/'selection.json')},'results':[]}
    for c in cases:
        results['results'].append({'test_id':c['test_id'],'request_payload':[{'message':c['question'],'msisdn':'SYNTHETIC_SUBSCRIBER'}],
            'actual_api_response':[{'attempt_id':'SYNTHETIC:'+c['test_id'],'response_sha256':hashlib.sha256(b'SYNTHETIC ANSWER').hexdigest(),
             'answer':'SYNTHETIC ANSWER','status_code':200,'complete':True,'error':None}]})
    (path/'results.json').write_text(json.dumps(results))
    return results


class ExpandedEvaluationTests(unittest.TestCase):
    def test_loader_uses_frozen_refs_not_current_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);synthetic_capture(p)
            _,rows=load_catalogue_run(p)
            self.assertEqual(len(rows),10)
            self.assertTrue(all(r['eligible_response'] for r in rows))
            self.assertTrue(all(r['retrieved_contexts'] is None and all(v is None for v in r['metric_scores'].values()) for r in rows))
            self.assertNotIn('msisdn',rows[0])
            self.assertEqual(rows[0]['reference'],json.loads(DATA.read_text())['cases'][0]['reference_answer'])

    def test_selection_snapshot_and_attempt_mismatches_rejected(self):
        for change in ('question','duplicate_attempt','selection','snapshot'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                p=Path(tmp);r=synthetic_capture(p)
                if change=='question':r['results'][0]['request_payload'][0]['message']='Wrong question'
                if change=='duplicate_attempt':r['results'][1]['actual_api_response'][0]['attempt_id']=r['results'][0]['actual_api_response'][0]['attempt_id']
                if change=='selection':r['results'].reverse()
                if change=='snapshot':r['metadata']['faq_catalogue_dataset_sha256']='wrong'
                (p/'results.json').write_text(json.dumps(r))
                with self.assertRaises(ValueError):load_catalogue_run(p)

    def test_invented_reference_rejected_even_with_new_snapshot_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);r=synthetic_capture(p);snapshot=p/'dataset.snapshot.json'
            d=json.loads(snapshot.read_text());d['cases'][0]['reference_answer']='Invented tariff'
            snapshot.write_text(json.dumps(d));r['metadata']['faq_catalogue_dataset_sha256']=digest(snapshot)
            (p/'results.json').write_text(json.dumps(r))
            with self.assertRaises(ValueError):load_catalogue_run(p)

    def test_incomplete_http_and_unconfirmed_mapping_never_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);r=synthetic_capture(p)
            r['results'][0]['actual_api_response'][0]['complete']=False
            r['results'][1]['actual_api_response'][0]['status_code']=502
            r['results'][2]['actual_api_response'][0]['answer']=''
            (p/'results.json').write_text(json.dumps(r));_,rows=load_catalogue_run(p)
            self.assertTrue(all(not row['eligible_response'] for row in rows[:3]))
            r['metadata']['config']['response_mapping_confirmed']=False
            (p/'results.json').write_text(json.dumps(r));_,rows=load_catalogue_run(p)
            self.assertTrue(all(not row['eligible_response'] for row in rows))

    def test_profile_plan_and_no_payload_identity_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);synthetic_capture(p)
            env={'MORPHEUS_EVAL_MODEL':json.loads(PROFILE.read_text())['model']}
            before=(p/'results.json').read_bytes()
            with patch.object(judge,'load',return_value=({},env)):
                code=judge.main(['--results',str(p),'--capture-format','catalogue','--profile',str(PROFILE),'--allow-draft-references','--max-judge-requests','20','--out',str(p/'plan.json')])
            self.assertEqual(code,0);report=json.loads((p/'plan.json').read_text())
            self.assertEqual(report['planned_metric_calls'],10)
            self.assertEqual(report['evaluator_parameters']['max_tokens'],4096)
            self.assertFalse(report['evaluator_parameters']['extra_body']['chat_template_kwargs']['enable_thinking'])
            self.assertNotIn('SYNTHETIC_SUBSCRIBER',(p/'plan.json').read_text())
            self.assertEqual(before,(p/'results.json').read_bytes())

    def test_model_or_parameter_drift_fails_before_loading_capture(self):
        env={'MORPHEUS_EVAL_MODEL':json.loads(PROFILE.read_text())['model']}
        with patch.object(judge,'load',return_value=({},env)):
            with self.assertRaises(ValueError):judge.main(['--results','missing','--out','unused','--profile',str(PROFILE),'--max-tokens','1024'])
        env['MORPHEUS_EVAL_MODEL']='nvidia/DIFFERENT_MODEL'
        with patch.object(judge,'load',return_value=({},env)):
            with self.assertRaises(ValueError):judge.main(['--results','missing','--out','unused','--profile',str(PROFILE)])
