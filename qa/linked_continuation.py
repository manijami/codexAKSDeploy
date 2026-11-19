"""Exact pending-only linked continuation, preserving original/recovery artifacts."""
import argparse
from contextlib import closing
import copy
import csv
import fcntl
import html
import json
import os
import signal
import sqlite3
import time
from collections import Counter
from pathlib import Path

from .config import ROOT,load,target,secrets
from .campaign import Campaign,now
from .campaign_io import Ledger,BudgetStop,atomic,immutable,fingerprint
from .campaign_plan import runtime_binding,code_fingerprints
from .campaign_privacy import sanitize
from .campaign_report import exports
from .faq_pilot import digest
from .evaluator_recovery import (audit_source,source_inventory,verify_source_files,read_json,
    new_binding,validate_result,worker as native_worker,ro_db)

OWN_FILES=('qa/linked_continuation.py','scripts/continue_campaign.py','continue_pending.command')


def own_fingerprints():return {name:digest(ROOT/name) for name in OWN_FILES}


def valid_records(state):
    return [{'test_id':cid,'metric':metric,'job_id':g['job_id'],'score':g['score']}
            for cid,rec in state['cases'].items() for metric,histories in rec.get('metrics',{}).items()
            for g in histories if g.get('score') is not None]


def error_records(state):
    return [{'test_id':cid,'metric':metric,'job_id':g.get('job_id'),'exception_type':g.get('exception_type'),'error':g['error']}
            for cid,rec in state['cases'].items() for metric,histories in rec.get('metrics',{}).items()
            for g in histories if g.get('score') is None and g.get('error')]


def audit_completed_recovery(source,recovery,check_runtime=True):
    original,state,jobs,_,_,caps,counts,old_valid=audit_source(source,check_runtime=check_runtime)
    plan=read_json(recovery/'recovery_manifest.json');checkpoint=read_json(recovery/'recovery_checkpoint.json')
    if plan['source_campaign_id']!=original['campaign_id'] or plan['original_manifest_sha256']!=digest(source/'manifest.json') or checkpoint['plan_sha256']!=digest(recovery/'recovery_manifest.json') or checkpoint['status']!='COMPLETE':raise ValueError('Completed recovery/source manifest mismatch')
    if check_runtime and plan['recovery_code_sha256']!={name:digest(ROOT/name) for name in plan['recovery_code_sha256']}:raise ValueError('Recovery implementation drift')
    verify_source_files(source,plan)
    baseline=copy.deepcopy(state);recovered=[];allowed=set()
    if len(checkpoint['jobs'])!=len(plan['selected']):raise ValueError('Recovery checkpoint is incomplete')
    for selected in plan['selected']:
        sid=selected['source_job_id'];entry=checkpoint['jobs'][sid]
        if sid not in jobs or entry.get('inflight') or not entry.get('artifact_sha256') or digest(recovery/entry['output'])!=entry['artifact_sha256']:raise ValueError('Recovered result artifact mismatch')
        result=read_json(recovery/entry['output']);validate_result(result,entry['job'],original)
        if result!=entry['result'] or result.get('score') is None:raise ValueError('Recovery is not a valid completed evaluation')
        expected=copy.deepcopy(jobs[sid]['job']);expected['job_id']=selected['binding']+':'+str(selected['next_evaluation_attempt']);expected['ledger']=str((recovery/'requests.sqlite').resolve())
        if expected!=entry['job']:raise ValueError('Recovered material/profile/request limits changed')
        path=recovery/'jobs'/(expected['job_id'].replace(':','_')+'.json')
        if read_json(path)!=expected:raise ValueError('Recovered immutable job changed')
        allowed.add(expected['job_id'])
        addition={**result,'binding':selected['binding'],'attempt_id':selected['attempt_id'],'response_sha256':selected['response_sha256'],
            'evaluation_attempt':selected['next_evaluation_attempt'],'provisional':True,'evaluated_at':entry['finished_at'],
            'source_job_id':sid,'recovery_only':True}
        baseline['cases'][selected['test_id']]['metrics'][selected['metric']].append(addition)
        recovered.append({'test_id':selected['test_id'],'metric':selected['metric'],'source_job_id':sid,'job_id':result['job_id'],'score':result['score']})
    derived=read_json(recovery/'derived_checkpoint.json')
    if derived['cases']!=baseline['cases']:raise ValueError('Recovery derived checkpoint differs from verified original+recovered results')
    with ro_db(source/'requests.sqlite') as a,ro_db(recovery/'requests.sqlite') as b:
        old_calls=list(a.execute('SELECT * FROM calls ORDER BY id'));last=old_calls[-1][0] if old_calls else 0
        if list(b.execute('SELECT * FROM calls WHERE id<=? ORDER BY id',(last,)))!=old_calls:raise ValueError('Recovery ledger lost original history')
        additions=list(b.execute('SELECT kind,job FROM calls WHERE id>?',(last,)))
        if any(kind!='judge' or job not in allowed for kind,job in additions) or len(additions)>plan['max_additional_judge_requests']:raise ValueError('Recovery request scope mismatch')
        recovery_caps={kind:{'maximum':maximum,'expires':expires} for kind,maximum,expires in b.execute('SELECT kind,maximum,expires FROM caps')}
        combined_counts=dict(b.execute('SELECT kind,count(*) FROM calls GROUP BY kind'))
    if recovery_caps!=caps or derived['ledger_counts']!=combined_counts or combined_counts.get('api',0)!=counts.get('api',0):raise ValueError('Combined ledger caps/counts/deadline mismatch')
    summary=read_json(recovery/'recovery_summary.json')
    recovered_source_ids={r['source_job_id'] for r in recovered}
    remaining=[e for e in error_records(state) if e['job_id'] not in recovered_source_ids]
    if summary['status']!='COMPLETE' or len(summary['remaining_original_failures'])!=len(remaining) or len(summary['recovered'])!=len(recovered):raise ValueError('Recovery report summary mismatch')
    return original,state,baseline,recovered,remaining,caps,combined_counts,old_valid


