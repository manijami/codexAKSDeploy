#!/usr/bin/env python3
"""Make an unscored annotation worksheet bound to exact captured attempts."""
import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    data=json.loads(args.results.read_text())
    records=[]
    for result in data['results']:
        for response in result['actual_api_response']:
            if result['suite']=='smoke':
                continue
            records.append({'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],
              'evaluator':'','evaluated_at':'','rationale':'','reference_answer':'',
              'contexts':[],'tool_outputs':[],'authorization_trace':[],
              'labels':{},'assessments':{},
              'required_metrics':response.get('required_metrics',[]),
              'critical_fabrication_observed':None,'unauthorized_disclosure_observed':None,'instruction_hijack_observed':None})
    args.out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    args.out.touch(mode=0o600,exist_ok=True)
    args.out.chmod(0o600)
    args.out.write_text(json.dumps({'attempts':records},indent=2)+'\n')
    print('Created unscored worksheet with '+str(len(records))+' attempts. Add actual sources and annotations before review.')

if __name__=='__main__':
    main()
