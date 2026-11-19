#!/usr/bin/env python3
"""AKS adapter for fresh, bounded QA runs; no historical fingerprint migration."""
import argparse,fcntl,json,os,re,signal,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from qa.config import ROOT,load,target
from qa.campaign_io import atomic,immutable,fingerprint,Ledger
from qa.campaign_plan import runtime_binding
from qa.faq_pilot import digest


def bootstrap_profile(root=ROOT):
    source=root/'deploy/aks/evaluator-profile.json'
    dest=root/'reports/faq_expansion/first_live_selection/evaluator_profile.json'
    if dest.exists():
        if dest.read_bytes()!=source.read_bytes():raise ValueError('Persisted evaluator profile differs from image; it will not be overwritten')
    else:
        dest.parent.mkdir(parents=True,exist_ok=True)
        fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o444)
        with os.fdopen(fd,'wb') as f:f.write(source.read_bytes())
    return dest


def selection(ids):
    from qa.runner import cases_from
    names=ids.split(',')
    if len(names)!=len(set(names)) or not 1<=len(names)<=10:raise ValueError('Select 1..10 distinct core case IDs')
    lookup={c['id']:c for c in cases_from()}
    if any(n not in lookup for n in names):raise ValueError('Unknown selected core case')
    cases=[lookup[n] for n in names]
    if any(c.get('writes') or c.get('payment') or c.get('manual_only') or c.get('suite')=='isolation' or 'B' in c.get('requires_accounts',[]) for c in cases):raise ValueError('AKS core adapter excludes writes/payments/channel and A/B authorization cases')
    if sum(len(c.get('turns',[{}])) for c in cases)>20:raise ValueError('Core batch exceeds 20 requests')
    return cases