def pending_cases(original,state,source,strict=True):
    lookup={c['id']:c for c in original['cases']}
    pending=[copy.deepcopy(c) for c in original['cases'] if c['executable'] and state['cases'][c['id']]['status']=='NOT_RUN']
    for c in pending:
        rec=state['cases'][c['id']]
        if any(rec.get(k) for k in ('attempted','responses','payloads','metrics','inflight','capture_done','uncertain_submission')):raise ValueError('NOT_RUN case has prior submitted/completed work')
        if c['campaign_blockers'] or c['suite'] not in ('document_faq','session_canary'):raise ValueError('Pending scope contains a blocker or unrelated suite')
        if c['suite']=='document_faq':
            f=c['faq_reference']
            if f['review_issue_ids'] or f['review_flags'] or f['reference_review_status']!='REQUIRES_BUSINESS_REVIEW' or f.get('retrieved_contexts') is not None:raise ValueError('Pending FAQ lost source-review exclusion or draft/context boundary')
    with ro_db(source/'requests.sqlite') as db:api_jobs=[r[0] for r in db.execute("SELECT job FROM calls WHERE kind='api'")]
    if any(job.startswith(original['campaign_id']+':'+c['id']+':') for c in pending for job in api_jobs):raise ValueError('Pending case already has an API ledger reservation')
    if strict and (len(pending)!=107 or Counter(c['suite'] for c in pending)!=Counter(document_faq=105,session_canary=2)):raise ValueError('Expected exactly 105 FAQ plus 2 session canary NOT_RUN cases')
    return pending


def verify_parents(manifest):
    for source in manifest['parents'].values():
        if source_inventory(Path(source['directory']))!=source['files_sha256']:raise ValueError('Original or completed recovery artifacts changed')


def binding_to_recovery(config,env,recovery,original):
    current=new_binding(config,env,(recovery/'credential.private.key').read_bytes(),original['evaluator'])
    if current!=read_json(recovery/'credential_binding.json'):raise ValueError('Current private binding differs from completed rotated-key recovery')
    if target(config,env,include_key=False)!=original['endpoint']:raise ValueError('Continuation QA endpoint changed')


