import copy
import json
import unittest
from pathlib import Path
from scripts.retry_faq_accuracy import merge, BASELINE_SHA
from qa.faq_pilot import digest, load_run

ROOT = Path(__file__).resolve().parents[1] / 'reports/faq_pilot'

class RetryTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT / 'accuracy_all.json').read_text())
        captured, rows = load_run(ROOT)
        self.fresh = {'source_results_sha256': digest(ROOT / 'results.json'),
                      'dataset_sha256': captured['metadata']['pilot_dataset_sha256'], 'rows': rows}
        self.retry = json.loads((ROOT / 'retries/prepared_plan/retry.json').read_text())

    def test_baseline_and_null_retry_preserve_seven(self):
        self.assertEqual(digest(ROOT / 'accuracy_all.json'), BASELINE_SHA)
        result = merge(self.base, self.retry, self.fresh)
        self.assertEqual(result['coverage']['valid_scores'], 7)
        for a, b in zip(result['rows'], self.base['rows']):
            self.assertEqual(a['metric_scores'], b['metric_scores'])

    def test_mismatched_attempt_reference_and_snapshot_rejected(self):
        for field in ('attempt_id', 'response_sha256', 'reference', 'reference_passages'):
            changed = copy.deepcopy(self.retry)
            changed['rows'][0][field] = 'synthetic mismatch'
            with self.assertRaises(ValueError):
                merge(self.base, changed, self.fresh)
        changed = copy.deepcopy(self.retry)
        changed['dataset_sha256'] = 'synthetic mismatch'
        with self.assertRaises(ValueError):
            merge(self.base, changed, self.fresh)

    def test_valid_retry_fills_only_null_and_retains_history(self):
        self.retry['native_ragas_executed'] = True
        self.retry['rows'][0]['metric_scores']['answer_accuracy'] = {'score': 1.0, 'reference_is_draft': True}
        result = merge(self.base, self.retry, self.fresh)
        self.assertEqual(result['coverage']['valid_scores'], 8)
        self.assertEqual(result['rows'][2], self.base['rows'][2])
        self.assertIn('retry_history', result['rows'][3])

    def test_partial_or_wrong_selection_rejected(self):
        self.retry['rows'][0]['metric_scores']['answer_accuracy'] = {'score': 1.0}
        with self.assertRaises(ValueError):
            merge(self.base, self.retry, self.fresh)
        self.retry['rows'].pop()
        with self.assertRaises(ValueError):
            merge(self.base, self.retry, self.fresh)
