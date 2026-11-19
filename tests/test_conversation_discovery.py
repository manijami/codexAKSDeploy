import json
import contextlib
import hashlib
import io
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from scripts.discover_conversations import collect, same_origin
from qa import runner


class ConversationDiscoveryTests(unittest.TestCase):
    def test_origin_rejects_foreign_urls_and_embedded_credentials(self):
        page = 'https://qa-serviceauth.mtn.co.za/morpheus-v5/'
        for candidate in ('https://example.com/a.js', 'http://qa-serviceauth.mtn.co.za/a.js',
                          'https://user@qa-serviceauth.mtn.co.za/a.js'):
            self.assertFalse(same_origin(page, candidate))
        self.assertTrue(same_origin(page, page + 'main.js'))

    def test_get_only_bounded_redacted_collection(self):
        config = {'max_live_requests': 2, 'min_interval_seconds': 0}
        env = {'MORPHEUS_API_KEY': 'SYNTHETIC_SECRET_ABC'}
        page = '<script src="main.js"></script><script src="https://example.com/no.js"></script><script src="extra.js"></script>'
        responses = [dict(status_code=200, complete=True, raw_body=page),
                     dict(status_code=200, complete=True, raw_body='conversation SYNTHETIC_SECRET_ABC')]
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.discover_conversations.request', side_effect=responses) as send:
            report = collect(config, env, Path(tmp))
            self.assertEqual(send.call_count, 2)
            for call in send.call_args_list:
                self.assertEqual(call.args[1], 'GET')
                self.assertIsNone(call.args[3])
            self.assertNotIn('example.com', send.call_args_list[1].args[0])
            self.assertEqual(report['unfetched_script_count'], 1)
            self.assertNotIn(env['MORPHEUS_API_KEY'], (Path(tmp) / 'source-01.txt').read_text())
            self.assertEqual(json.loads((Path(tmp) / 'manifest.json').read_text())['method'], 'GET')

    def test_redirect_is_not_treated_as_a_contract(self):
        with tempfile.TemporaryDirectory() as tmp, patch('scripts.discover_conversations.request', return_value={
                'status_code': 302, 'complete': True, 'raw_body': '<script src="a.js"></script>'}) as send:
            report = collect({'max_live_requests': 9, 'min_interval_seconds': 0}, {}, Path(tmp))
            self.assertEqual(send.call_count, 1)
            self.assertEqual(report['sources'][0]['status_code'], 302)


class FrontendFreshTests(unittest.TestCase):
    def test_fresh_workflow_binding_redaction_and_settings_preservation(self):
        config = json.loads((Path(__file__).parent / 'fixtures/settings.json').read_text())
        config.update(response_mapping_confirmed=True, snapshot_paths=['response.message'], min_interval_seconds=0)
        env = {'QA_MSISDN_A': 'SYNTHETIC_QA_ACCOUNT', 'QA_CONVERSATION_ID_A': 'SYNTHETIC_ORIGINAL_SESSION'}
        before = json.dumps([config, env], sort_keys=True)
        sent = []

        def transport(url, method, headers, body, settings):
            payload = json.loads(body)
            sent.append(payload['conversationId'])
            self.assertEqual(uuid.UUID(sent[-1]).version, 4)
            self.assertEqual(payload['subscriberType'], 'POSTPAID')
            raw = 'data: {"type":"response","response":{"message":"Synthetic test answer"}}\n\n'
            return {'status_code': 200, 'headers': {'content-type': 'text/event-stream', 'x-conversation-id': sent[-1]},
                    'complete': True, 'error': None, 'answer': 'Synthetic test answer', 'raw_body': raw,
                    'events': [{'data': '{"type":"response","response":{"message":"Synthetic test answer"}}'}],
                    'response_sha256': hashlib.sha256(raw.encode()).hexdigest()}

        with tempfile.TemporaryDirectory() as tmp, patch.object(runner, 'load', return_value=(config, env)), patch.object(runner, 'request', side_effect=transport), contextlib.redirect_stdout(io.StringIO()):
            code = runner.main(['run', '--id', 'BASELINE-001,FR-01-V02', '--frontend-fresh', '--live', '--out', tmp])
            data = json.loads((Path(tmp) / 'results.json').read_text())
            self.assertEqual(code, 2)  # Functional evidence is intentionally absent.
            self.assertEqual(len(set(sent)), 2)
            for result in data['results']:
                check = result['actual_api_response'][0]['frontend_conversation_verification']
                self.assertTrue(check['response_id_matches_request'])
                self.assertIsNone(check['fresh_state_isolation'])
            saved = (Path(tmp) / 'results.json').read_text()
            self.assertTrue(all(value not in saved for value in sent))
        self.assertEqual(json.dumps([config, env], sort_keys=True), before)

    def test_fresh_flag_dry_plan_does_not_send(self):
        # Exercise dry planning with no private identity or credential dependency.
        config=json.loads((runner.ROOT/'config/settings.json').read_text())
        with tempfile.TemporaryDirectory() as tmp, patch.object(runner,'load',return_value=(config,{})), patch.object(runner, 'request') as send, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(['run', '--id', 'BASELINE-001', '--frontend-fresh', '--out', tmp]), 0)
            send.assert_not_called()
