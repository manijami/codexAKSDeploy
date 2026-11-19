"""CLI for bounded QA runs, dry plans, demos and offline evidence reviews."""
import argparse
import copy
import hashlib
import json
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import ROOT, load, request_headers, secrets, target
from .evaluation import SEMANTIC, evaluate
from .privacy import redact
from .reporting import write_report
from .transport import request


def now():
    return datetime.now(timezone.utc).isoformat()


def cases_from(root=ROOT):
    result = []
    for path in sorted((root / 'suites').glob('*.json')):
        result.extend(json.loads(path.read_text()))
    ids = [c['id'] for c in result]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate test IDs in suites.')
    return result


def select(cases, suite='all', ids=None, limit=None):
    selected = [c for c in cases if (suite == 'all' or c['suite'] in suite.split(',')) and (not ids or c['id'] in ids.split(','))]
    if ids and set(ids.split(',')) - {c['id'] for c in selected}:
        raise ValueError('Unknown or filtered test ID. Use list to inspect IDs.')
    return selected[:limit] if limit is not None else selected


def expand(value, variables):
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, str):
        for k, v in variables.items():
            value = value.replace('{{' + k + '}}', str(v))
        if '{{' in value:
            raise ValueError('Unresolved template variable in request. Configure designated QA accounts.')
    return value


class Sessions:
    def __init__(self, env, config):
        self.mode = config.get('conversation_mode', 'provided')
        self.pools = {}
        self.used = set()
        for account in ('A', 'B'):
            pool = json.loads(env.get(f'QA_CONVERSATION_IDS_{account}_JSON', '[]'))
            single = env.get(f'QA_CONVERSATION_ID_{account}', '')
            if single:
                pool = [single] + pool
            self.pools[account] = list(dict.fromkeys(pool))

    def take(self, account):
        if self.mode == 'client_uuid':
            return str(uuid.uuid4())
        if self.mode != 'provided':
            raise ValueError('conversation_mode must be provided or client_uuid.')
        while self.pools[account]:
            value = self.pools[account].pop(0)
            if value not in self.used:
                self.used.add(value)
                return value
        raise ValueError(f'Need an unused QA conversation ID for account {account}. Supply a pool or confirm client_uuid support.')


def blockers(case, env, config, allow_writes):
    reasons = []
    if case.get('manual_only'):
        reasons.append('Requires WhatsApp/channel or infrastructure evidence; follow docs/CHANNEL_UAT.md.')
    if case.get('writes') and not (allow_writes and config.get('writes_fixture_ready')):
        reasons.append('Requires --allow-writes and writes_fixture_ready for a resettable QA fixture.')
    if case.get('payment') and not config.get('payment_sandbox_ready'):
        reasons.append('Requires configured non-production payment sandbox and webhook fixture.')
    fixture = case.get('fixture')
    if fixture and not config.get('fixtures', {}).get(fixture, {}).get('ready'):
        reasons.append('Missing verified backend fixture: ' + fixture)
    if case.get('suite') != 'smoke':
        for account in case.get('requires_accounts', ['A']):
            if not env.get(f'QA_MSISDN_{account}'):
                reasons.append('Configure QA_MSISDN_' + account)
        if case.get('distinct_auth'):
            a = request_headers(env, 'A')
            b = request_headers(env, 'B')
            if a == b and target(config, env, account='A') == target(config, env, account='B'):
                reasons.append('Isolation test requires distinct authenticated A/B sessions, not only different MSISDN values.')
    return reasons


def prepare(case, env, config, sessions, template):
    steps = []
    slots = {}
    for index, turn in enumerate(case.get('turns', [{'message': case.get('message', '')}])):
        actor = turn.get('account', 'A')
        slot = turn.get('session', actor)
        if case.get('suite') != 'smoke' and slot not in slots:
            # A stolen A conversation with B credentials must reserve it under A first.
            owner = turn.get('session_owner', actor)
            slots[slot] = sessions.take(owner)
        variables = {'msisdn': env.get('QA_MSISDN_' + turn.get('target_msisdn', actor), ''),
                     'msisdn_A': env.get('QA_MSISDN_A', ''), 'msisdn_B': env.get('QA_MSISDN_B', ''),
                     'conversation_id': slots.get(slot, ''),
                     'subscriber_type': env.get('QA_SUBSCRIBER_TYPE_' + actor, 'POSTPAID'),
                     'message': turn.get('message', '')}
        variables['message'] = expand(variables['message'], variables)
        body = case['body'] if 'body' in case else expand(template, variables)
        if 'payload_overrides' in turn:
            body.update(expand(turn['payload_overrides'], variables))
        if case.get('omit_fields'):
            for key in case['omit_fields']:
                body.pop(key, None)
        fixture_headers = config.get('fixtures', {}).get(case.get('fixture'), {}).get('headers', {})
        headers = request_headers(env, actor, fixture_headers)
        steps.append({'method': case.get('method', 'POST'), 'headers': headers, 'account': actor,
                      'body': None if case.get('method') == 'HEAD' else body,
                      'expected_status': turn.get('expected_status', case.get('expected_status', [200])),
                      'required_metrics': turn.get('metrics', case.get('metrics', [])), 'turn': index + 1})
    return steps


