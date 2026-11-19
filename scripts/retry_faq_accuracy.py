#!/usr/bin/env python3
"""Bounded targeted evaluator retry and conservative history-preserving merge."""
import argparse
import copy
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.faq_pilot import digest, load_run

IDS = ('FAQ-PILOT-004', 'FAQ-PILOT-007', 'FAQ-PILOT-008')
BASELINE_SHA = '633991fb093ce7e65eb2d528b045053600f9f13d37e279b6c6180377e9d2b542'
BINDING = ('test_id', 'attempt_id', 'response_sha256', 'user_input', 'response',
           'reference', 'reference_passages', 'reference_review_status')


def merge(base, retry, fresh):
    for report in (base, retry):
        if report['source_results_sha256'] != fresh['source_results_sha256'] or report['dataset_sha256'] != fresh['dataset_sha256']:
            raise ValueError('Capture or reference snapshot mismatch')
    expected = {r['test_id']: r for r in fresh['rows']}
    original = {r['test_id']: r for r in base['rows']}
    if len(original) != len(base['rows']) or set(original) != set(expected):
        raise ValueError('Baseline rows mismatch')
    incoming = {r['test_id']: r for r in retry['rows']}
    if len(incoming) != 3 or len(retry['rows']) != 3 or set(incoming) != set(IDS):
        raise ValueError('Retry must contain exactly the three selected questions')
    result = copy.deepcopy(base)
    for row in result['rows']:
        key = row['test_id']
        if any(row[k] != expected[key][k] for k in BINDING):
            raise ValueError('Baseline attempt/reference mismatch')
        if key in incoming:
            candidate = incoming[key]
            if any(candidate[k] != expected[key][k] for k in BINDING):
                raise ValueError('Retry attempt/reference mismatch')
            if row['metric_scores']['answer_accuracy'] is not None:
                raise ValueError('Cannot overwrite a valid historical score')
            score = candidate['metric_scores']['answer_accuracy']
            if score is not None:
                value = score['score']
                if (not retry.get('native_ragas_executed') or
                    not retry.get('execution_limits', {}).get('both_judges_required') or
                    type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 or
                    candidate.get('evaluation_errors')):
                    raise ValueError('Invalid/incomplete retry score')
                row['metric_scores']['answer_accuracy'] = copy.deepcopy(score)
            row['retry_history'] = [copy.deepcopy(candidate)]
    values = [r['metric_scores']['answer_accuracy']['score'] for r in result['rows'] if r['metric_scores']['answer_accuracy'] is not None]
    result['coverage'] = {'valid_scores': len(values), 'total_questions': len(expected),
                          'fraction': len(values)/len(expected), 'valid_score_average': sum(values)/len(values) if values else None}
    result['evaluation_history'] = [
        {'origin': 'accuracy_all.json', 'parameters': {'max_tokens': 1024, 'thinking': 'not explicitly set', 'temperature': 0.01, 'top_p': 0.1}, 'evaluator': base.get('evaluator'), 'judge_requests_attempted': base.get('judge_requests_attempted')},
        {'origin': 'retry.json', 'parameters': retry.get('evaluator_parameters'), 'limits': retry.get('execution_limits'), 'evaluator': retry.get('evaluator'), 'judge_requests_attempted': retry.get('judge_requests_attempted')}]
    for field in ('judge_requests_attempted', 'judge_request_budget', 'completed_metric_calls', 'planned_metric_calls'):
        result.pop(field, None)
    result['mode'] = 'merged-provisional'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--out', type=Path, help='New directory; default timestamped retry history')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    run = root / 'reports/faq_pilot'
    basepath = run / 'accuracy_all.json'
    if digest(basepath) != BASELINE_SHA:
        raise ValueError('Historical accuracy_all.json changed')
    base = json.loads(basepath.read_text())
    captured, rows = load_run(run)
    fresh = {'source_results_sha256': digest(run / 'results.json'),
             'dataset_sha256': captured['metadata']['pilot_dataset_sha256'], 'rows': rows}
    out = args.out or run / 'retries' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(root / 'scripts/evaluate_faq_ragas.py'),
               '--results', str(run), '--id', ','.join(IDS), '--mode', 'answer-accuracy',
               '--allow-draft-references', '--limit', '3', '--max-judge-requests', '6',
               '--max-tokens', '4096', '--thinking', 'off', '--out', str(out / 'retry.json')]
    if args.execute:
        command.append('--execute')
    code = subprocess.run(command, cwd=root, check=False).returncode
    if digest(basepath) != BASELINE_SHA:
        raise ValueError('Historical report changed during evaluation')
    retry_path = out / 'retry.json'
    if not retry_path.exists():
        return code or 2
    retry = json.loads(retry_path.read_text())
    merged = merge(base, retry, fresh)
    merged['history_files'] = {'baseline_sha256': digest(basepath), 'retry_sha256': digest(retry_path)}
    (out / 'merged.json').write_text(json.dumps(merged, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    print('Coverage and valid-score average: ' + json.dumps(merged['coverage']))
    print('Saved retry history and provisional merge: ' + str(out))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