def make_manifest(original,state,baseline,recovered,remaining,caps,counts,pending,source,recovery,key,config,env):
    manifest=copy.deepcopy(original)
    manifest.update(campaign_id=original['campaign_id']+'-PENDING107-001',created_at=now(),mode='linked-pending-continuation',
        allow_draft_references=True,original_created_at=original['created_at'],original_campaign_id=original['campaign_id'],
        original_expires_at=state['expires_at'],binding=runtime_binding(config,env,key),
        parents={'original':{'directory':str(source.resolve()),'files_sha256':source_inventory(source)},
                 'recovery':{'directory':str(recovery.resolve()),'files_sha256':source_inventory(recovery)}},
        original_code_sha256=original['code_sha256'],continuation_code_sha256=own_fingerprints(),
        selected_original_case_sha256={c['id']:fingerprint(c) for c in pending},selected_test_ids=[c['id'] for c in pending],
        baseline_ledger_counts=counts,original_caps=caps,preserved_valid_scores_sha256=fingerprint(valid_records(baseline)),
        preserved_valid_score_count=len(valid_records(baseline)),preserved_remaining_evaluator_errors=remaining,
        recovered_results=recovered,credential_provenance='Original credential verifier remains unchanged. Current credentials match the completed recovery and are separately bound for this explicitly authorized continuation.')
    manifest['code_sha256']={**original['code_sha256'],**own_fingerprints()}
    manifest['cases']=copy.deepcopy(pending)
    for index,c in enumerate(manifest['cases']):c['batch']=manifest['campaign_id']+':'+str(index//10)
    posts=sum(len(c.get('turns',[{}])) for c in pending);faq=sum(c['suite']=='document_faq' for c in pending)
    turns=posts;judge=faq*2+turns*4
    manifest['estimates']={'planned_cases':len(pending),'executable_cases':len(pending),'blocked_cases':0,
        'document_cases':faq,'eligible_document_cases':faq,'maximum_selected_api_requests':posts,
        'planned_base_metric_invocations':faq+turns*2,'estimated_judge_http_requests_without_retrieval':judge,
        'maximum_new_judge_requests':judge,'original_planned_cases':len(original['cases']),
        'note':'Only saved eligible NOT_RUN IDs. Existing results are preserved as a linked baseline; serial new captures are fresh observations.'}
    manifest['scope']='107 exact pending cases only. No completed/source-blocked cases or prior evaluator failures are executed.'
    manifest['execution_prerequisites']=[];manifest['trace_import_allowed']=False
    return manifest


def validate_new_ledger(directory,manifest):
    recovery=Path(manifest['parents']['recovery']['directory'])
    with ro_db(recovery/'requests.sqlite') as parent,ro_db(directory/'requests.sqlite') as current:
        prefix=list(parent.execute('SELECT * FROM calls ORDER BY id'));last=prefix[-1][0] if prefix else 0
        if list(current.execute('SELECT * FROM calls WHERE id<=? ORDER BY id',(last,)))!=prefix:raise ValueError('Continuation ledger lost original/recovery history')
        caps={kind:{'maximum':maximum,'expires':expires} for kind,maximum,expires in current.execute('SELECT kind,maximum,expires FROM caps')}
        if caps!=manifest['original_caps']:raise ValueError('Continuation cap/deadline drift')
        extra=list(current.execute('SELECT kind,job FROM calls WHERE id>?',(last,)))
    case_ids=set(manifest['selected_test_ids']);api=0;judge=0
    for kind,job in extra:
        if kind=='api':
            api+=1;parts=job.rsplit(':',2)
            if len(parts)!=3 or parts[0]!=manifest['campaign_id'] or parts[1] not in case_ids:raise ValueError('Continuation ledger includes an out-of-scope QA request')
        elif kind=='judge':judge+=1
        else:raise ValueError('Unknown continuation request class')
    if api>manifest['estimates']['maximum_selected_api_requests'] or judge>manifest['estimates']['maximum_new_judge_requests']:raise ValueError('Continuation selected-work request bound exceeded')
    return api,judge


def validate_checkpoint(directory,manifest,state):
    if state['manifest_sha256']!=digest(directory/'manifest.json') or state['expires_at']!=manifest['original_expires_at'] or state.get('retry_evaluators') or state.get('trace_import') or state.get('evidence_import'):raise ValueError('Continuation checkpoint policy/manifest changed')
    if set(state['cases'])!=set(manifest['selected_test_ids']):raise ValueError('Continuation checkpoint contains other case IDs')
    known_jobs=set()
    case_lookup={c['id']:c for c in manifest['cases']}
    for cid,rec in state['cases'].items():
        if len(rec.get('responses',[]))!=len(rec.get('response_artifact_sha256',{})):raise ValueError('Continuation response/artifact count mismatch')
        for relative,sha in rec.get('response_artifact_sha256',{}).items():
            if digest(directory/relative)!=sha:raise ValueError('Continuation response artifact changed')
            saved=read_json(directory/relative);matches=[(i,r) for i,r in enumerate(rec['responses']) if r['attempt_id']==saved['response']['attempt_id']]
            if len(matches)!=1 or matches[0][1]!=saved['response'] or rec['payloads'][matches[0][0]]!=saved['payload']:raise ValueError('Continuation captured answer/payload changed')
        allowed_metrics={'deepeval_answer_relevancy','custom_coverage_review'}
        if case_lookup[cid]['suite']=='document_faq':allowed_metrics.add('ragas_answer_accuracy')
        for metric,histories in rec.get('metrics',{}).items():
            if metric not in allowed_metrics:raise ValueError('Unexpected metric in pending-only continuation')
            for grade in histories:
                job_id=grade.get('job_id') or grade['binding']+':'+str(grade['evaluation_attempt'])
                if grade['evaluation_attempt']!=1:raise ValueError('Continuation evaluator retry is not authorized')
                known_jobs.add(job_id)
                job_path=directory/'jobs'/(job_id.replace(':','_')+'.json')
                if job_path.exists():
                    job=read_json(job_path)
                    if job['metric']!=metric or job['job_id']!=job_id or fingerprint(job['material'])!=grade['material_sha256'] or job['profile']!=manifest['evaluator'] or job['material']['attempt_id']!=grade['attempt_id'] or job['material']['response_sha256']!=grade['response_sha256']:raise ValueError('Continuation job material/profile/attempt changed')
                    binding=fingerprint({'metric':metric,'material':grade['material_sha256'],'evaluator':manifest['evaluator'],'code':manifest['code_sha256']})
                    if binding!=grade['binding']:raise ValueError('Continuation metric binding changed')
                    response=next((r for r in rec['responses'] if r['attempt_id']==grade['attempt_id']),None)
                    if not response or response['response_sha256']!=grade['response_sha256'] or job['material']['response']!=response['answer']:raise ValueError('Metric no longer matches exact captured answer')
                    if job['request_limit']!=manifest['metric_definitions'][metric]['max_requests'] or job['threshold']!=manifest['metric_definitions'][metric]['threshold'] or job['ledger']!=str((directory/'requests.sqlite').resolve()):raise ValueError('Continuation worker request policy changed')
                    case=case_lookup[cid];turn=int(grade['attempt_id'].rsplit(':',1)[1])-1
                    if job['material']['user_input']!=sanitize(case['turns'][turn]['message']) or job['material'].get('retrieved_contexts') is not None:raise ValueError('Question/context binding changed')
                    if case.get('faq_reference') and job['material'].get('reference')!=sanitize(case['faq_reference']['reference_answer']):raise ValueError('Worker reference differs from frozen draft')
                if grade.get('artifact_sha256'):
                    artifact=directory/grade['path']
                    if digest(artifact)!=grade['artifact_sha256']:raise ValueError('Continuation metric artifact changed')
                    saved=read_json(artifact)
                    if any(saved.get(k)!=grade.get(k) for k in ('score','method','job_id','profile_sha256','material_sha256')):raise ValueError('Continuation score differs from immutable result')
                elif grade.get('score') is not None:raise ValueError('Continuation valid score has no artifact')
    recovery=Path(manifest['parents']['recovery']['directory'])
    with ro_db(recovery/'requests.sqlite') as old,ro_db(directory/'requests.sqlite') as new:
        last=old.execute('SELECT max(id) FROM calls').fetchone()[0] or 0
        if any(job not in known_jobs or not (directory/'jobs'/(job.replace(':','_')+'.json')).exists() for (job,) in new.execute("SELECT job FROM calls WHERE id>? AND kind='judge'",(last,))):raise ValueError('Judge ledger reservation has no checkpointed continuation job')


def render(directory,manifest,state,original,baseline,recovered,remaining,private=()):
    combined=copy.deepcopy(baseline)
    for cid,record in state['cases'].items():
        if cid not in manifest['selected_test_ids'] or baseline['cases'][cid]['status']!='NOT_RUN':raise ValueError('Consolidation would replace completed history')
        combined['cases'][cid]=copy.deepcopy(record)
    combined.update(run_status=state['run_status'],updated_at=state['updated_at'],ledger_counts=state.get('ledger_counts',manifest['baseline_ledger_counts']),
        stop_reason='Linked pending-only continuation: '+str(state.get('stop_reason') or state['run_status'])+'. Original/recovery artifacts remain unchanged; outstanding evaluator failures are not retried.')
    view=copy.deepcopy(original);view['campaign_id']=original['campaign_id']+' — consolidated linked continuation'
    view['linked_continuation_manifest']={k:v for k,v in manifest.items() if k not in ('cases','parents')}
    report=exports(directory,view,combined,private)
    if fingerprint(valid_records(baseline))!=manifest['preserved_valid_scores_sha256']:raise ValueError('Consolidation baseline valid results changed')
    original_valid_count=manifest['preserved_valid_score_count']-len(recovered)
    continuation_valid=valid_records(state);new_errors=error_records(state)
    api=combined['ledger_counts'].get('api',0)-manifest['baseline_ledger_counts'].get('api',0)
    judge=combined['ledger_counts'].get('judge',0)-manifest['baseline_ledger_counts'].get('judge',0)
    info={'original_campaign_id':original['campaign_id'],'continuation_status':state['run_status'],
        'selected_test_ids':manifest['selected_test_ids'],'historical_valid_results':original_valid_count,
        'recovered_valid_results':len(recovered),'continuation_valid_results':len(continuation_valid),
        'preserved_valid_metric_results':manifest['preserved_valid_score_count'],'remaining_historical_evaluator_errors':remaining,
        'continuation_evaluator_errors':new_errors,'qa_requests_added':api,'judge_requests_added':judge,
        'combined_request_counts':combined['ledger_counts'],'original_caps_and_deadline_preserved':True,
        'source_artifacts_unchanged':True,'blocked_cases_retained':sum(not c['executable'] for c in original['cases']),
        'baseline_metric_results_unchanged':True,'no_completed_cases_or_prior_evaluator_errors_reexecuted':True,
        'report_policy':'All document scores provisional; unsupported retrieval, grounding, safety and authorization remain unassessed; model flags are unverified candidates.'}
    report['linked_continuation']=info;atomic(directory/'results.json',sanitize(report,private));atomic(directory/'continuation_summary.json',sanitize(info,private));atomic(directory/'consolidated_checkpoint.json',combined)
    def rows(data):
        return ''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in row)+'</tr>' for row in data)
    errors=[(e['test_id'],e['metric'],e.get('exception_type') or e['error']) for e in remaining]
    section='<section class="box" id="continuation"><h2>Linked continuation: original / recovered / new results</h2><p>Only the 107 verified eligible NOT_RUN cases are executable. Original and completed recovery files stay unchanged. No old evaluator failures are retried.</p><table><tr><th>Stage</th><th>Valid metric results</th></tr>'+rows([('Original',original_valid_count),('Quota recovery',len(recovered)),('Continuation',len(continuation_valid))])+'</table><p>New QA requests: '+str(api)+'; new judge requests: '+str(judge)+'. Combined counts include the original campaign and recovery.</p><h3>Remaining historical evaluator errors — not retried</h3><table><tr><th>Test</th><th>Metric</th><th>Failure</th></tr>'+rows(errors)+'</table><p>Continuation evaluator errors: '+str(len(new_errors))+'. Historical error counts elsewhere preserve failure history, including the subsequently recovered quota error.</p></section>'
    path=directory/'report.html';text=path.read_text().replace('</header>','</header>'+section,1).replace('<th>Judge errors</th>','<th>Historical judge errors</th>')
    recovered_cases={r['test_id'] for r in recovered}
    for case in original['cases']:
        tag='CONTINUATION' if case['id'] in manifest['selected_test_ids'] else 'ORIGINAL + RECOVERED' if case['id'] in recovered_cases else 'ORIGINAL / BLOCKED'
        needle='<summary>'+html.escape(case['id']+' · ')
        text=text.replace(needle,'<summary>['+tag+'] '+html.escape(case['id']+' · '),1)
    path.write_text(text)
    (directory/'summary.md').write_text('# Linked pending-only continuation\n\nStatus: '+state['run_status']+'\n\nOriginal valid metrics: '+str(original_valid_count)+'; recovered: '+str(len(recovered))+'; continuation: '+str(len(continuation_valid))+'.\n\nHistorical outstanding evaluator errors retained: '+str(len(remaining))+'. No old work is retried. New QA/judge requests: '+str(api)+'/'+str(judge)+'.\n\nDraft references provisional; unsupported metrics unassessed. See report.html.\n')
    return report


