#!/usr/bin/env python3
"""Build a separate, reproducible offline FAQ catalogue from business Word files."""
import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT
from qa.faq_pilot import digest
from scripts.build_faq_pilot import paragraphs
from scripts.faq_catalogue_review import add_curated_issues

DATASET = ROOT / 'datasets/business_faq_v1.json'
SECTION_HEADINGS = {'MTN ID and Biometric Login', 'Contract Cancellation FAQs',
    'Subscription Cancellation FAQs', 'Cooling-Off (CPA) Cancellation FAQs',
    'eSIM Basics', 'Device Compatibility', 'RICA Requirements', 'eSIM Activation',
    'Lost QR Code', 'SIM Replacement and Number Retention', 'Early Termination Fee (ETF)',
    'Early Termination Fee Payment and Cancellation', 'Upgrade Eligibility',
    'Device Cover', 'Upgrade Request Assistance'}
JOURNEY = re.compile(r'^i(?:Join|Recharge|Pay|Use|Help|Leave)$', re.I)


def passage(name, sha, ps, number):
    text = ps[number-1]
    return {'document': name, 'document_sha256': sha, 'paragraph': number, 'text': text,
            'passage_sha256': hashlib.sha256(text.encode()).hexdigest()}


def explicit_units(ps):
    starts = [i for i,t in enumerate(ps) if re.match(r'^\s*Question:', t)]
    units = []
    for n,start in enumerate(starts):
        end = starts[n+1] if n+1 < len(starts) else len(ps)
        answer_start = next((i for i in range(start+1,end) if re.match(r'^\s*Answer:',ps[i])), None)
        if answer_start is None:
            continue
        stop = next((i for i in range(answer_start+1,end) if JOURNEY.match(ps[i].strip()) or ps[i].strip() in SECTION_HEADINGS or ps[i].strip().startswith('FAQ Count:') or 'No approved FAQs available' in ps[i]), end)
        numbers = [i+1 for i in range(answer_start,stop) if ps[i].strip()]
        q = re.sub(r'^\s*Question:\s*','',ps[start]).strip()
        if numbers and any(re.sub(r'^Answer:\s*','',ps[k-1]).strip() for k in numbers):
            units.append({'source_question':q,'question_paragraph':start+1,'numbers':numbers})
    return units


def pi_units(ps):
    result=[]
    for start,end in ((1,9),(10,21),(22,32),(33,44),(45,51),(52,58)):
        a=next(i for i in range(start,end+1) if ps[i-1].startswith('Answer:'))
        result.append({'source_question':re.sub(r'^Question:\s*','',ps[start-1]).strip(),
                       'question_paragraph':start,'numbers':[i for i in range(a,end+1) if ps[i-1].strip()]})
    return result


def atm_units(ps):
    # One small city/site/address lookup from each of the eight source sections.
    starts=[i for i,t in enumerate(ps) if t.startswith('What is the MoMo Express ATM locations')]
    units=[]
    for start in starts:
        header = next(k for k in range(start+1, start+10) if ps[k].strip() == 'Street Address')
        i=header+1  # Blank separators differ by section; locate actual column header.
        city,site,address=ps[i:i+3]
        units.append({'source_question':f'What street address is listed for the MoMo Express ATM site {site.strip()} in {city.strip()}?',
                      'question_paragraph':None,'numbers':[i+1,i+2,i+3],
                      'table_section_paragraph':start+1,
                      'custom_answer':f'{site.strip()} in {city.strip()}: {address.strip()}.'})
    return units


def choose(units, limit=8):
    if len(units)<=limit:return units
    return [units[round(i*(len(units)-1)/(limit-1))] for i in range(limit)]


