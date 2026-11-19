"""Expanded-catalogue integrity, reference boundaries and bounded execution guards."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa.config import ROOT
from qa.faq_pilot import digest
from scripts.build_faq_catalogue import DATASET, validate, explicit_units, atm_units
from scripts.run_faq_catalogue import main, selected_cases


class CatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data=json.loads(DATASET.read_text())

    def test_all_documents_have_exact_passage_backed_cases(self):
        data=validate(self.data)
        self.assertGreaterEqual(len(data['cases']),350)
        self.assertEqual(len(data['corpus_inventory']),58)
        self.assertEqual(len({c['document_id'] for c in data['cases']}),58)
        self.assertTrue(all(c['reference_review_status']=='REQUIRES_BUSINESS_REVIEW' and c['retrieved_contexts'] is None for c in data['cases']))

    def test_wrong_reference_hash_text_and_invented_answer_rejected(self):
        for field,value in [('document_sha256','synthetic mismatch'),('text','invented passage'),('passage_sha256','synthetic mismatch')]:
            data=copy.deepcopy(self.data);data['cases'][0]['reference_passages'][0][field]=value
            with self.assertRaises(ValueError):validate(data)
        data=copy.deepcopy(self.data);data['cases'][0]['reference_answer']='Invented account balance'
        with self.assertRaises(ValueError):validate(data)
        data=copy.deepcopy(self.data);data['cases'][0]['retrieved_contexts']=['Reference masquerading as retrieval']
        with self.assertRaises(ValueError):validate(data)

    def test_no_section_title_bleeds_into_expected_answer(self):
        synthetic=['Question: First?','Answer: Supported statement.','','Lost QR Code','Question: Next?','Answer: Next statement.']
        self.assertEqual(explicit_units(synthetic)[0]['numbers'],[2])

    def test_atm_blank_lines_do_not_become_city_or_site(self):
        synthetic=['What is the MoMo Express ATM locations in Example?','','  ','City','Site Name','Street Address','Example City','Synthetic Site','10 Example Street']
        row=atm_units(synthetic)[0]
        self.assertEqual(row['numbers'],[7,8,9])
        self.assertEqual(row['custom_answer'],'Synthetic Site in Example City: 10 Example Street.')

    def test_batches_cover_catalogue_once_and_never_exceed_ten(self):
        selected=[]
        for b in range(1,(len(self.data['cases'])+9)//10+1):
            batch=selected_cases(self.data,batch=b)
            self.assertLessEqual(len(batch),10);selected.extend(c['test_id'] for c in batch)
        self.assertEqual(selected,[c['test_id'] for c in self.data['cases']])
        for value in (0,-1,999):
            with self.assertRaises(ValueError):selected_cases(self.data,batch=value)
        ids=[c['test_id'] for c in self.data['cases'][:11]]
        for value in (','.join(ids),ids[0]+','+ids[0],'FAQ-PILOT-001'):
            with self.assertRaises(ValueError):selected_cases(self.data,ids=value)

    def test_live_needs_explicit_draft_opt_in_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):main(['--batch','1','--live','--out',str(Path(temp)/'live')])
            with self.assertRaises(ValueError):main(['--batch','1','--out',temp])

    def test_cli_preserves_snapshot_and_keeps_references_out_of_messages(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'plan'
            with patch('scripts.run_faq_catalogue.run',return_value=0) as runner:
                self.assertEqual(main(['--batch','44','--out',str(out)]),0)
            options=runner.call_args.args[0];cases=runner.call_args.kwargs['cases']
            self.assertFalse(options.live);self.assertFalse(options.allow_writes)
            self.assertLessEqual(len(cases),10)
            self.assertEqual(digest(out/'dataset.snapshot.json'),digest(DATASET))
            for c,source in zip(cases,selected_cases(self.data,batch=44)):
                self.assertEqual(c['turns'],[{'message':source['question']}])
                self.assertNotIn('reference_passages',c['turns'][0])
            selection=json.loads((out/'selection.json').read_text())
            self.assertEqual(selection['maximum_attempted_posts'],2)

    def test_original_pilot_and_all_historical_reports_preserved(self):
        protected=json.loads((ROOT/'reports/faq_expansion/historical_preservation.json').read_text())
        for relative,sha in protected.items():
            self.assertEqual(digest(ROOT/relative),sha,relative)