def demo_response(case, step, config):
    from .transport import SSEParser, extract_answer
    if case['suite'] == 'smoke':
        body = json.dumps({'detail': [{'type': 'missing', 'loc': ['body', field], 'msg': 'Field required', 'input': {}} for field in case.get('missing_fields', [])]}) if step['method'] != 'HEAD' else ''
        return {'status_code': case['expected_status'][0], 'headers': {'allow': 'POST', 'content-type': 'application/json'}, 'raw_body': body, 'events': [], 'answer': '', 'first_byte_ms': 1, 'first_event_ms': None, 'elapsed_ms': 2, 'complete': True, 'error': None, 'done_received': False, 'response_sha256': hashlib.sha256(body.encode()).hexdigest()}
    body = 'event: answer\ndata: {"text":"DEMO ONLY: synthetic response, no MTN subscriber data."}\n\nevent: done\ndata: [DONE]\n\n'
    parser = SSEParser()
    parser.feed(body)
    return {'status_code': 200, 'headers': {'content-type': 'text/event-stream'}, 'raw_body': body, 'events': parser.finish(), 'answer': extract_answer(parser.events, config), 'first_byte_ms': 5, 'first_event_ms': 7, 'elapsed_ms': 9, 'complete': True, 'error': None, 'done_received': True, 'response_sha256': hashlib.sha256(body.encode()).hexdigest()}


def make_result(case, steps=None, responses=None, status='BLOCKED', reasons=(), scores=None, observations=()):
    return {'test_id': case['id'], 'category': case['category'], 'suite': case['suite'],
            'requirements': case.get('requirements', []), 'expected_behavior': case.get('expected', ''),
            'request_payload': [s['body'] for s in (steps or [])], 'actual_api_response': responses or [],
            'metric_scores': scores or {m: None for m in set(SEMANTIC + case.get('metrics', []))},
            'status': status, 'defect_description': '; '.join(reasons), 'observations': list(observations)}


def load_evidence(path):
    if not path:
        return {}
    content = json.loads(Path(path).read_text())
    records = content['attempts']
    if isinstance(records, dict):
        return records
    return {r['attempt_id']: r for r in records}