def build(root=ROOT):
    inventory=[]; cases=[]; issues=[]
    for docno,path in enumerate(sorted((root/'sources/business_kb').glob('*.docx')),1):
        ps=paragraphs(path); sha=digest(path)
        units=explicit_units(ps)
        if path.name=='Pi_ChatBot_Customer_FAQs.docx':units=pi_units(ps)
        if path.name=='NP- MoMo Express ATM Locations.docx':units=atm_units(ps)
        # Store document consists of locator paraphrases, not eight distinct behaviours.
        if path.name=='NP-MTN Store.docx':units=[units[1]]
        unique=[]; seen={}
        for u in units:
            key=' '.join(u['source_question'].lower().split())
            if key in seen:
                issues.append({'issue_id':f'DUP-{docno:02d}-{len(issues)+1:03d}','document':path.name,
                    'kind':'DUPLICATE_SOURCE_QUESTION','paragraphs':[seen[key]['question_paragraph'],u['question_paragraph']],
                    'description':'Duplicate question text; selected only once. Compare both answer blocks before business approval.'})
                continue
            seen[key]=u;unique.append(u)
        selected=choose(unique)
        if path.name=='NP-MTN App-Nex Gen.docx':
            priorities=[u for u in unique if u['source_question'] in ('How long are my Gems valid?', 'What is the end date of the Flip & Win game?')]
            selected=sorted(selected[:6]+priorities,key=lambda u:u['question_paragraph'])
        date=re.search(r'Date:\s*(\d{4}-\d{2}-\d{2})',ps[1] if len(ps)>1 else '')
        inventory.append({'document_id':f'DOC-{docno:02d}','document':path.name,'document_sha256':sha,
            'paragraph_count':len(ps),'available_distinct_source_units':len(unique),'selected_cases':len(selected),
            'document_date':date.group(1) if date else None,
            'selection_note':'At most eight source FAQ blocks, evenly spread in source order; all smaller sets retained. Store locator paraphrases collapsed; ATM eight section lookups; Pi six canonical FAQs.'})
        for unitno,u in enumerate(selected,1):
            refs=[passage(path.name,sha,ps,k) for k in u['numbers']]
            answer=u.get('custom_answer') or '\n'.join(re.sub(r'^\s*Answer:\s*','',p['text']).strip() for p in refs).strip()
            q=u['source_question']
            flags=[]
            if re.search(r'\b20(?:1\d|2\d)\b',answer):flags.append('DATE_OR_CAMPAIGN_VALIDITY_REVIEW')
            if re.search(r'R\s*\d|\b(?:fee|price|charge|interest|banking|account number|payment)\b',q+' '+answer,re.I):flags.append('PRICE_PAYMENT_OR_FINANCIAL_TERMS_REVIEW')
            if re.search(r'\b(?:selected|qualif|eligible|eligibility|certain|where available|may|usually|typically|up to)\b',answer,re.I):flags.append('CONDITIONAL_OR_UNSPECIFIED_SCOPE')
            if re.search(r'\b(?:here|click|select|download)\b',answer,re.I) and 'http' not in answer and re.search(r'link|here|download',answer,re.I):flags.append('LINK_TARGET_NEEDS_VERIFICATION')
            if '[' in answer or 'TBC' in answer or 'TBD' in answer:flags.append('SOURCE_FORMAT_OR_PLACEHOLDER_REVIEW')
            cases.append({'test_id':f'FAQ-DOC-{docno:02d}-{unitno:02d}','document_id':f'DOC-{docno:02d}',
                'topic':ps[0].strip(),'source_question':q,
                'question':'For general information only; do not access or change any account: '+q,
                'source_question_passage':passage(path.name,sha,ps,u['question_paragraph']) if u['question_paragraph'] else None,
                'reference_answer':answer,'reference_review_status':'REQUIRES_BUSINESS_REVIEW',
                'reference_passages':refs,'retrieved_contexts':None,
                'review_flags':flags,'review_issue_ids':[],
                'execution_scope':'READ_ONLY_INFORMATIONAL','selection_kind':'source_faq' if u['question_paragraph'] else 'source_table_lookup',
                'table_section_passage':passage(path.name,sha,ps,u['table_section_paragraph']) if 'table_section_paragraph' in u else None})
    data = {'schema_version':1,'dataset_id':'business-faq-catalogue-v1','reference_status':'REQUIRES_BUSINESS_REVIEW',
        'scope':'Offline local-document draft expectations. No retrieved chunks, approved policy, live execution or scores claimed.',
        'paragraph_numbering':'1-based document.xml w:p order, including empty and table paragraphs; not page numbers.',
        'selection_policy':'Balanced document sample, at most eight distinct FAQ blocks per document; no generated paraphrase padding. App includes two explicit expired-date questions for review.',
        'corpus_inventory':inventory,'source_issues':issues,'cases':cases}
    add_curated_issues(data,root)
    return data


