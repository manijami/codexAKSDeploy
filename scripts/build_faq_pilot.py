#!/usr/bin/env python3
"""Build passage-bound draft FAQ references from local business Word documents."""
import hashlib
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT

# Pilot specs/answers are private assets, never embedded in this public module.


def paragraphs(path):
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    return [''.join(t.text or '' for t in p.findall('.//w:t', ns))
            for p in root.findall('.//w:p', ns)]


def build(root=ROOT):
    spec_path = Path(root) / 'datasets/faq_pilot_specs.private.json'
    if not spec_path.exists(): raise ValueError('Supply approved private pilot specs before building references')
    specs = json.loads(spec_path.read_text())
    kb = root / 'sources/business_kb'
    corpus = []
    for path in sorted(kb.glob('*.docx')):
        ps = paragraphs(path)
        corpus.append({'document': path.name, 'document_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'title': ps[0] if ps else '', 'paragraph_count': len(ps),
                       'question_count': sum('Question:' in p for p in ps)})
    records = []
    for i, (topic, name, numbers, question, answer) in enumerate(specs, 1):
        path = kb / name
        ps = paragraphs(path)
        records.append({'test_id': f'FAQ-PILOT-{i:03d}', 'topic': topic,
                        'question': question, 'reference_answer': answer,
                        'reference_review_status': 'REQUIRES_BUSINESS_REVIEW',
                        'reference_passages': [
                            {'document': name, 'document_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                             'paragraph': n, 'text': ps[n-1],
                             'passage_sha256': hashlib.sha256(ps[n-1].encode()).hexdigest()}
                            for n in numbers],
                        'retrieved_contexts': None})
    return {'schema_version': 1, 'dataset_id': 'business-faq-pilot-v1',
            'reference_status': 'REQUIRES_BUSINESS_REVIEW',
            'paragraph_numbering': '1-based document.xml w:p order, including empty paragraphs and tables; not page numbers.',
            'scope': 'Draft local-document FAQ references; not proof of the Azure Search index contents or production policy validity.',
            'corpus_inventory': corpus, 'cases': records}


def markdown(data):
    lines = ['# Business FAQ pilot references', '', '**All references require business review.** No expected answer is approved ground truth.',
             '', 'Reference passages below are local document excerpts, not chunks retrieved by Azure Search.',
             '', data['paragraph_numbering'], '']
    for c in data['cases']:
        lines += [f"## {c['test_id']} — {c['topic']}", '', '**Question:** ' + c['question'],
                  '', '**Draft expected answer:** ' + c['reference_answer'], '', '**Status:** REQUIRES_BUSINESS_REVIEW', '']
        for p in c['reference_passages']:
            lines += [f"**{p['document']} — paragraph {p['paragraph']}**", '', '> ' + p['text'], '',
                      f"Document SHA-256: `{p['document_sha256']}`; passage SHA-256: `{p['passage_sha256']}`.", '']
    return '\n'.join(lines)


if __name__ == '__main__':
    dataset = build()
    out = ROOT / 'datasets'
    out.mkdir(exist_ok=True)
    (out / 'faq_pilot.json').write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + '\n')
    (out / 'faq_pilot.md').write_text(markdown(dataset))
    print('Built 10 draft references with exact document passages; business review required.')
