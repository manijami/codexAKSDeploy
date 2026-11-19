"""Frozen expanded-catalogue capture adapter; never invents retrieval evidence."""
import hashlib
import json
import re
from pathlib import Path
from .faq_pilot import METRICS, digest


def frozen_catalogue(dataset):
    cases=dataset['cases']; inventory=dataset['corpus_inventory']
    if dataset.get('dataset_id')!='business-faq-catalogue-v1' or len(cases)<350 or len(inventory)!=58:
        raise ValueError('Unknown expanded snapshot schema')
    docs={d['document']:d for d in inventory}
    ids={c['test_id']:c for c in cases}
    if len(docs)!=len(inventory) or len(ids)!=len(cases):raise ValueError('Duplicate snapshot IDs')
    for case in cases:
        if case.get('retrieved_contexts') is not None or case['reference_review_status']!='REQUIRES_BUSINESS_REVIEW':raise ValueError('Invalid frozen reference boundary')
        if not case['question'] or not case['reference_answer'] or not case['reference_passages']:raise ValueError('Incomplete frozen case')
        for p in case['reference_passages']:
            if p['document'] not in docs or p['document_sha256']!=docs[p['document']]['document_sha256'] or type(p['paragraph']) is not int or p['paragraph']<1 or hashlib.sha256(p['text'].encode()).hexdigest()!=p['passage_sha256']:
                raise ValueError('Invalid frozen source binding')
        # Source versions come from the snapshot, not potentially updated local documents.
        if case['selection_kind']=='source_table_lookup':
            if len(case['reference_passages'])!=3:raise ValueError('Invalid table row')
            city,site,address=[p['text'].strip() for p in case['reference_passages']]
            expected=f'{site} in {city}: {address}.'
        else:
            expected='\n'.join(re.sub(r'^\s*Answer:\s*','',p['text']).strip() for p in case['reference_passages']).strip()
        if expected!=case['reference_answer']:raise ValueError('Reference answer differs from frozen passages')
    return ids


def load_catalogue_run(directory):
    directory=Path(directory)
    result=json.loads((directory/'results.json').read_text())
    meta=result['metadata'];snapshot=directory/'dataset.snapshot.json'
    if meta.get('faq_catalogue_dataset_sha256')!=digest(snapshot):raise ValueError('Frozen catalogue digest mismatch')
    ids=frozen_catalogue(json.loads(snapshot.read_text()))
    selection_path=directory/'selection.json'
    if meta.get('faq_catalogue_selection_sha256')!=digest(selection_path):raise ValueError('Selection digest mismatch')
    selection=json.loads(selection_path.read_text()); selected=selection['test_ids']
    if not 1<=len(selected)<=10 or len(set(selected))!=len(selected) or not set(selected)<=set(ids):raise ValueError('Invalid bounded selection')
    records=result['results']
    if [r['test_id'] for r in records]!=selected:raise ValueError('Capture does not match ordered selection')
    rows=[];attempts=set()
    for record in records:
        case=ids[record['test_id']]; responses=record['actual_api_response'];payloads=record['request_payload']
        if len(responses)>1 or (responses and len(payloads)!=len(responses)):raise ValueError('Expected one captured turn per selected FAQ')
        for response,payload in zip(responses,payloads):
            if payload['message']!=case['question']:raise ValueError('Question differs from frozen reference')
            attempt=response['attempt_id'];sha=response['response_sha256']
            if not isinstance(attempt,str) or not attempt or attempt in attempts or not re.fullmatch(r'[0-9a-f]{64}',sha):raise ValueError('Invalid or duplicate attempt binding')
            attempts.add(attempt)
            eligible=(meta['mode']=='live' and meta.get('config',{}).get('response_mapping_confirmed') is True and response.get('status_code')==200 and response.get('complete') is True and not response.get('error') and bool(response.get('answer')))
            rows.append({'test_id':case['test_id'],'attempt_id':attempt,'response_sha256':sha,
                'user_input':payload['message'],'response':response.get('answer',''),
                'reference':case['reference_answer'],'reference_passages':case['reference_passages'],
                'reference_review_status':case['reference_review_status'],'eligible_response':eligible,
                'retrieved_contexts':None,'retrieval_provenance':None,
                'metric_scores':{m:None for m in METRICS},
                'protocol_evidence':{'http_status':response.get('status_code'),'complete':response.get('complete'),
                    'first_event_ms':response.get('first_event_ms'),'total_ms':response.get('elapsed_ms')},
                'application_status_note':'HTTP/stream completion do not establish application success; inspect final saved SSE response separately.'})
    return result,rows