def validate(data, root=ROOT):
    cases=data['cases']; inv=data['corpus_inventory']
    if len(cases)<350 or len(inv)!=58 or len({r['document'] for r in inv})!=58:
        raise ValueError('Need at least 350 cases across all 58 documents')
    if len({c['test_id'] for c in cases})!=len(cases):raise ValueError('Duplicate IDs')
    docs={p.name:p for p in (root/'sources/business_kb').glob('*.docx')}
    if set(docs)!={r['document'] for r in inv}:raise ValueError('Corpus inventory mismatch')
    ps={name:paragraphs(p) for name,p in docs.items()}; shas={name:digest(p) for name,p in docs.items()}
    counts=Counter(c['document_id'] for c in cases)
    for item in inv:
        if item['document_sha256']!=shas[item['document']] or counts[item['document_id']]!=item['selected_cases'] or not item['selected_cases']:raise ValueError('Document coverage/hash mismatch')
    for c in cases:
        if not c['question'] or not c['reference_answer'] or not c['reference_passages'] or c['retrieved_contexts'] is not None or c['reference_review_status']!='REQUIRES_BUSINESS_REVIEW':raise ValueError('Invalid draft case')
        if len({p['document'] for p in c['reference_passages']}) != 1:
            raise ValueError('Each case must bind to one primary document')
        if c['selection_kind']=='source_table_lookup':
            if len(c['reference_passages'])!=3:raise ValueError('ATM row needs three cells')
            city,site,address=[p['text'].strip() for p in c['reference_passages']]
            if {city,site,address} & {'City','Site Name','Street Address'}:raise ValueError('ATM header is not data')
            expected=f'{site} in {city}: {address}.'
        else:
            expected='\n'.join(re.sub(r'^\s*Answer:\s*','',p['text']).strip() for p in c['reference_passages']).strip()
        if c['reference_answer']!=expected:raise ValueError('Draft answer differs from extractive source')
        if c['question']!='For general information only; do not access or change any account: '+c['source_question']:raise ValueError('Question binding mismatch')
        refs=c['reference_passages']+[p for p in (c.get('source_question_passage'),c.get('table_section_passage')) if p]
        for p in refs:
            name=p['document']
            if p['document_sha256']!=shas[name] or ps[name][p['paragraph']-1]!=p['text'] or hashlib.sha256(p['text'].encode()).hexdigest()!=p['passage_sha256']:raise ValueError('Exact source passage mismatch')
    issue_ids={i['issue_id'] for i in data['source_issues']}
    if len(issue_ids)!=len(data['source_issues']):raise ValueError('Duplicate issue IDs')
    for c in cases:
        if not set(c['review_issue_ids'])<=issue_ids:raise ValueError('Unknown review issue')
    for issue in data['source_issues']:
        for p in issue.get('source_passages',[]):
            name=p['document']
            if p['document_sha256']!=shas[name] or ps[name][p['paragraph']-1]!=p['text'] or hashlib.sha256(p['text'].encode()).hexdigest()!=p['passage_sha256']:raise ValueError('Review issue source mismatch')
    return data


def markdown(data):
    lines=['# Expanded business FAQ draft catalogue','','All cases require business review. Supporting passages are local document references, never claimed retrieved chunks. No scores assigned.','',data['paragraph_numbering'],'']
    for c in data['cases']:
        lines += [f"## {c['test_id']} — {c['topic']}",'','**Question:** '+c['question'],'','**Draft expected answer:**','',c['reference_answer'],'','**Business status:** '+c['reference_review_status'],'','**Review flags:** '+(', '.join(c['review_flags']+c['review_issue_ids']) or 'No automatic flag; business review still required.'),'']
        for p in c['reference_passages']:
            lines += [f"**{p['document']}, paragraph {p['paragraph']}**",'','```text',p['text'],'```','',f"Document SHA-256: `{p['document_sha256']}`; passage SHA-256: `{p['passage_sha256']}`.",'']
    return '\n'.join(lines)


