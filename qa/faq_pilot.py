"""Draft-reference and exact-attempt boundaries for the small FAQ pilot."""
import hashlib
import json
from pathlib import Path

from .config import ROOT

METRICS = ('answer_accuracy', 'context_precision', 'context_recall',
           'retrieval_relevancy', 'faithfulness', 'groundedness', 'safety')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_dataset(data, root=ROOT, check_documents=False):
    cases = data['cases']
    if len(cases) != 10 or len({c['test_id'] for c in cases}) != 10:
        raise ValueError('Pilot requires exactly ten unique cases.')
    for case in cases:
        if not case.get('question') or not case.get('reference_answer') or not case.get('reference_passages'):
            raise ValueError('Question, reference and passages are required.')
        if case.get('retrieved_contexts') is not None:
            raise ValueError('Reference dataset cannot contain claimed retrieved contexts.')
        for passage in case['reference_passages']:
            if hashlib.sha256(passage['text'].encode()).hexdigest() != passage['passage_sha256']:
                raise ValueError('Reference passage digest mismatch.')
            if check_documents:
                from scripts.build_faq_pilot import paragraphs
                path = root / 'sources/business_kb' / passage['document']
                if path.parent.resolve() != (root / 'sources/business_kb').resolve():
                    raise ValueError('Document path outside business corpus.')
                if digest(path) != passage['document_sha256'] or paragraphs(path)[passage['paragraph']-1] != passage['text']:
                    raise ValueError('Reference no longer matches its source document.')
    return data


def catalogue(data):
    return [{'id': c['test_id'], 'suite': 'faq_pilot', 'category': c['topic'],
             'turns': [{'message': c['question']}],
             'expected': 'Draft reference requires business review: ' + c['reference_answer'],
             'metrics': ['answer_accuracy', 'groundedness'], 'expected_status': [200],
             'requirements': ['FAQ-PILOT'], 'latency_class': 'rag'}
            for c in data['cases']]


def evaluation_rows(results, dataset):
    by_id = {c['test_id']: c for c in validate_dataset(dataset)['cases']}
    rows = []
    seen = set()
    for result in results['results']:
        case = by_id[result['test_id']]
        for index, response in enumerate(result['actual_api_response']):
            attempt = response['attempt_id']
            if attempt in seen:
                raise ValueError('Duplicate attempt ID.')
            seen.add(attempt)
            question = result['request_payload'][index]['message']
            if question != case['question']:
                raise ValueError('Captured question does not match frozen dataset.')
            rows.append({'test_id': case['test_id'], 'attempt_id': attempt,
                         'response_sha256': response['response_sha256'],
                         'user_input': question, 'response': response.get('answer', ''),
                         'reference': case['reference_answer'],
                         'reference_passages': case['reference_passages'],
                         'reference_review_status': case['reference_review_status'],
                         'eligible_response': results['metadata']['mode'] == 'live' and response.get('status_code') == 200
                         and response.get('complete') is True and not response.get('error') and bool(response.get('answer')),
                         'retrieved_contexts': None, 'retrieval_provenance': None,
                         'metric_scores': {m: None for m in METRICS}})
    return rows


def attach_retrieval(rows, export):
    """Consume normalized exact-attempt trace exports, never the reference dataset."""
    records = export.get('attempts', [])
    if not isinstance(records, list):
        raise ValueError('Retrieval export attempts must be a list.')
    by_id = {r['attempt_id']: r for r in rows}
    seen = set()
    for record in records:
        attempt = record['attempt_id']
        if attempt in seen or attempt not in by_id:
            raise ValueError('Duplicate or unknown retrieval attempt.')
        seen.add(attempt)
        row = by_id[attempt]
        if record.get('response_sha256') != row['response_sha256']:
            raise ValueError('Retrieval trace digest does not match capture.')
        required = ('trace_id', 'retrieved_at', 'exported_by', 'correlation_basis')
        if record.get('source_system') != 'azure_search' or not all(record.get(k) for k in required):
            raise ValueError('Actual Azure Search export provenance and correlation are required.')
        chunks = record.get('contexts')
        if not isinstance(chunks, list) or not chunks:
            continue  # Absent/empty evidence cannot become a perfect retrieval score.
        if [c.get('rank') for c in chunks] != list(range(1, len(chunks)+1)):
            raise ValueError('Preserve complete retrieved rank order starting at one.')
        if not all(c.get('id') and isinstance(c.get('text'), str) and c['text'].strip() for c in chunks):
            raise ValueError('Each retrieved chunk needs its source ID and actual text.')
        row['retrieved_contexts'] = [c['text'] for c in chunks]
        row['retrieval_provenance'] = {k: v for k, v in record.items() if k != 'contexts'}
    return rows


def load_run(directory):
    directory = Path(directory)
    results = json.loads((directory / 'results.json').read_text())
    snapshot = directory / 'dataset.snapshot.json'
    if results['metadata'].get('pilot_dataset_sha256') != digest(snapshot):
        raise ValueError('Frozen dataset does not match captured run metadata.')
    dataset = json.loads(snapshot.read_text())
    return results, evaluation_rows(results, dataset)