def binding(directory,args,config,env):
    params={'operation':args.operation,'case_ids':args.case_ids if args.operation=='core' else None,
            'allow_draft_references':args.allow_draft_references,'max_api_requests':args.max_api_requests,
            'max_judge_requests':args.max_judge_requests,'max_seconds':args.max_seconds,
            'entrypoint_sha256':digest(Path(__file__)),'image_profile_sha256':digest(ROOT/'deploy/aks/evaluator-profile.json')}
    # Include all shipped executable/reference inputs in the wrapper binding.
    for folder in ('qa','scripts','suites','datasets','sources'):
        params[folder+'_sha256']={str(p.relative_to(ROOT)):digest(p) for p in sorted((ROOT/folder).rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc',)}
    keypath=directory/'runtime.private.key';path=directory/'parameters.json'
    if path.exists():
        key=keypath.read_bytes();saved=json.loads(path.read_text())
        if saved!={'parameters':params,'binding':runtime_binding(config,env,key)}:raise ValueError('Run inputs, code or private configuration changed; do not bypass the binding')
    else:
        key=os.urandom(32)
        fd=os.open(keypath,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'wb') as f:f.write(key)
        immutable(path,{'parameters':params,'binding':runtime_binding(config,env,key)})


def preflight(args,config,env):
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',args.run_id):raise ValueError('run-id must be a DNS label, at most 63 characters')
    if not 1<=args.max_api_requests<=600 or not 1<=args.max_judge_requests<=6000 or not 60<=args.max_seconds<=86400:raise ValueError('Caps exceed supported bounded limits')
    if args.operation=='core':selection(args.case_ids)
    elif args.mode not in ('plan','live','resume'):raise ValueError('Native workflows support plan/live/resume')
    if args.mode=='resume' and args.operation=='core':raise ValueError('Core batches cannot auto-resume; inspect the saved reservation and use a new ID only for explicitly selected unrun work')
    if args.operation=='safety' and (args.max_api_requests,args.max_judge_requests,args.max_seconds)!=(102,510,21600):raise ValueError('Expanded safety uses fixed 102 QA / 510 judge / 21600-second limits')
    if args.mode in ('live','resume'):
        if target(config,env,include_key=False)!='https://qa-serviceauth.mtn.co.za/morpheus-v5/api/prepaid/text?platform=whatsapp':raise ValueError('Live target must remain the approved QA endpoint')
        if not env.get('QA_MSISDN_A') or not(env.get('MORPHEUS_API_KEY') or env.get('QA_API_KEY_A')):raise ValueError('Designated QA identity or API credential missing')
        if not config.get('response_mapping_confirmed'):raise ValueError('Confirm SSE mapping before live testing')
    if args.operation!='core':
        if not args.allow_draft_references:raise ValueError('Explicit draft-reference/rubric opt-in is required')
        for name in ('ragas','deepeval') if args.operation=='full-pack' else ('deepeval',):
            if not (ROOT/f'.venv-{name}/bin/python').exists():raise ValueError('Use the full image with the required native evaluator environments')


def dispatch(args,directory):
    if args.operation=='core':
        from qa.runner import main as core
        if args.mode=='doctor':return core(['doctor'])
        argv=['run','--id',args.case_ids,'--limit','10','--out',str(directory/args.mode)]
        if args.mode=='live':argv+=['--live','--frontend-fresh']
        elif args.mode=='demo':argv+=['--demo']
        return core(argv)
    if args.operation=='full-pack':
        from qa.campaign import main as campaign
        caps=['--max-api-requests',str(args.max_api_requests),'--max-judge-requests',str(args.max_judge_requests),'--max-seconds',str(args.max_seconds),'--allow-draft-references']
        if args.mode=='plan':return campaign(['plan','--out',str(directory/'plan/full-pack'),*caps])
        if args.mode=='live':return campaign(['execute','--out',str(directory/'execution/full-pack'),*caps])
        if args.mode=='resume':return campaign(['resume','--campaign',str(directory/'execution/full-pack'),*caps])
        raise ValueError('full-pack supports plan/live/resume')
    if args.mode not in ('plan','live','resume'):raise ValueError('safety supports plan/live/resume')
    from qa.safety_pack import make_plan
    from qa.safety_response_continuation import prepare,validate,execute,render,read
    from qa.config import secrets
    config,env=load();parent=directory/'execution/safety-parent';child=directory/'execution/safety'
    if not (child/'manifest.json').exists():
        if args.mode=='resume':raise ValueError('No saved safety execution exists')
        # Fresh zero-request parent; no expired Mac history is imported/rebound.
        old,state=make_plan(parent,config,env)
        Ledger(parent/'requests.sqlite',{'api':102,'judge':510},old['expires_at'])
        manifest,state=prepare(child,parent,config,env)
    else:manifest,state=read(child/'manifest.json'),read(child/'checkpoint.json')
    key=validate(child,manifest,state,config,env)
    if args.mode=='plan':render(child,manifest,state,secrets(env));print('Safety plan: '+str(child/'report.html'));return 0
    return execute(child,manifest,state,config,env,key)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--operation',choices=('core','full-pack','safety'),default='core')
    p.add_argument('--mode',choices=('doctor','plan','demo','live','resume'),default='plan')
    p.add_argument('--run-id',required=True);p.add_argument('--case-ids',default='BASELINE-001')
    p.add_argument('--allow-draft-references',action='store_true')
    p.add_argument('--max-api-requests',type=int,default=600);p.add_argument('--max-judge-requests',type=int,default=6000);p.add_argument('--max-seconds',type=int,default=86400)
    args=p.parse_args(argv);config,env=load();preflight(args,config,env)
    bootstrap_profile();root=ROOT/'reports/aks';root.mkdir(exist_ok=True)
    with (root/'execution.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Another AKS QA job already holds this report-volume lock')
        directory=root/args.run_id;directory.mkdir(exist_ok=True);binding(directory,args,config,env)
        if args.mode in ('live','resume'):
            reservation=directory/'live_reservation.json'
            if args.mode=='live' and reservation.exists():raise ValueError('Live execution already reserved; never replay it automatically. Inspect artifacts and use supported resume')
            if args.mode=='resume' and not reservation.exists():raise ValueError('No matching execution reservation exists')
            if not reservation.exists():immutable(reservation,{'operation':args.operation,'run_id':args.run_id,'status':'reserved_before_live_dispatch'})
        if args.operation=='core' and args.mode=='live':
            # Never raise the configured core ceiling above the 20-request batch cap.
            if config['max_live_requests']>20:raise ValueError('Core private configuration exceeds 20-request batch cap')
        def expired(*_):raise TimeoutError('Bounded invocation deadline reached; saved reservation prevents replay')
        previous=signal.signal(signal.SIGALRM,expired)
        signal.alarm(args.max_seconds)
        try:code=dispatch(args,directory)
        finally:signal.alarm(0);signal.signal(signal.SIGALRM,previous)
        atomic(directory/('invocation_'+args.mode+'.json'),{'operation':args.operation,'mode':args.mode,'exit_code':code,'note':'Exit code 2 can mean inconclusive evidence or a partial bounded run; inspect report/checkpoint. A completed Kubernetes Job is not QA acceptance.'})
        print('Persistent artifacts: '+str(directory));return code

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError,RuntimeError):
        print('AKS QA refused: verify configuration, run identity, immutable inputs, selected scope and checkpoint. No private values displayed.',file=sys.stderr);raise SystemExit(2)
