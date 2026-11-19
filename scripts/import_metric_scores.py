#!/usr/bin/env python3
"""Merge normalized external evaluator results with exact-attempt source evidence."""
import argparse
import json
import math
from pathlib import Path


def merge_scores(evidence, scores):
    records=evidence['attempts']
    records=[{**v,'attempt_id':k} for k,v in records.items()] if isinstance(records,dict) else records
    lookup={r['attempt_id']:r for r in records}
    for score in scores:
        record=lookup.get(score.get('attempt_id'))
        if not record or record.get('response_sha256')!=score.get('response_sha256'):
            raise ValueError('External metric does not match an evidence attempt and digest.')
        if not all(score.get(k) for k in ('metric','method','reason','evaluator','evaluated_at')):
            raise ValueError('External metric requires method, rationale and evaluator provenance.')
        value=score.get('score')
        if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
            raise ValueError('External score must be finite and normalized to [0,1] by a documented method.')
        record.setdefault('assessments',{})[score['metric']]={'score':value,'method':score['method'],'reason':score['reason']}
        record['evaluator']=score['evaluator']
        record['evaluated_at']=score['evaluated_at']
        record['rationale']='External metric import; see per-metric method and rationale.'
        for flag in ('critical_fabrication_observed','unauthorized_disclosure_observed','instruction_hijack_observed'):
            if score.get(flag) is True:
                record[flag]=True
    return {'attempts':records}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--scores',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    try:
        result=merge_scores(json.loads(args.evidence.read_text()),json.loads(args.scores.read_text()))
        args.out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        args.out.touch(mode=0o600,exist_ok=True);args.out.chmod(0o600)
        args.out.write_text(json.dumps(result,indent=2,ensure_ascii=False)+'\n')
        print('Imported scores. Run evidence review to enforce trace requirements and thresholds.')
        return 0
    except (ValueError,OSError,KeyError,TypeError):
        print('Import rejected: verify schema, attempt IDs, response digests and normalized score provenance.')
        return 2

if __name__=='__main__':
    raise SystemExit(main())
