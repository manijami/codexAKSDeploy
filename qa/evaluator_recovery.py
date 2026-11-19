"""Read-only-source, quota-only evaluator recovery with independent credential binding.

This does not weaken Campaign.resume. The original aggregate credential binding
cannot authorize a rotated key; a new manifest authorizes evaluator-only work.
"""
import argparse
from contextlib import contextmanager,closing
import copy
import fcntl
import hashlib
import hmac
import html
import json
import math
import os
import signal
import sqlite3
import subprocess
import time
from datetime import datetime,timezone
from pathlib import Path

from .config import ROOT,load,merge,secrets
from .campaign_io import Ledger,BudgetStop,atomic,immutable,fingerprint
from .campaign_plan import code_fingerprints,package_versions
from .campaign_privacy import sanitize
from .campaign_report import exports
from .faq_pilot import digest
from .faq_catalogue_evaluation import frozen_catalogue

GENERATION={'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}
OWN_FILES=('qa/evaluator_recovery.py','scripts/recover_quota_evaluations.py')


def now():return datetime.now(timezone.utc).isoformat()


def read_json(path):return json.loads(Path(path).read_text())


@contextmanager
def ro_db(path):
    connection=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:yield connection
    finally:connection.close()


def source_inventory(directory):
    # Private values are not copied into the recovery plan or exported report.
    return {str(p.relative_to(directory)):digest(p) for p in sorted(directory.rglob('*'))
            if p.is_file() and not p.name.endswith('.lock') and 'private' not in p.name}


def configured_settings():
    config=read_json(ROOT/'config/settings.json');local=ROOT/'config/local.json'
    return merge(config,read_json(local)) if local.exists() else config


def validate_profile(profile):
    if profile['generation_parameters']!=GENERATION or not profile.get('both_accuracy_judges_required') or profile.get('sdk_retries')!=0 or profile.get('structured_attempts')!=1 or not profile['model'].startswith('nvidia/'):
        raise ValueError('Original evaluator generation/completion policy mismatch')


def audit_source(directory,check_runtime=True):
    directory=Path(directory);manifest=read_json(directory/'manifest.json');state=read_json(directory/'checkpoint.json')
    if manifest['mode']!='execute' or state['manifest_sha256']!=digest(directory/'manifest.json'):raise ValueError('Original manifest integrity failed')
    if digest(directory/'dataset.snapshot.json')!=manifest['source_hashes']['business_faq']:raise ValueError('Frozen reference snapshot mismatch')
    frozen=frozen_catalogue(read_json(directory/'dataset.snapshot.json'))
    if code_fingerprints()!=manifest['code_sha256']:raise ValueError('Original campaign/evaluator source code fingerprint changed')
    if check_runtime:
        if fingerprint(configured_settings())!=manifest['binding']['config']:raise ValueError('Original noncredential configuration changed')
        for package in ('ragas','deepeval'):
            if package_versions(ROOT/f'.venv-{package}'/'bin/python')!=manifest['packages'][package]:raise ValueError('Original evaluator package versions changed')
        for name,sha in manifest['source_hashes'].items():
            path=ROOT/('datasets/business_faq_v1.json' if name=='business_faq' else 'reports/faq_expansion/first_live_selection/evaluator_profile.json' if name=='evaluator_profile' else name)
            if digest(path)!=sha:raise ValueError('Original catalogue/reference/profile source fingerprint changed')
    validate_profile(manifest['evaluator'])
    cases={c['id']:c for c in manifest['cases']};job_index={};valid_scores=[]
    for case_id,record in state['cases'].items():
        if case_id not in cases:raise ValueError('Unknown original checkpoint case')
        case=cases[case_id]
        if case.get('faq_reference') and case['faq_reference']!=frozen.get(case_id):raise ValueError('Manifest reference differs from frozen snapshot')
        artifacts=record.get('response_artifact_sha256',{})
        if len(artifacts)!=len(record.get('responses',[])):raise ValueError('Original response artifact count mismatch')
        for relative,sha in artifacts.items():
            artifact=directory/relative
            if digest(artifact)!=sha:raise ValueError('Original response artifact changed')
            saved=read_json(artifact)
            matches=[(i,r) for i,r in enumerate(record['responses']) if r['attempt_id']==saved['response']['attempt_id']]
            if len(matches)!=1 or matches[0][1]!=saved['response'] or record['payloads'][matches[0][0]]!=saved['payload']:raise ValueError('Original response/payload checkpoint mismatch')
        for metric,histories in record.get('metrics',{}).items():
            for grade in histories:
                if grade.get('inflight'):continue  # Not a proven quota failure; never auto-replay.
                path=directory/grade['path']
                if not grade.get('artifact_sha256') or digest(path)!=grade['artifact_sha256']:raise ValueError('Original evaluator result artifact changed')
                saved=read_json(path)
                for field in ('score','method','job_id','material_sha256','profile_sha256'):
                    if saved.get(field)!=grade.get(field):raise ValueError('Original evaluator result/checkpoint mismatch')
                job_id=grade['job_id'];job_path=directory/'jobs'/(job_id.replace(':','_')+'.json');job=read_json(job_path)
                if job['job_id']!=job_id or job['metric']!=metric or job['profile']!=manifest['evaluator'] or fingerprint(job['material'])!=grade['material_sha256'] or job['material_sha256']!=grade['material_sha256'] or grade['profile_sha256']!=fingerprint(manifest['evaluator']):raise ValueError('Original job/material/profile binding mismatch')
                binding=fingerprint({'metric':metric,'material':grade['material_sha256'],'evaluator':manifest['evaluator'],'code':manifest['code_sha256']})
                if grade['binding']!=binding or job_id!=binding+':'+str(grade['evaluation_attempt']):raise ValueError('Original metric binding mismatch')
                material=job['material'];response=next((r for r in record.get('responses',[]) if r['attempt_id']==grade['attempt_id']),None)
                if response is None or response['response_sha256']!=grade['response_sha256'] or material['attempt_id']!=grade['attempt_id'] or material['response_sha256']!=grade['response_sha256'] or material['response']!=response['answer']:raise ValueError('Original exact-answer binding mismatch')
                turn=int(grade['attempt_id'].rsplit(':',1)[1])-1
                expected_question=sanitize(case.get('turns',[{}])[turn].get('message',''))
                if material['user_input']!=expected_question:raise ValueError('Original question binding mismatch')
                if case.get('faq_reference') and material.get('reference')!=sanitize(frozen[case_id]['reference_answer']):raise ValueError('Job reference differs from frozen reference')
                job_index[job_id]={'test_id':case_id,'metric':metric,'grade':grade,'job':job,'record':record,'response':response}
                if grade.get('score') is not None:valid_scores.append({'test_id':case_id,'metric':metric,'job_id':job_id,'score':grade['score']})
    with ro_db(directory/'requests.sqlite') as db:
        caps={kind:{'maximum':maximum,'expires':expires} for kind,maximum,expires in db.execute('SELECT kind,maximum,expires FROM caps')}
        counts=dict(db.execute('SELECT kind,count(*) FROM calls GROUP BY kind'))
        quota=list(db.execute('SELECT job,status,retry_after,coalesce(finished,started) FROM calls WHERE kind=\'judge\' AND status IN (402,429) ORDER BY id'))
    if caps['judge']['maximum']!=manifest['limits']['judge_requests'] or caps['api']['maximum']!=manifest['limits']['api_requests'] or any(v['expires']!=state['expires_at'] for v in caps.values()) or counts!=state['ledger_counts']:raise ValueError('Original ledger/checkpoint ceiling/count mismatch')
    selected=[];excluded=[]
    for job_id,status,retry_after,finished in quota:
        if job_id not in job_index:raise ValueError('Quota ledger event has no exact saved metric job')
        item=job_index[job_id];grade=item['grade'];r=item['response'];record=item['record'];metric=item['metric'];job=item['job']
        same=[g for g in record['metrics'][metric] if g.get('binding')==grade['binding']]
        reason=None
        if grade.get('score') is not None or any(g.get('score') is not None for g in same):reason='A matching evaluation already has a valid score'
        elif record['status'] in ('FAIL','BLOCKED','NOT_RUN') or r.get('status_code')!=200 or r.get('application_status')!='success' or r.get('complete') is not True or r.get('error') or not r.get('answer'):reason='Actual chatbot failure, incomplete response or missing answer; excluded'
        elif len(same)>=manifest['limits']['max_evaluator_attempts']:reason='Original evaluation-attempt limit reached'
        elif any(word in metric for word in ('faithfulness','context_')) and not job['material'].get('retrieved_contexts'):reason='Missing exact retrieved context; excluded'
        elif job['request_limit']!=manifest['metric_definitions'][metric]['max_requests'] or job['threshold']!=manifest['metric_definitions'][metric]['threshold']:raise ValueError('Original metric limits/threshold mismatch')
        if reason:excluded.append({'job_id':job_id,'reason':reason});continue
        if any(x['source_job_id']==job_id for x in selected):continue
        selected.append({'source_job_id':job_id,'test_id':item['test_id'],'metric':metric,'binding':grade['binding'],
            'attempt_id':grade['attempt_id'],'response_sha256':grade['response_sha256'],'material_sha256':grade['material_sha256'],
            'request_limit':job['request_limit'],'next_evaluation_attempt':len(same)+1,
            'http_status':status,'retry_not_before':finished+int(retry_after) if retry_after else finished})
    return manifest,state,job_index,selected,excluded,caps,counts,valid_scores


def new_binding(config,env,key,profile):
    from urllib.parse import urlsplit
    base=env.get('MORPHEUS_EVAL_BASE_URL','');parsed=urlsplit(base)
    allowed=json.loads(env.get('MORPHEUS_EVAL_ALLOWED_HOSTS_JSON','[]'))
    if parsed.scheme!='https' or parsed.hostname not in allowed or parsed.hostname=='qa-serviceauth.mtn.co.za' or parsed.username or parsed.password or parsed.query or parsed.fragment or not env.get('MORPHEUS_EVAL_API_KEY') or env.get('MORPHEUS_EVAL_MODEL')!=profile['model']:raise ValueError('Approved evaluator configuration missing or drifted')
    private={k:v for k,v in env.items() if k.startswith(('MORPHEUS_','QA_'))}
    return {'config':fingerprint(config),'evaluator_and_identity_binding':hmac.new(key,json.dumps(private,sort_keys=True).encode(),hashlib.sha256).hexdigest(),
            'authorization':'User explicitly authorized the locally rotated evaluator key for a new evaluator-only recovery. Original aggregate credential fingerprint is preserved, not waived or claimed equal.'}


def verify_source_files(directory,plan):
    current=source_inventory(directory)
    if current!=plan['source_files_sha256']:raise ValueError('Original campaign artifacts changed since recovery was planned')


def render(directory,plan,original,state,recovery_state,private=()):
    combined=copy.deepcopy(state);source_errors=[];recovered=[];retry_errors=[]
    for case_id,record in state['cases'].items():
        for metric,histories in record.get('metrics',{}).items():
            for g in histories:
                if g.get('score') is None and g.get('error'):source_errors.append({'test_id':case_id,'metric':metric,'source_job_id':g.get('job_id'),'exception_type':g.get('exception_type'),'error':g['error']})
    for entry in recovery_state.get('jobs',{}).values():
        if not entry.get('result'):continue
        result=entry['result'];target=next(t for t in plan['selected'] if t['source_job_id']==entry['source_job_id'])
        addition={**result,'binding':target['binding'],'attempt_id':target['attempt_id'],'response_sha256':target['response_sha256'],
            'evaluation_attempt':target['next_evaluation_attempt'],'provisional':True,'evaluated_at':entry['finished_at'],
            'source_job_id':target['source_job_id'],'recovery_only':True}
        combined['cases'][target['test_id']]['metrics'][target['metric']].append(addition)
        row={'test_id':target['test_id'],'metric':target['metric'],'source_job_id':target['source_job_id'],
             'recovery_job_id':result['job_id'],'score':result.get('score'),'status':'RECOVERED' if result.get('score') is not None else 'STILL_UNASSESSED',
             'error':result.get('error'),'response_sha256':target['response_sha256']}
        (recovered if result.get('score') is not None else retry_errors).append(row)
    recovered_ids={r['source_job_id'] for r in recovered}
    remaining=[r for r in source_errors if r['source_job_id'] not in recovered_ids]
    counts=Ledger(directory/'requests.sqlite').counts() if (directory/'requests.sqlite').exists() else plan['original_ledger_counts']
    combined.update(run_status='PARTIAL',updated_at=now(),stop_reason='Evaluator-only recovery '+recovery_state['status']+'. Original campaign remains partial; unrun tests were not executed.',ledger_counts=counts)
    report=exports(directory,original,combined,private)
    information={'status':recovery_state['status'],'original_campaign_id':original['campaign_id'],
        'original_campaign_directory':plan['source_directory'],'original_stop_reason':state.get('stop_reason'),
        'original_artifacts_unchanged':True,'morpheus_requests_added':0,'judge_requests_added':counts.get('judge',0)-plan['original_ledger_counts'].get('judge',0),
        'original_failure_count':len(source_errors),'historical_failures_retained':True,'recovered':recovered,'remaining_original_failures':remaining,
        'recovery_failures':retry_errors,'generation_settings':original['evaluator'],'unrun_tests_remain_unrun':True,
        'credential_transition':'New evaluator-only credential binding; original campaign private fingerprint is not bypassed. Original API/capture code is never invoked.',
        'source_artifact_paths_note':'Original response/job/metric artifact paths refer to the original campaign directory. Recovery artifacts are stored separately.'}
    report['quota_recovery']=information;atomic(directory/'results.json',sanitize(report,private));atomic(directory/'recovery_summary.json',sanitize(information,private));atomic(directory/'derived_checkpoint.json',combined)
    def table(rows):
        return '<table><tr><th>Test</th><th>Metric</th><th>Status/error</th><th>Score</th></tr>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in (r['test_id'],r['metric'],r.get('status') or r.get('exception_type') or r.get('error'),r.get('score')) )+'</tr>' for r in rows)+'</table>'
    section='<section class="box" id="quota-recovery"><h2>Evaluator-only quota recovery — '+html.escape(recovery_state['status'])+'</h2><p>Original artifacts and failure history retained. No Morpheus requests added. Original campaign remains PARTIAL; unrun tests remain unrun.</p><p>Recovered: '+str(len(recovered))+'; remaining original evaluator failures: '+str(len(remaining))+'; historical original failures: '+str(len(source_errors))+'. Judge-error counts elsewhere include preserved history.</p><h3>Recovered scores</h3>'+table(recovered)+'<h3>Remaining failures (not silently retried)</h3>'+table(remaining+retry_errors)+'</section>'
    path=directory/'report.html';text=path.read_text().replace('</header>','</header>'+section,1).replace('<th>Judge errors</th>','<th>Historical judge errors</th>');path.write_text(text)
    (directory/'summary.md').write_text('# Evaluator-only quota recovery\n\nOriginal campaign remains PARTIAL. '+str(len(recovered))+' recovered; '+str(len(remaining))+' original evaluator failures remain; 0 Morpheus requests added.\n\nAll draft comparisons provisional. Retrieval, groundedness, authorization and safety remain unassessed without supporting evidence.\n')


def worker(job,out,seconds,runtime_env=None):
    package='ragas' if job['metric'].startswith('ragas_') else 'deepeval'
    path=out.parent.parent/'jobs'/(job['job_id'].replace(':','_')+'.json');immutable(path,job)
    child_environment=None
    if runtime_env is not None:
        child_environment={**os.environ,**{k:v for k,v in runtime_env.items() if k.startswith(('MORPHEUS_','QA_'))}}
    process=subprocess.Popen([str(ROOT/f'.venv-{package}'/'bin/python'),str(ROOT/'scripts/campaign_judge.py'),'--job',str(path),'--out',str(out)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=child_environment)
    try:process.wait(timeout=max(0.1,seconds))
    except BaseException:
        process.terminate()
        try:process.wait(timeout=3)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        raise
    if not out.exists():raise ValueError('Recovery evaluator produced no artifact')
    return read_json(out)


def execute(directory,plan,manifest,source_state,jobs,run_worker=worker):
    verify_source_files(Path(plan['source_directory']),plan)
    config,env=load();key_path=directory/'credential.private.key'
    state_path=directory/'recovery_checkpoint.json'
    if not key_path.exists():key_path.write_bytes(os.urandom(32));os.chmod(key_path,0o600)
    binding=new_binding(config,env,key_path.read_bytes(),manifest['evaluator'])
    if binding['config']!=manifest['binding']['config']:raise ValueError('Original configuration drifted')
    binding_path=directory/'credential_binding.json'
    if binding_path.exists():
        if read_json(binding_path)!=binding:raise ValueError('Recovery credential/configuration binding changed')
    else:immutable(binding_path,binding)
    if state_path.exists():state=read_json(state_path)
    else:state={'plan_sha256':digest(directory/'recovery_manifest.json'),'jobs':{},'status':'READY'}
    if state['plan_sha256']!=digest(directory/'recovery_manifest.json'):raise ValueError('Recovery manifest changed')
    ledger_path=directory/'requests.sqlite'
    if not ledger_path.exists():
        with ro_db(Path(plan['source_directory'])/'requests.sqlite') as source,closing(sqlite3.connect(ledger_path)) as destination:source.backup(destination)
    ledger=Ledger(ledger_path)
    allowed={t['binding']+':'+str(t['next_evaluation_attempt']) for t in plan['selected']}
    with ro_db(ledger_path) as db:
        caps={kind:{'maximum':maximum,'expires':expires} for kind,maximum,expires in db.execute('SELECT kind,maximum,expires FROM caps')}
        if caps!=plan['original_caps']:raise ValueError('Recovery ledger changed original caps or deadline')
        counts=dict(db.execute('SELECT kind,count(*) FROM calls GROUP BY kind'))
        with ro_db(Path(plan['source_directory'])/'requests.sqlite') as original_db:
            original_calls=list(original_db.execute('SELECT * FROM calls ORDER BY id'))
        last_original=original_calls[-1][0] if original_calls else 0
        if list(db.execute('SELECT * FROM calls WHERE id<=? ORDER BY id',(last_original,)))!=original_calls:raise ValueError('Copied ledger original request history changed')
        added=list(db.execute('SELECT kind,job FROM calls WHERE id>?',(last_original,)))
        if any(kind!='judge' or job not in allowed for kind,job in added):raise ValueError('Recovery ledger contains an out-of-scope request')
        if counts.get('api',0)!=plan['original_ledger_counts'].get('api',0):raise ValueError('Recovery ledger contains forbidden Morpheus requests')
        if counts.get('judge',0)-plan['original_ledger_counts'].get('judge',0)>sum(t['request_limit'] for t in plan['selected']):raise ValueError('Quota-only recovery request scope exceeded')
    for source_id,entry in state['jobs'].items():
        if source_id not in jobs or entry['job']['job_id'] not in allowed:raise ValueError('Unknown recovery job in checkpoint')
        expected=copy.deepcopy(jobs[source_id]['job']);target=next(t for t in plan['selected'] if t['source_job_id']==source_id)
        expected['job_id']=target['binding']+':'+str(target['next_evaluation_attempt']);expected['ledger']=str(ledger_path.resolve())
        if entry['job']!=expected:raise ValueError('Recovery checkpoint material/profile/limits changed')
        if entry.get('result',{}).get('score') is not None and not entry.get('artifact_sha256'):raise ValueError('Recovered score has no immutable native artifact')
    if ledger.remaining_seconds()<=0:
        state.update(status='BLOCKED_ORIGINAL_DEADLINE_EXPIRED',stop_reason='Original campaign deadline expired; not extended by recovery')
        atomic(state_path,state);render(directory,plan,manifest,source_state,state,secrets(env));return 2
    try:
        ledger.acknowledge_recovery();state['status']='RUNNING';atomic(state_path,state)
        for target in plan['selected']:
            source_id=target['source_job_id']
            if source_id in state['jobs']:
                entry=state['jobs'][source_id]
                if entry.get('inflight'):
                    output=directory/entry['output']
                    if output.exists():
                        result=read_json(output)
                        validate_result(result,entry['job'],manifest)
                        entry.update(result=result,finished_at=now(),inflight=False,artifact_sha256=digest(output))
                    else:entry.update(result={'score':None,'job_id':entry['job']['job_id'],'error':'Recovery outcome uncertain; original two-attempt limit prevents automatic resend.'},inflight=False,finished_at=now())
                    atomic(state_path,state)
                elif entry.get('artifact_sha256'):
                    if digest(directory/entry['output'])!=entry['artifact_sha256']:raise ValueError('Recovery result artifact changed')
                    result=read_json(directory/entry['output']);validate_result(result,entry['job'],manifest)
                    if result!=entry['result']:raise ValueError('Recovery checkpoint score differs from native artifact')
                continue
            verify_source_files(Path(plan['source_directory']),plan)
            if time.time()<target['retry_not_before']:raise BudgetStop('Original Retry-After has not elapsed')
            if ledger.remaining_seconds()<=0:raise BudgetStop('Original deadline expired')
            job=copy.deepcopy(jobs[source_id]['job']);job['job_id']=target['binding']+':'+str(target['next_evaluation_attempt']);job['ledger']=str(ledger_path.resolve())
            output=Path('metrics')/(job['job_id'].replace(':','_')+'.json')
            entry={'source_job_id':source_id,'job':job,'output':str(output),'inflight':True};state['jobs'][source_id]=entry;atomic(state_path,state)
            try:
                seconds=min(manifest['limits']['metric_worker_seconds'],ledger.remaining_seconds())
                result=worker(job,directory/output,seconds,env) if run_worker is worker else run_worker(job,directory/output,seconds)
                validate_result(result,job,manifest)
                if (directory/output).exists():os.chmod(directory/output,0o444);entry['artifact_sha256']=digest(directory/output)
            except Exception as exc:result={'score':None,'error':'Recovery evaluator incomplete: '+type(exc).__name__,'job_id':job['job_id']}
            entry.update(result=result,inflight=False,finished_at=now());atomic(state_path,state)
            if ledger.provider_stop():raise BudgetStop('Evaluator provider stopped recovery; failure retained')
        state['status']='COMPLETE' if all(e.get('result',{}).get('score') is not None for e in state['jobs'].values()) else 'COMPLETE_WITH_UNASSESSED_METRICS'
    except KeyboardInterrupt:state['status']='INTERRUPTED'
    except BudgetStop as exc:state.update(status='PARTIAL',stop_reason=str(exc))
    except Exception as exc:state.update(status='PARTIAL',stop_reason='Recovery stopped by integrity/worker error: '+type(exc).__name__)
    finally:
        atomic(state_path,state);verify_source_files(Path(plan['source_directory']),plan)
        render(directory,plan,manifest,source_state,state,secrets(env))
    return 0 if state['status']=='COMPLETE' else 2


def validate_result(result,job,manifest):
    if result.get('job_id')!=job['job_id'] or result.get('material_sha256')!=job['material_sha256'] or result.get('profile_sha256')!=fingerprint(manifest['evaluator']):raise ValueError('Recovered metric material/profile binding invalid')
    score=result.get('score')
    if score is not None:
        package='ragas' if job['metric'].startswith('ragas_') else 'deepeval'
        if type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1 or result.get('generation_parameters')!=GENERATION or result.get('evaluator_version')!=manifest['packages'][package][package]:raise ValueError('Recovered native score/provenance invalid')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--campaign-id',required=True);parser.add_argument('--execute',action='store_true');args=parser.parse_args(argv)
    if not args.campaign_id.replace('T','').replace('Z','').isalnum():parser.error('Invalid campaign ID')
    matches=[p.parent for p in (ROOT/'reports/full_pack').glob('*/manifest.json') if read_json(p).get('campaign_id')==args.campaign_id]
    if len(matches)!=1:raise ValueError('Campaign ID not found uniquely')
    source=matches[0].resolve();directory=ROOT/'reports/full_pack_recovery'/args.campaign_id/'quota_only'
    with (source/'campaign.lock').open('r') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Original campaign or recovery is running')
        manifest,state,jobs,selected,excluded,caps,counts,valid=audit_source(source)
        directory.mkdir(parents=True,exist_ok=True);plan_path=directory/'recovery_manifest.json'
        if plan_path.exists():
            plan=read_json(plan_path);verify_source_files(source,plan)
            if plan['selected']!=selected or plan['recovery_code_sha256']!={name:digest(ROOT/name) for name in OWN_FILES}:raise ValueError('Recovery selection or source changed')
        else:
            plan={'schema_version':1,'source_campaign_id':args.campaign_id,'source_directory':str(source),
                'created_at':now(),'workflow':'Separate evaluator-only authorization; original aggregate credential binding is preserved, not bypassed.',
                'source_files_sha256':source_inventory(source),'original_manifest_sha256':digest(source/'manifest.json'),
                'original_ledger_counts':counts,'original_caps':caps,'expires_at':state['expires_at'],
                'recovery_code_sha256':{name:digest(ROOT/name) for name in OWN_FILES},'selected':selected,'excluded':excluded,
                'original_valid_scores_sha256':fingerprint(valid),'original_valid_score_count':len(valid),
                'max_additional_judge_requests':sum(t['request_limit'] for t in selected),'max_additional_morpheus_requests':0,
                'evaluator':manifest['evaluator'],'credential_transition':'Updated key bound separately at execution; original private binding cannot validate a new key.',
                'unassessed_metric_policy':'No retries for missing chunks/evidence, unrun jobs, chatbot failures, nonquota errors or valid scores.'}
            immutable(plan_path,plan)
        if not args.execute:
            atomic(directory/'selection_summary.json',{'selected':selected,'excluded':excluded,'maximum_added_judge_requests':plan['max_additional_judge_requests'],
                'original_requests_unchanged':counts,'original_valid_scores_preserved':len(valid),'deadline_utc':datetime.fromtimestamp(state['expires_at'],timezone.utc).isoformat()})
            if not (directory/'recovery_checkpoint.json').exists():render(directory,plan,manifest,state,{'status':'PLANNED_NOT_EXECUTED','jobs':{}})
            print('Prepared '+str(len(selected))+' quota-only evaluator job(s); at most '+str(plan['max_additional_judge_requests'])+' added judge requests; 0 Morpheus requests.')
            print('Recovery directory: '+str(directory));return 0
        try:code=execute(directory,plan,manifest,state,jobs)
        finally:print('Recovery HTML: '+str(directory/'report.html'))
        return code
