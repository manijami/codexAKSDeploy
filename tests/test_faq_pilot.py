"""Offline pilot checks; all responses, traces and evaluator scores are synthetic."""
import asyncio
import contextlib
import copy
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from qa.config import ROOT
from qa.faq_pilot import attach_retrieval, catalogue, evaluation_rows, load_run, validate_dataset
from scripts.build_faq_pilot import build
from scripts import evaluate_faq_ragas as judge
from scripts import run_faq_pilot as pilot


class FaqPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = json.loads((ROOT / 'datasets/faq_pilot.json').read_text())

    def rows(self):
        case = self.dataset['cases'][0]
        results = {'metadata': {'mode': 'live'}, 'results': [{
            'test_id': case['test_id'], 'request_payload': [{'message': case['question']}],
            'actual_api_response': [{'attempt_id': 'SYNTHETIC:FAQ-PILOT-001:1',
                                     'response_sha256': 'SYNTHETIC_RESPONSE_DIGEST',
                                     'answer': 'Synthetic evaluator test response', 'status_code': 200,
                                     'complete': True, 'error': None}]}]}
        return evaluation_rows(results, self.dataset)

    def export(self):
        return {'attempts': [{'attempt_id': 'SYNTHETIC:FAQ-PILOT-001:1',
            'response_sha256': 'SYNTHETIC_RESPONSE_DIGEST', 'source_system': 'azure_search',
            'trace_id': 'SYNTHETIC_TRACE', 'retrieved_at': 'SYNTHETIC_TIMESTAMP',
            'exported_by': 'synthetic_test_exporter', 'correlation_basis': 'synthetic_fixture_only',
            'contexts': [{'id': 'SYNTHETIC_CHUNK', 'rank': 1, 'text': 'Synthetic retrieved text, not a reference.'}]}]}

    def test_ten_distinct_draft_references_exactly_match_documents(self):
        validate_dataset(self.dataset, check_documents=True)
        self.assertEqual(build(), self.dataset)
        self.assertEqual(len({c['topic'] for c in self.dataset['cases']}), 10)
        self.assertTrue(all(c['reference_review_status'] == 'REQUIRES_BUSINESS_REVIEW' for c in self.dataset['cases']))
        self.assertTrue(all(c['retrieved_contexts'] is None for c in self.dataset['cases']))

    def test_reference_tampering_rejected(self):
        bad = copy.deepcopy(self.dataset)
        bad['cases'][0]['reference_passages'][0]['text'] += 'Invented passage'
        with self.assertRaises(ValueError):
            validate_dataset(bad)

    def test_reference_is_never_retrieval(self):
        row = self.rows()[0]
        self.assertIsNone(row['retrieved_contexts'])
        self.assertEqual(judge.metric_inputs(row, 'all', True).keys(), {'answer_accuracy'})
        self.assertTrue(all(v is None for v in row['metric_scores'].values()))
        self.assertEqual(judge.metric_inputs(row, 'all', False), {})

    def test_no_response_is_ineligible_not_an_accuracy_failure(self):
        row = self.rows()[0]
        row['eligible_response'] = False
        self.assertEqual(judge.metric_inputs(row, 'all', True), {})

    def test_export_requires_exact_attempt_digest_and_provenance(self):
        for field in ('response_sha256', 'source_system', 'trace_id', 'correlation_basis'):
            bad = self.export()
            bad['attempts'][0][field] = 'wrong' if field in ('response_sha256', 'source_system') else ''
            with self.assertRaises(ValueError):
                attach_retrieval(self.rows(), bad)

    def test_retrieval_rank_and_trace_kept_separate(self):
        rows = attach_retrieval(self.rows(), self.export())
        row = rows[0]
        tasks = judge.metric_inputs(row, 'retrieval', True)
        self.assertEqual(set(tasks), {'context_precision', 'context_recall', 'faithfulness'})
        self.assertEqual(tasks['faithfulness']['retrieved_contexts'], ['Synthetic retrieved text, not a reference.'])
        self.assertNotIn('reference', tasks['faithfulness'])
        self.assertNotIn('response', tasks['context_precision'])
        bad = self.export()
        bad['attempts'][0]['contexts'][0]['rank'] = 2
        with self.assertRaises(ValueError):
            attach_retrieval(self.rows(), bad)

    def test_empty_export_does_not_pass_retrieval(self):
        export = self.export()
        export['attempts'][0]['contexts'] = []
        rows = attach_retrieval(self.rows(), export)
        self.assertIsNone(rows[0]['retrieved_contexts'])

    def test_judge_allowlist_and_hard_request_budget(self):
        budget = judge.JudgeBudget(['judge.example.test'], 1, 10)
        with self.assertRaises(ValueError):
            budget.claim('https://other.example.test/v1')
        budget.claim('https://judge.example.test/v1/chat/completions')
        with self.assertRaises(ValueError):
            budget.claim('https://judge.example.test/v1/chat/completions')
        self.assertEqual(budget.count, 1)

    def test_configuration_names_only_and_no_default_provider(self):
        self.assertEqual(judge.configuration({}), list(judge.SETTINGS))
        env = dict.fromkeys(judge.SETTINGS, 'synthetic')
        env.update(MORPHEUS_EVAL_BASE_URL='http://judge.example.test/v1',
                   MORPHEUS_EVAL_ALLOWED_HOSTS_JSON='["judge.example.test"]')
        with self.assertRaises(ValueError):
            judge.configuration(env)

    def test_diagnostics_never_include_exception_text_or_completion_content(self):
        exc = ValueError('SYNTHETIC_SECRET_KEY and subscriber data must not appear')
        exc.last_completion = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='length', message=SimpleNamespace(content='SYNTHETIC_PRIVATE_COMPLETION'))])
        info = judge.safe_exception(exc)
        self.assertEqual(info, {'exception_type': 'ValueError', 'finish_reasons': ['length']})
        self.assertNotIn('SYNTHETIC', json.dumps(info))

    def test_dry_cli_preserves_catalogue_and_never_sends(self):
        # Offline plans must not depend on private .env contents.
        config=json.loads((ROOT/'config/settings.json').read_text())
        with tempfile.TemporaryDirectory() as tmp, patch('qa.runner.load',return_value=(config,{})), patch('qa.runner.request') as send, contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / 'dry'
            self.assertEqual(pilot.main(['--out', str(out)]), 0)
            send.assert_not_called()
            results, rows = load_run(out)
            self.assertEqual(len(results['results']), 10)
            self.assertTrue(all(r['status'] == 'NOT_RUN' for r in results['results']))
            self.assertEqual(rows, [])
            self.assertEqual(len(__import__('qa.runner', fromlist=['cases_from']).cases_from()), 220)
            with self.assertRaises(ValueError):
                pilot.main(['--out', str(out)])
            snapshot = out / 'dataset.snapshot.json'
            snapshot.write_text(snapshot.read_text() + '\n')
            with self.assertRaises(ValueError):
                load_run(out)

    def test_live_execution_contract_with_synthetic_transport(self):
        config = json.loads((Path(__file__).parent / 'fixtures/settings.json').read_text())
        config.update(response_mapping_confirmed=True, snapshot_paths=['response.message'], min_interval_seconds=0)
        env = {'QA_MSISDN_A': 'SYNTHETIC_QA_ACCOUNT', 'QA_CONVERSATION_ID_A': 'SYNTHETIC_OLD_ID'}
        original = copy.deepcopy((config, env))
        ids = []
        def transport(url, method, headers, body, cfg):
            request = json.loads(body)
            self.assertEqual(method, 'POST')
            self.assertEqual(set(request), {'conversationId', 'msisdn', 'subscriberType', 'message'})
            self.assertEqual(request['subscriberType'], 'POSTPAID')
            self.assertNotIn('reference', request)
            ids.append(request['conversationId'])
            raw = 'data: {"type":"response","response":{"message":"SYNTHETIC ANSWER"}}\n\n'
            return {'status_code': 200, 'headers': {'content-type': 'text/event-stream', 'x-conversation-id': ids[-1]},
                    'complete': True, 'error': None, 'answer': 'SYNTHETIC ANSWER', 'raw_body': raw,
                    'events': [{'data': '{"type":"response","response":{"message":"SYNTHETIC ANSWER"}}'}],
                    'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}
        with tempfile.TemporaryDirectory() as tmp, patch('qa.runner.load', return_value=(config, env)), patch('qa.runner.request', side_effect=transport), contextlib.redirect_stdout(io.StringIO()):
            out = Path(tmp) / 'synthetic'
            self.assertEqual(pilot.main(['--live', '--out', str(out)]), 2)
            results, rows = load_run(out)
            self.assertEqual(len(set(ids)), 10)
            self.assertTrue(all(r['status'] == 'INCONCLUSIVE' for r in results['results']))
            self.assertEqual(len(rows), 10)
            self.assertTrue(all(all(v is None for v in r['metric_scores'].values()) for r in rows))
            self.assertTrue(all(i not in (out / 'results.json').read_text() for i in ids))
            path = out / 'results.json'
            before = path.read_bytes()
            with patch.object(judge, 'load', return_value=(config, {})):
                report = out / 'accuracy.plan.json'
                self.assertEqual(judge.main(['--results', str(out), '--out', str(report), '--allow-draft-references']), 0)
                plan = json.loads(report.read_text())
                self.assertEqual(plan['planned_metric_calls'], 10)
                self.assertFalse(plan['native_ragas_executed'])
                blocked = out / 'accuracy.blocked.json'
                self.assertEqual(judge.main(['--results', str(out), '--out', str(blocked), '--execute', '--allow-draft-references']), 2)
                self.assertTrue(all(all(v is None for v in row['metric_scores'].values()) for row in json.loads(blocked.read_text())['rows']))
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual((config, env), original)

    def test_native_adapter_contract_with_synthetic_sdk_stubs(self):
        # Exercises integration wiring only. These stubs are not native SDK execution.
        calls = []
        ratings = [0.5, 1.0]
        class Scorer:
            def __init__(self, **kwargs):
                pass
            def _average_scores(self, first, second):
                return (first + second) / 2
            async def ascore(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(value=self._average_scores(*ratings))
        class HttpClient:
            def __init__(self, **kwargs):
                self.transport = kwargs['transport']
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        class HttpTransport:
            def __init__(self, **kwargs):
                pass
            async def aclose(self):
                pass
        modules = {name: ModuleType(name) for name in ('httpx', 'openai', 'ragas', 'ragas.llms', 'ragas.metrics', 'ragas.metrics.collections')}
        modules['httpx'].AsyncBaseTransport = object
        modules['httpx'].AsyncHTTPTransport = HttpTransport
        modules['httpx'].AsyncClient = HttpClient
        modules['openai'].AsyncOpenAI = lambda **kwargs: SimpleNamespace(**kwargs)
        async def unused_generate(*args, **kwargs):
            raise AssertionError('Synthetic scorer does not call the real LLM')
        modules['ragas.llms'].llm_factory = lambda *args, **kwargs: SimpleNamespace(agenerate=unused_generate)
        for name in ('AnswerAccuracy', 'ContextPrecision', 'ContextRecall', 'Faithfulness'):
            setattr(modules['ragas.metrics.collections'], name, Scorer)
        args = SimpleNamespace(limit=10, mode='all', allow_draft_references=True)
        env = {'MORPHEUS_EVAL_BASE_URL': 'https://judge.example.test/v1',
               'MORPHEUS_EVAL_MODEL': 'synthetic_model', 'MORPHEUS_EVAL_API_KEY': 'SYNTHETIC_KEY'}
        with patch.dict('sys.modules', modules), patch.dict(os.environ):
            rows = self.rows()
            asyncio.run(judge.native_evaluate(rows, env, args, judge.JudgeBudget(['judge.example.test'], 5, 10)))
            self.assertEqual(len(calls), 1)
            self.assertEqual(set(calls[0]), {'user_input', 'response', 'reference'})
            self.assertEqual(rows[0]['metric_scores']['answer_accuracy']['score'], 0.75)
            self.assertTrue(rows[0]['metric_scores']['answer_accuracy']['reference_is_draft'])
            self.assertIsNone(rows[0]['metric_scores']['faithfulness'])
            self.assertIsNone(rows[0]['metric_scores']['safety'])
            ratings[:] = [float('nan'), 1.0]
            rows = self.rows()
            asyncio.run(judge.native_evaluate(rows, env, args, judge.JudgeBudget(['judge.example.test'], 5, 10)))
            self.assertIsNone(rows[0]['metric_scores']['answer_accuracy'])
            self.assertTrue(rows[0]['evaluation_errors'])
