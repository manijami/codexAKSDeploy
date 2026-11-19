#!/usr/bin/env python3
"""Render one explicit AKS Job offline; never contact Kubernetes or Azure."""
import argparse,json,re
from pathlib import Path

def render(image,run_id,operation='core',mode='plan',case_ids='BASELINE-001',name=None):
    if not re.fullmatch(r'[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}',image):raise ValueError('Use a complete approved image reference pinned by SHA256 digest')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',run_id):raise ValueError('run-id must be a DNS label')
    if operation not in ('core','full-pack','safety') or mode not in ('plan','live','resume','demo','doctor'):raise ValueError('Invalid operation/mode')
    if operation=='core' and mode=='resume':raise ValueError('Core batches do not support automatic resume')
    if operation!='core' and mode not in ('plan','live','resume'):raise ValueError('Native workflows support plan/live/resume')
    name=name or ('qa-'+run_id+'-'+mode)
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',name):raise ValueError('Job name must be a DNS label; use --name if the run-id is long')
    job=json.loads((Path(__file__).parent/'deployment.json').read_text());job['metadata']['name']=name
    c=job['spec']['template']['spec']['containers'][0];c['image']=image
    c['args']=['--operation',operation,'--mode',mode,'--run-id',run_id]
    if operation=='core':c['args']+=['--case-ids',case_ids]
    else:c['args']+=['--allow-draft-references']
    if operation=='safety':
        c['args']+=['--max-api-requests','102','--max-judge-requests','510','--max-seconds','21600'];job['spec']['activeDeadlineSeconds']=21690
    if mode in ('live','resume'):c['envFrom'][0]['secretRef']['optional']=False
    return job

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--run-id',required=True);p.add_argument('--operation',choices=('core','full-pack','safety'),default='core');p.add_argument('--mode',default='plan');p.add_argument('--case-ids',default='BASELINE-001');p.add_argument('--name');p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    result=render(a.image,a.run_id,a.operation,a.mode,a.case_ids,a.name)
    with a.output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    print('Rendered offline: '+str(a.output))
if __name__=='__main__':main()
