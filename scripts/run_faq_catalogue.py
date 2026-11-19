#!/usr/bin/env python3
"""Plan or capture at most ten informational FAQs from the expanded catalogue."""
import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT
from qa.faq_pilot import digest, catalogue
from qa.runner import run
from scripts.build_faq_catalogue import DATASET, validate


def selected_cases(data,batch=None,ids=None):
    cases=data['cases']
    if batch is not None:
        if not 1<=batch<=(len(cases)+9)//10:raise ValueError('Batch outside catalogue range')
        return cases[(batch-1)*10:batch*10]
    wanted=ids.split(',') if ids else []
    if not 1<=len(wanted)<=10 or len(set(wanted))!=len(wanted) or not set(wanted)<={c['test_id'] for c in cases}:raise ValueError('Select 1..10 unique known IDs')
    return [c for c in cases if c['test_id'] in wanted]


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    selection=parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--batch',type=int);selection.add_argument('--id')
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--allow-draft-references',action='store_true')
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args(argv)
    if args.live and not args.allow_draft_references:raise ValueError('Live exploratory capture requires --allow-draft-references')
    if args.out.exists():raise ValueError('Output exists; preserve captures by choosing a new directory')
    data=validate(json.loads(DATASET.read_text()))
    cases=selected_cases(data,args.batch,args.id)
    args.out.mkdir(parents=True)
    snapshot=args.out/'dataset.snapshot.json';snapshot.write_bytes(DATASET.read_bytes())
    selection_path=args.out/'selection.json'
    selection_path.write_text(json.dumps({'test_ids':[c['test_id'] for c in cases],
        'maximum_attempted_posts':len(cases),'automatic_retries':False,
        'references_are_draft':True,'review_flags':{c['test_id']:{'flags':c['review_flags'],'issues':c['review_issue_ids']} for c in cases}},indent=2)+'\n')
    options=SimpleNamespace(suite='all',id=None,limit=None,live=args.live,demo=False,
                            allow_writes=False,evidence=None,out=str(args.out),frontend_fresh=True)
    print(f'Selected {len(cases)} read-only informational questions. Draft references require business review.')
    scenarios=catalogue({'cases':cases})
    for scenario in scenarios:
        scenario['suite']='business_faq'
    return run(options,cases=scenarios,metadata={
        'faq_catalogue_dataset_sha256':digest(snapshot),'faq_catalogue_dataset_id':data['dataset_id'],
        'faq_catalogue_selection_sha256':digest(selection_path),'reference_status':data['reference_status'],
        'reference_context_boundary':'Local reference passages are not actual retrieved chunks.'})

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError):
        print('Catalogue stopped: check source integrity, selection, draft opt-in and new output path. No automatic retries.',file=sys.stderr)
        raise SystemExit(2)
