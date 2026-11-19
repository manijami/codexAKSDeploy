"""Explicit, passage-bound source issues for business review; no policy corrections."""
import hashlib
import json
from pathlib import Path
from scripts.build_faq_pilot import paragraphs

# Locations refer to the immutable local source version, not current policy claims.
# Curated source-review issues are private, required inputs.


def add_curated_issues(data,root):
    path=Path(root)/'datasets/faq_source_issues.private.json'
    if not path.exists():raise ValueError('Supply approved private source-review issues; never silently omit exclusions')
    issues=json.loads(path.read_text())
    cache={}
    for issue_id,kind,description,locations in issues:
        refs=[]
        for name,numbers in locations:
            if name not in cache:
                path=Path(root)/'sources/business_kb'/name
                cache[name]=(paragraphs(path),hashlib.sha256(path.read_bytes()).hexdigest())
            ps,sha=cache[name]
            for n in numbers:
                text=ps[n-1]
                refs.append({'document':name,'document_sha256':sha,'paragraph':n,'text':text,
                             'passage_sha256':hashlib.sha256(text.encode()).hexdigest()})
        affected=[]
        names={name for name,_ in locations}
        # Conservatively link document-related cases, including ones whose draft
        # expectations omit the exact disputed block. This is a review scope, not a grade.
        for case in data['cases']:
            if case['reference_passages'][0]['document'] in names:
                affected.append(case['test_id']);case['review_issue_ids'].append(issue_id)
        data['source_issues'].append({'issue_id':issue_id,'kind':kind,'description':description,
            'source_passages':refs,'related_test_ids':affected,'status':'REQUIRES_BUSINESS_REVIEW'})