class PendingCampaign(Campaign):
    def __init__(self,directory,manifest,state,config,env,key,original,baseline,recovered,remaining,transport=None,worker=None):
        self.original=original;self.baseline=baseline;self.recovered=recovered;self.remaining=remaining
        super().__init__(directory,manifest,state,config,env,key,transport,worker)
    def checkpoint(self,report=False):
        super().checkpoint(False)
        if report:render(self.directory,self.manifest,self.state,self.original,self.baseline,self.recovered,self.remaining,self.private)
    def capture_case(self,case):
        if case['id'] not in self.manifest['selected_test_ids']:raise ValueError('Attempted case outside pending manifest')
        record=self.start_case(case)
        if not record.get('capture_done') and not record.get('inflight'):
            turn=len(record.get('responses',[]))+1;attempt=self.manifest['campaign_id']+':'+case['id']+':'+str(turn)
            with ro_db(self.directory/'requests.sqlite') as db:reserved=db.execute("SELECT count(*) FROM calls WHERE kind='api' AND job=?",(attempt,)).fetchone()[0]
            if reserved:
                path=Path('responses')/case['id']/f'turn_{turn:02d}.json'
                if (self.directory/path).exists():record['inflight']={'attempt_id':attempt,'path':str(path)}
                else:
                    record.update(status='INCONCLUSIVE',capture_done=True,uncertain_submission=True)
                    record['reasons'].append('Existing QA request reservation lacks a final capture; it will not be resent.')
                    self.checkpoint();return
        return super().capture_case(case)
    def worker_process(self,job,out):
        return native_worker(job,out,min(self.limits['metric_worker_seconds'],self.ledger.remaining_seconds()),self.env)