def run(args, cases=None, metadata=None):
    config, env = load()
    if getattr(args, 'frontend_fresh', False):
        # Observed QA frontend workflow; opt-in per invocation, never persisted.
        config = {**config, 'conversation_mode': 'client_uuid'}
    cases = select(cases_from() if cases is None else cases, args.suite, args.id, args.limit)
    if not cases:
        raise ValueError('No matching cases.')
    if args.live and args.demo:
        raise ValueError('Choose --live or --demo.')
    mode = 'live' if args.live else ('demo' if args.demo else 'dry-run')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    sessions = Sessions(env, config)
    template = json.loads((ROOT / 'config/payload.template.json').read_text())
    evidence = load_evidence(args.evidence)
    results, count, last_request = [], 0, 0
    demo_env = {**env, 'QA_MSISDN_A': 'TEST_ACCOUNT_A', 'QA_MSISDN_B': 'TEST_ACCOUNT_B'}
    demo_config = {**config, 'conversation_mode': 'client_uuid'}
    if mode == 'demo':
        sessions = Sessions(demo_env, demo_config)
    for case in cases:
        if mode == 'dry-run':
            reasons = blockers(case, env, config, args.allow_writes)
            results.append(make_result(case, status='NOT_RUN', reasons=['Dry plan: no request sent.'] + reasons))
            continue
        reasons = blockers(case, env, config, args.allow_writes) if mode == 'live' else []
        if mode == 'demo' and case.get('manual_only'):
            reasons = ['Manual-only scenario is not simulated.']
        if reasons:
            results.append(make_result(case, reasons=reasons))
            continue
        try:
            steps = prepare(case, demo_env if mode == 'demo' else env, config, sessions, template)
        except ValueError as exc:
            results.append(make_result(case, reasons=[str(exc)]))
            continue
        if mode == 'live' and count + len(steps) > config['max_live_requests']:
            results.append(make_result(case, steps, reasons=['Run request budget would be exceeded. Select a smaller batch.']))
            continue
        responses = []
        for step in steps:
            if mode == 'live':
                delay = config['min_interval_seconds'] - (time.monotonic() - last_request)
                if delay > 0:
                    time.sleep(delay)
                last_request = time.monotonic()
                response = request(target(config, env, account=step['account']), step['method'], step['headers'],
                                   None if step['body'] is None else json.dumps(step['body']).encode(), config)
                count += 1
            else:
                response = demo_response(case, step, config)
            response['attempt_id'] = run_id + ':' + case['id'] + ':' + str(step['turn'])
            if getattr(args, 'frontend_fresh', False) and isinstance(step['body'], dict):
                returned_id = response.get('headers', {}).get('x-conversation-id')
                response['frontend_conversation_verification'] = {
                    'workflow': 'frontend-generated UUID v4, first text POST',
                    'response_id_present': bool(returned_id),
                    'response_id_matches_request': (returned_id == step['body'].get('conversationId')) if returned_id else None,
                    'fresh_state_isolation': None,
                    'limitation': 'Response binding alone does not prove absence of prior state or subject authorization.'}
            response['required_metrics'] = step['required_metrics']
            response['expected_status'] = step['expected_status']
            responses.append(response)
            if response.get('error') or response.get('status_code') not in step['expected_status']:
                break  # Do not perform later actions after an unexpected earlier failure.
        scores, status, reasons, observations = evaluate(case, responses, config, evidence)
        if len(responses) != len(steps):
            reasons.append('Later turns were not sent after an earlier failure.')
            status = 'FAIL'
        results.append(make_result(case, steps, responses, status, reasons, scores, observations))
        print(case['id'] + ': ' + status, flush=True)
    meta = {'mode': mode, 'created_at': now(), 'run_id': run_id, 'live_requests_attempted': count,
            'thresholds': config['thresholds'], 'config': {k: config[k] for k in ('response_mapping_confirmed', 'conversation_mode')},
            'scope': 'Direct API. WhatsApp delivery, typing and webhook behavior require channel UAT.'}
    if metadata:
        meta.update(metadata)
    runtime_secrets = []
    for result in results:
        for body in result['request_payload']:
            if isinstance(body, dict):
                runtime_secrets.extend(body.get(k, '') for k in ('conversationId', 'msisdn'))
        for response in result['actual_api_response']:
            runtime_secrets.append(response.get('headers', {}).get('x-conversation-id', ''))
    directory = write_report(args.out or ROOT / 'reports' / run_id, results, meta, secrets(env) + runtime_secrets)
    print('Report: ' + str(directory / 'report.html'))
    if mode == 'dry-run' or mode == 'demo':
        return 0
    if any(r['status'] == 'FAIL' for r in results):
        return 1
    return 2 if any(r['status'] != 'PASS' for r in results) else 0


def review(args):
    config, env = load()
    original = json.loads(Path(args.results).read_text())
    evidence = load_evidence(args.evidence)
    lookup = {c['id']: c for c in cases_from()}
    results = []
    # Keep the thresholds/config used for this run instead of silently changing the gate.
    config['thresholds'] = original['metadata'].get('thresholds', config['thresholds'])
    config.update(original['metadata'].get('config', {}))
    for result in original['results']:
        updated = copy.deepcopy(result)
        if result.get('actual_api_response'):
            case = lookup[result['test_id']]
            scores, status, reasons, observations = evaluate(case, result['actual_api_response'], config, evidence)
            updated.update(metric_scores=scores, status=status, defect_description='; '.join(reasons), observations=observations)
        results.append(updated)
    meta = {**original['metadata'], 'reviewed_at': now(), 'evidence_review': True}
    directory = write_report(args.out or Path(args.results).parent / 'reviewed', results, meta, secrets(env))
    print('Reviewed report: ' + str(directory / 'report.html'))
    return 1 if any(r['status'] == 'FAIL' for r in results) else (2 if any(r['status'] != 'PASS' for r in results) else 0)