def write_outputs(data):
    validate(data)
    DATASET.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
    DATASET.with_suffix('.md').write_text(markdown(data))
    out=ROOT/'reports/faq_expansion';out.mkdir(exist_ok=True)
    with (out/'document_coverage.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['document_id','document','document_sha256','document_date','available_distinct_source_units','selected_cases','test_ids','flagged_cases','review_status'])
        for d in data['corpus_inventory']:
            cs=[c for c in data['cases'] if c['document_id']==d['document_id']]
            writer.writerow([d[k] for k in ('document_id','document','document_sha256','document_date','available_distinct_source_units','selected_cases')]+[';'.join(c['test_id'] for c in cs),sum(bool(c['review_flags'] or c['review_issue_ids']) for c in cs),'REQUIRES_BUSINESS_REVIEW'])
    lines=['# Document-to-test coverage','','Counts describe selected draft cases, not policy, retrieval or behavioural coverage. Source FAQ paraphrases can overlap.','', '| Document | Available distinct source units | Selected tests | Test IDs |','|---|---:|---:|---|']
    for d in data['corpus_inventory']:
        cs=[c for c in data['cases'] if c['document_id']==d['document_id']]
        lines.append(f"| {d['document']} | {d['available_distinct_source_units']} | {len(cs)} | {', '.join(c['test_id'] for c in cs)} |")
    (out/'document_coverage.md').write_text('\n'.join(lines)+'\n')
    issue_lines=['# Source ambiguities and potential conflicts','','No issue here changes a reference or establishes a resolved policy. Exact cited passages are retained in the JSON catalogue. Automated flags are triage hints, not exhaustive conflict detection. All 58 documents still require business review.','']
    for issue in data['source_issues']:
        issue_lines += [f"## {issue['issue_id']} — {issue['kind']}",'',issue['description'],'']
        for p in issue.get('source_passages',[]):
            issue_lines += [f"**{p['document']} — paragraph {p['paragraph']}**",'','```text',p['text'],'```','',f"Document SHA-256: `{p['document_sha256']}`; passage SHA-256: `{p['passage_sha256']}`.",'']
        issue_lines += ['Related selected tests: '+(', '.join(issue.get('related_test_ids',[])) or 'See document coverage; source issue may span unselected FAQ blocks.'),'']
    (out/'source_review_issues.md').write_text('\n'.join(issue_lines))
    lines=['# Bounded FAQ execution batches','','Run each command separately only when ready. No loop or automatic continuation. Each command captures at most ten informational questions; no evaluator calls. All expectations remain drafts. Existing QA identity/subscriber type and transport limits are reused. POSTPAID on the prepaid route is preserved; applicability still needs API-owner/business confirmation.','',"Start in this project and run `.venv/bin/python run.py doctor`. Omit `--live --allow-draft-references` for an offline plan. Each output directory must be new. After each live batch inspect SSE mapping, errors, request budget and review flags before selecting another batch. No automatic POST retries.",'','```bash',"cd '/Users/subhangisilla/dev/Whatsapp-Test - codex/mtn-whatsapp-qa'",'```','']
    for i in range((len(data['cases'])+9)//10):
        lines += [f'Batch {i+1:03d}:','','```bash',f'.venv/bin/python scripts/run_faq_catalogue.py --batch {i+1} --live --allow-draft-references --out reports/faq_catalogue_live/batch_{i+1:03d}_run1','```','']
    (out/'batch_commands.md').write_text('\n'.join(lines))
    print(f"Built {len(data['cases'])} draft cases across {len(data['corpus_inventory'])} documents.")


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--check',action='store_true');args=parser.parse_args()
    if args.check:
        data=validate(json.loads(DATASET.read_text())); print(f"Validated {len(data['cases'])} cases, exact passages and all 58 source hashes.")
    else:write_outputs(build())

if __name__=='__main__':main()