def execute(directory,manifest,original,baseline,recovered,remaining,transport=None,worker=None):
    verify_parents(manifest);config,env=load();recovery=Path(manifest['parents']['recovery']['directory'])
    binding_to_recovery(config,env,recovery,original)
    key=(directory/'identity.private.key').read_bytes()
    if runtime_binding(config,env,key)!=manifest['binding']:raise ValueError('Continuation credential/configuration fingerprint changed')
    if manifest['original_code_sha256']!=code_fingerprints() or manifest['continuation_code_sha256']!=own_fingerprints():raise ValueError('Continuation/campaign implementation changed')
    if digest(directory/'dataset.snapshot.json')!=manifest['source_hashes']['business_faq']:raise ValueError('Continuation frozen reference snapshot changed')
    state=read_json(directory/'checkpoint.json')
    if not (directory/'requests.sqlite').exists():
        with ro_db(recovery/'requests.sqlite') as source,closing(sqlite3.connect(directory/'requests.sqlite')) as destination:source.backup(destination)
    validate_new_ledger(directory,manifest);validate_checkpoint(directory,manifest,state)
    campaign=PendingCampaign(directory,manifest,state,config,env,key,original,baseline,recovered,remaining,transport,worker)
    if campaign.ledger.remaining_seconds()<=0:
        state.update(run_status='BLOCKED_ORIGINAL_DEADLINE_EXPIRED',stop_reason='Original deadline expired; continuation does not extend it.');campaign.checkpoint(True);return 2
    try:campaign.ledger.acknowledge_recovery()
    except BudgetStop as exc:
        state.update(run_status='PARTIAL',stop_reason=str(exc));campaign.checkpoint(True);return 2
    old_handlers={}
    def cancel(*_):campaign.cancelled=True;raise KeyboardInterrupt
    for sig in (signal.SIGINT,signal.SIGTERM):old_handlers[sig]=signal.signal(sig,cancel)
    try:code=campaign.run()
    finally:
        for sig,handler in old_handlers.items():signal.signal(sig,handler)
        verify_parents(manifest);validate_new_ledger(directory,manifest)
    return code


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--campaign-id',required=True);parser.add_argument('--execute',action='store_true');args=parser.parse_args(argv)
    matches=[p.parent for p in (ROOT/'reports/full_pack').glob('*/manifest.json') if read_json(p).get('campaign_id')==args.campaign_id]
    if len(matches)!=1:raise ValueError('Original campaign ID is not unique')
    source=matches[0].resolve();recovery=ROOT/'reports/full_pack_recovery'/args.campaign_id/'quota_only'
    directory=ROOT/'reports/full_pack_continuation'/args.campaign_id/'pending_107'
    with (source/'campaign.lock').open('r') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Original, recovery or linked continuation is already running')
        original,state,baseline,recovered,remaining,caps,counts,old_valid=audit_completed_recovery(source,recovery)
        pending=pending_cases(original,state,source)
        if len(old_valid)!=460 or len(valid_records(baseline))!=461 or len(remaining)!=32:raise ValueError('Expected preserved original/recovery score/error baseline changed')
        config,env=load();binding_to_recovery(config,env,recovery,original)
        if (directory/'manifest.json').exists():
            manifest=read_json(directory/'manifest.json');verify_parents(manifest)
            if manifest['selected_test_ids']!=[c['id'] for c in pending] or manifest['continuation_code_sha256']!=own_fingerprints():raise ValueError('Pending selection or implementation drift')
        else:
            directory.mkdir(parents=True,exist_ok=False);key=os.urandom(32);(directory/'identity.private.key').write_bytes(key);os.chmod(directory/'identity.private.key',0o600)
            manifest=make_manifest(original,state,baseline,recovered,remaining,caps,counts,pending,source,recovery,key,config,env)
            immutable(directory/'manifest.json',manifest)
            (directory/'dataset.snapshot.json').write_bytes((source/'dataset.snapshot.json').read_bytes());os.chmod(directory/'dataset.snapshot.json',0o444)
            new_state={'manifest_sha256':digest(directory/'manifest.json'),'created_at':now(),'updated_at':now(),'expires_at':state['expires_at'],
                'run_status':'PLANNED_NOT_EXECUTED','cases':{},'ledger_counts':counts,'retry_evaluators':False}
            for case in manifest['cases']:
                new_state['cases'][case['id']]={'status':'NOT_RUN','responses':[],'payloads':[],'metrics':{},'reasons':[],
                    'missing_evidence':['Exact-attempt retrieved chunks unavailable; context precision/recall/faithfulness remain null.','Groundedness/tool correctness/authorization/safety require supported backend and human evidence.']}
            atomic(directory/'checkpoint.json',new_state)
            with (directory/'pending_ids.csv').open('w',newline='') as handle:
                writer=csv.writer(handle);writer.writerow(['test_id','suite','original_status','draft_status','source_case_sha256'])
                writer.writerows((c['id'],c['suite'],'NOT_RUN',c.get('faq_reference',{}).get('reference_review_status','NOT_APPLICABLE'),fingerprint(c)) for c in pending)
        if args.execute:
            code=execute(directory,manifest,original,baseline,recovered,remaining)
        else:
            current=read_json(directory/'checkpoint.json');render(directory,manifest,current,original,baseline,recovered,remaining,secrets(env));code=0
            print('Verified 107 pending IDs: 105 FAQs + 2 canaries; maximum 108 new QA and 642 judge requests. Baseline preserved: 461 valid metrics.')
        print('Consolidated HTML: '+str(directory/'report.html'));return code