def performance(args):
    config, _ = load()
    data = json.loads(Path(args.results).read_text())
    if data['metadata']['mode'] != 'live':
        raise ValueError('Performance SLA analysis requires a live capture, not demo/dry-run data.')
    lookup = {c['id']: c for c in cases_from()}
    groups = {'rag': [], 'tool': []}
    failures = []
    for case in data['results']:
        group = lookup[case['test_id']].get('latency_class')
        if group not in groups:
            continue
        for response in case['actual_api_response']:
            if response.get('status_code') != 200 or response.get('error') or not response.get('complete'):
                failures.append(response['attempt_id'])
            else:
                groups[group].append(response)
    report = {'scope': 'Direct API timings; no WhatsApp typing/delivery or full channel latency measured.', 'failures': failures, 'groups': {}}
    for group, responses in groups.items():
        values = sorted(r['elapsed_ms'] for r in responses)
        first = sorted(r['first_event_ms'] for r in responses if r['first_event_ms'] is not None)
        n = len(values)
        p95 = values[math.ceil(.95 * n) - 1] if n else None
        first_p95 = first[math.ceil(.95 * len(first)) - 1] if first else None
        budget = config['performance_response_budget_ms'][group]
        status = 'INCONCLUSIVE' if n < config['performance_min_samples'] else ('PASS' if p95 <= budget and len(first) == n and first_p95 <= config['first_event_budget_ms'] and not failures else 'FAIL')
        report['groups'][group] = {'n': n, 'p95_total_ms': p95, 'p95_first_event_ms': first_p95, 'budget_total_ms': budget, 'minimum_samples': config['performance_min_samples'], 'status': status}
    path = Path(args.out or Path(args.results).parent / 'performance.json')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
    return 1 if any(v['status'] == 'FAIL' for v in report['groups'].values()) else (2 if any(v['status'] != 'PASS' for v in report['groups'].values()) else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('list', 'run'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--suite', default='all')
        cmd.add_argument('--id')
        cmd.add_argument('--limit', type=int)
        if name == 'run':
            cmd.add_argument('--live', action='store_true')
            cmd.add_argument('--frontend-fresh', action='store_true', help='Use the observed QA frontend UUID-v4 workflow for this invocation only; backend freshness remains unverified.')
            cmd.add_argument('--demo', action='store_true')
            cmd.add_argument('--allow-writes', action='store_true')
            cmd.add_argument('--evidence')
            cmd.add_argument('--out')
    sub.add_parser('doctor')
    cmd = sub.add_parser('review')
    cmd.add_argument('--results', required=True)
    cmd.add_argument('--evidence', required=True)
    cmd.add_argument('--out')
    cmd = sub.add_parser('performance')
    cmd.add_argument('--results', required=True)
    cmd.add_argument('--out')
    args = parser.parse_args(argv)
    try:
        if args.command == 'run':
            return run(args)
        if args.command == 'review':
            return review(args)
        if args.command == 'performance':
            return performance(args)
        config, env = load()
        if args.command == 'list':
            for case in select(cases_from(), args.suite, args.id, args.limit):
                print(f"{case['id']:24} {case['suite']:16} {case['category']} | {case.get('expected', '')}")
            return 0
        print('Python: ' + sys.version.split()[0])
        print('Cases: ' + str(len(cases_from())))
        print('Target (key omitted): ' + target(config, env, include_key=False))
        for key in ('MORPHEUS_API_KEY', 'MORPHEUS_COOKIE', 'QA_MSISDN_A', 'QA_CONVERSATION_ID_A', 'QA_MSISDN_B'):
            print(key + ': ' + ('configured' if env.get(key) else 'not configured'))
        print('Conversation mode: ' + config['conversation_mode'])
        print('SSE response mapping confirmed: ' + str(config['response_mapping_confirmed']))
        print('No network calls made. See START_HERE_MAC.md for missing settings.')
        return 0
    except (ValueError, OSError, KeyError, TypeError) as exc:
        try:
            _, env = load()
            message = redact(str(exc), secrets(env))
        except Exception:
            message = 'Configuration or input error. Check JSON and literal .env syntax.'
        print('Error: ' + message, file=sys.stderr)
        return 2
