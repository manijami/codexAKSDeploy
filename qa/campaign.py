"""Serial unattended campaign with immutable manifests and crash-safe no-replay POSTs."""
import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

from .config import ROOT,load,target,secrets
from .runner import prepare,evaluate,load_evidence
from .transport import request,extract_answer
from .faq_pilot import digest,attach_retrieval
from .campaign_io import atomic,immutable,fingerprint,Ledger,BudgetStop
from .campaign_plan import build_manifest,runtime_binding,code_fingerprints,package_versions,PINNED
from .campaign_privacy import sanitize
from .campaign_report import exports


def now():return datetime.now(timezone.utc).isoformat()


def application(response):
    final=None;first_useful=None
    for event in response.get('events',[]):
        try:item=json.loads(event.get('data',''))
        except (ValueError,TypeError):continue
        if isinstance(item,dict) and item.get('type')=='response' and isinstance(item.get('response'),dict):final=item['response']
    return final.get('status') if final else None


class PersistentSessions:
    def __init__(self,sessions,case_id,save):self.sessions=sessions;self.case_id=case_id;self.index=0;self.save=save
    def take(self,account):
        values=self.sessions.setdefault(self.case_id,[])
        if self.index==len(values):values.append(str(uuid.uuid4()));self.save()
        value=values[self.index];self.index+=1;return value


class Campaign:
    def __init__(self,directory,manifest,state,config,env,key,transport=None,worker=None):
        self.directory=Path(directory);self.manifest=manifest;self.state=state;self.config=config;self.env=env;self.key=key
        self.transport=transport or request;self.worker=worker or self.worker_process
        self.limits=manifest['limits'];self.private=secrets(env)
        self.ledger=Ledger(self.directory/'requests.sqlite')
        self.sessions_path=self.directory/'sessions.private.json'
        self.sessions=json.loads(self.sessions_path.read_text()) if self.sessions_path.exists() else {}
        self.private += [v for values in self.sessions.values() for v in values]
        self.cancelled=False;self.last_request=0;self.auth_failures=0;self.connection_failures=0
    def checkpoint(self,report=False):
        self.state['updated_at']=now();self.state['ledger_counts']=self.ledger.counts()
        atomic(self.directory/'checkpoint.json',self.state)
        if report:exports(self.directory,self.manifest,self.state,self.private)
    def stopped(self):
        if self.cancelled:raise KeyboardInterrupt
        if time.time()>=self.state['expires_at']:raise BudgetStop('Immutable campaign elapsed-time limit reached')
        provider=self.ledger.provider_stop()
        if provider:raise BudgetStop('Evaluator/provider quota or billing limit; checkpoint retained. Retry-After='+str(provider[1]))
    def start_case(self,case):
        return self.state['cases'].setdefault(case['id'],{'status':'NOT_RUN' if case['executable'] else 'BLOCKED','responses':[],'payloads':[], 'metrics':{},'reasons':list(case['campaign_blockers']),
            'missing_evidence':['Exact-attempt retrieved chunks unavailable; context precision/recall/faithfulness remain null.',
                'Groundedness/tool correctness/authorization/safety require supported backend and human evidence.']})
    def capture_case(self,case):
        rec=self.start_case(case)
        if not case['executable']:
            rec.update(status='BLOCKED',reasons=case['campaign_blockers']);self.checkpoint();return
        if rec.get('capture_done'):return
        if rec.get('inflight'):
            artifact=self.directory/rec['inflight']['path']
            if artifact.exists():
                saved=json.loads(artifact.read_text());rec['responses'].append(saved['response']);rec['payloads'].append(saved['payload']);rec.setdefault('response_artifact_sha256',{})[rec['inflight']['path']]=digest(artifact);rec.pop('inflight');self.checkpoint()
            else:
                rec.update(status='INCONCLUSIVE',capture_done=True,uncertain_submission=True)
                rec['reasons'].append('In-flight POST may have reached the server. Not automatically resent on resume; remaining case turns not sent.')
                rec.pop('inflight');self.checkpoint();return
        runtime={**self.config,'conversation_mode':'client_uuid','snapshot_paths':['response.message'],'delta_paths':[],'response_mapping_confirmed':True}
        def save_sessions():
            atomic(self.sessions_path,self.sessions,private=True)
            self.private[:]=secrets(self.env)+[v for values in self.sessions.values() for v in values]
        prepared=prepare(case,self.env,runtime,PersistentSessions(self.sessions,case['id'],save_sessions),
                         json.loads((ROOT/'config/payload.template.json').read_text()))
        for step in prepared[len(rec['responses']):]:
            self.stopped()
            if rec['responses'] and (rec['responses'][-1].get('error') or rec['responses'][-1].get('status_code') not in prepared[len(rec['responses'])-1]['expected_status']):break
            index=step['turn'];attempt=self.manifest['campaign_id']+':'+case['id']+':'+str(index)
            path=Path('responses')/case['id']/f'turn_{index:02d}.json'
            call=self.ledger.claim('api',attempt,batch=str(case['batch']),batch_limit=self.limits['batch_api_requests'])
            rec['inflight']={'attempt_id':attempt,'path':str(path)};rec['attempted']=True;self.checkpoint()
            delay=self.limits['min_interval_seconds']-(time.monotonic()-self.last_request)
            if delay>0:time.sleep(delay)
            self.last_request=time.monotonic()
            self.stopped()
            runtime['timeout_seconds']=max(0.1,min(self.config['timeout_seconds'],self.ledger.remaining_seconds()))
            runtime['read_timeout_seconds']=max(0.1,min(self.config['read_timeout_seconds'],runtime['timeout_seconds']))
            # The budget slot and in-flight marker are durable before any POST.
            response=self.transport(target(runtime,self.env,account=step['account']),step['method'],step['headers'],
                None if step['body'] is None else json.dumps(step['body']).encode(),runtime)
            self.ledger.finish(call,response.get('status_code'))
            response.update(attempt_id=attempt,expected_status=step['expected_status'],required_metrics=step['required_metrics'],captured_at=now())
            response['answer']=extract_answer(response.get('events',[]),runtime)
            response['application_status']=application(response)
            response['first_useful_answer_ms']=None  # Existing parser has no per-event timestamp; do not invent one.
            response['frontend_conversation_verification']={'fresh_uuid_workflow':True,
                'response_id_present':bool(response.get('headers',{}).get('x-conversation-id')),
                'response_id_matches_request':response.get('headers',{}).get('x-conversation-id')==step['body'].get('conversationId') if isinstance(step['body'],dict) and response.get('headers',{}).get('x-conversation-id') else None,
                'fresh_state_isolation':None}
            if not response.get('response_sha256'):response['response_sha256']=hashlib.sha256(response.get('raw_body','').encode()).hexdigest()
            safe=sanitize({'response':response,'payload':step['body']},self.private)
            immutable(self.directory/path,safe)
            rec['responses'].append(safe['response']);rec['payloads'].append(safe['payload']);rec.setdefault('response_artifact_sha256',{})[str(path)]=digest(self.directory/path);rec.pop('inflight');self.checkpoint()
            print(case['id']+' turn '+str(index)+': captured HTTP '+str(response.get('status_code'))+', application '+str(response.get('application_status')),flush=True)
            actual=response.get('status_code');self.connection_failures=self.connection_failures+1 if actual is None else 0
            self.auth_failures=self.auth_failures+1 if actual in (401,403) and actual not in step['expected_status'] else 0
            if self.connection_failures>=self.limits['connectivity_failure_stop']:raise BudgetStop('Repeated systemic connectivity failures')
            if self.auth_failures>=self.limits['auth_failure_stop']:raise BudgetStop('Repeated unexpected authentication failures')
            self.stopped()
        scores,status,reasons,observations=evaluate(case,rec['responses'],runtime,{})
        for r in rec['responses']:
            if r.get('status_code')==200 and r.get('application_status') in ('error','failed','failure'):
                status='FAIL';reasons.append('HTTP 200 contains final application status '+r['application_status']+'.')
            elif r.get('status_code')==200 and r.get('application_status')!='success':
                if status=='PASS':status='INCONCLUSIVE'
                reasons.append('Final application response status missing or unrecognized; transport success alone is insufficient.')
        if len(rec['responses'])!=len(prepared):
            if status!='BLOCKED':status='FAIL'
            reasons.append('Later turns not sent after an earlier failed/incomplete turn.')
        if case.get('canary_expected') and len(rec['responses'])==2:
            match=case['canary_expected'] in rec['responses'][-1].get('answer','')
            rec['visible_canary_match']=match
            if not match:status='FAIL';reasons.append('Synthetic canary not recalled in retained session.')
        if case.get('canary_forbidden') and rec['responses']:
            leak=case['canary_forbidden'] in rec['responses'][-1].get('answer','')
            rec['visible_canary_leak']=leak
            if leak:status='FAIL';reasons.append('Synthetic canary from a different case appeared in fresh-session output; investigate freshness, not proof of independent-principal access.')
        rec.update(status=status,reasons=list(dict.fromkeys(reasons+observations)),evidence_scores=scores,capture_done=True)
        self.checkpoint()
    def apply_evidence(self,case):
        rec=self.start_case(case)
        if not self.state.get('evidence_import') or not rec.get('responses'):return
        source=self.state['evidence_import']
        if rec.get('applied_evidence_import')==source:return
        evidence=load_evidence(self.directory/source)
        runtime={**self.config,'response_mapping_confirmed':True}
        scores,status,reasons,observations=evaluate(case,rec['responses'],runtime,evidence)
        rec.setdefault('evidence_review_history',[]).append({'prior_status':rec['status'],'import':source,'reviewed_at':now()})
        rec['evidence_scores']=scores
        critical=any(v and v.get('method')=='critical_failure_override' for v in scores.values())
        rec['critical_observed']=critical
        if critical or status=='FAIL':rec['status']='FAIL'
        elif rec['status']!='FAIL':rec['status']=status
        rec['reasons']=list(dict.fromkeys(rec['reasons']+reasons+observations))
        rec['applied_evidence_import']=source;self.checkpoint()
    def trace_material(self,response):
        if not self.state.get('trace_import'):return None
        trace=json.loads((self.directory/self.state['trace_import']).read_text())
        matching=[r for r in trace.get('attempts',[]) if r.get('attempt_id')==response['attempt_id']]
        if not matching:return None
        row={'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],'retrieved_contexts':None}
        attach_retrieval([row],{'attempts':matching})
        return row if row.get('retrieved_contexts') else None
    def material(self,case,response,index):
        material={'test_id':case['id'],'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],
            'user_input':case.get('turns',[{}])[index].get('message',''),'response':response.get('answer',''),
            'expected':case.get('expected',''),'reference':case.get('faq_reference',{}).get('reference_answer'),
            'reference_review_status':case.get('faq_reference',{}).get('reference_review_status'),'retrieved_contexts':None}
        try:trace=self.trace_material(response)
        except (ValueError,KeyError,TypeError):
            trace=None
            record=self.start_case(case);record['trace_import_error']=True
            reason='Trace import failed exact-attempt/provenance/rank validation; retrieval metrics remain unassessed.'
            if reason not in record['missing_evidence']:record['missing_evidence'].append(reason)
        record=self.start_case(case)
        generic='Exact-attempt retrieved chunks unavailable; context precision/recall/faithfulness remain null.'
        if generic in record['missing_evidence']:record['missing_evidence'].remove(generic)
        if trace:material.update(retrieved_contexts=trace['retrieved_contexts'],retrieval_provenance=trace['retrieval_provenance'])
        else:
            reason=response['attempt_id']+': exact-attempt retrieved passages unavailable; retrieval/faithfulness metrics unassessed.'
            if reason not in record['missing_evidence']:record['missing_evidence'].append(reason)
        return sanitize(material,self.private)
    def grade_case(self,case):
        rec=self.start_case(case)
        for index,response in enumerate(rec.get('responses',[])):
            if case['suite']=='smoke' or response.get('status_code')!=200 or not response.get('complete') or response.get('error') or not response.get('answer'):continue
            material=self.material(case,response,index)
            names=['deepeval_answer_relevancy','custom_coverage_review']
            if case['suite']=='document_faq':names.insert(0,'ragas_answer_accuracy')
            if case['suite']=='safety':names.append('deepeval_toxicity')
            if material.get('retrieved_contexts'):
                names+=['ragas_faithfulness','deepeval_faithfulness']
                if material.get('reference'):names+=['ragas_context_precision','ragas_context_recall','deepeval_context_precision','deepeval_context_recall']
                rec.setdefault('trace_provenance',{})[response['attempt_id']]=material['retrieval_provenance']
            for name in names:
                self.stopped()
                per_material={k:v for k,v in material.items() if v is not None}
                if name not in ('ragas_faithfulness','deepeval_faithfulness','ragas_context_precision','ragas_context_recall','deepeval_context_precision','deepeval_context_recall'):
                    per_material.pop('retrieved_contexts',None);per_material.pop('retrieval_provenance',None)
                material_sha=fingerprint(per_material);definition=self.manifest['metric_definitions'][name]
                key=fingerprint({'metric':name,'material':material_sha,'evaluator':self.manifest['evaluator'],'code':self.manifest['code_sha256']})
                histories=rec['metrics'].setdefault(name,[])
                matches=[g for g in histories if g['binding']==key]
                if matches:
                    last=matches[-1]
                    if last.get('inflight'):
                        artifact=self.directory/last['path']
                        if artifact.exists():
                            value=json.loads(artifact.read_text())
                            score=value.get('score')
                            if value.get('material_sha256')!=material_sha or value.get('profile_sha256')!=fingerprint(self.manifest['evaluator']) or (score is not None and (type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1)):
                                last.update(score=None,error='Recovered evaluator artifact binding invalid')
                            else:
                                last.update(value);last['artifact_sha256']=digest(artifact)
                            last.pop('inflight',None);self.checkpoint()
                        else:last.update(score=None,error='Interrupted evaluator; outcome unknown, not automatically retried.');last.pop('inflight',None);self.checkpoint()
                    if last.get('score') is not None or not self.state.get('retry_evaluators') or len(matches)>=self.limits['max_evaluator_attempts']:continue
                if self.ledger.counts().get('judge',0)>=self.limits['judge_requests']:raise BudgetStop('Campaign-wide judge request ceiling reached')
                number=len(matches)+1;job_id=key+':'+str(number)
                path=Path('metrics')/(job_id.replace(':','_')+'.json')
                entry={'binding':key,'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],
                    'material_sha256':material_sha,'path':str(path),'inflight':True,'score':None,'evaluation_attempt':number}
                histories.append(entry);self.checkpoint()
                print(case['id']+' '+name+': evaluator started',flush=True)
                job={'job_id':job_id,'metric':name,'material':per_material,'material_sha256':material_sha,
                    'profile':self.manifest['evaluator'],'request_limit':definition['max_requests'],'threshold':definition['threshold'],
                    'ledger':str((self.directory/'requests.sqlite').resolve())}
                try:
                    value=self.worker(job,self.directory/path)
                    if value.get('material_sha256')!=material_sha or value.get('profile_sha256')!=fingerprint(self.manifest['evaluator']):raise ValueError('Evaluator output binding mismatch')
                    score=value.get('score')
                    if score is not None and (type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1):raise ValueError('Invalid worker score')
                    entry.update(value)
                    if (self.directory/path).exists():
                        os.chmod(self.directory/path,0o444)
                        entry['artifact_sha256']=digest(self.directory/path)
                except Exception as exc:entry.update(score=None,error='Evaluator worker incomplete: '+type(exc).__name__)
                entry.pop('inflight',None);entry['evaluated_at']=now();entry['provisional']=bool(case.get('faq_reference'))
                self.checkpoint()
                print(case['id']+' '+name+(': scored' if entry.get('score') is not None else ': unassessed (evaluator incomplete)'),flush=True)
                self.stopped()
    def worker_process(self,job,out):
        package='ragas' if job['metric'].startswith('ragas_') else 'deepeval'
        job_path=self.directory/'jobs'/(job['job_id'].replace(':','_')+'.json')
        immutable(job_path,job)
        command=[str(ROOT/f'.venv-{package}'/'bin/python'),str(ROOT/'scripts/campaign_judge.py'),'--job',str(job_path),'--out',str(out)]
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:process.wait(timeout=max(0.1,min(self.limits['metric_worker_seconds'],self.ledger.remaining_seconds())))
        except BaseException:
            process.terminate()
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            raise
        if not out.exists():raise ValueError('No evaluator output')
        return json.loads(out.read_text())
    def run(self):
        self.state['run_status']='RUNNING';self.state['stop_reason']=None;self.checkpoint(report=True)
        try:
            for index,case in enumerate(self.manifest['cases']):
                self.stopped()
                try:self.capture_case(case)
                except (BudgetStop,KeyboardInterrupt):raise
                except Exception as exc:
                    rec=self.start_case(case)
                    if rec.get('inflight'):
                        rec.update(uncertain_submission=True,capture_done=True,status='INCONCLUSIVE')
                        rec['reasons'].append('Capture interrupted with uncertain submission; no auto-resend. '+type(exc).__name__)
                        rec.pop('inflight')
                    else:rec.update(status='BLOCKED',capture_done=True,reasons=['Case preparation/transport failure: '+type(exc).__name__])
                    self.checkpoint()
                if case['executable']:
                    try:self.apply_evidence(case)
                    except (ValueError,KeyError,TypeError):
                        self.start_case(case)['reasons'].append('Invalid evidence import; annotations were not applied.')
                    self.grade_case(case)
                print(f'{index+1}/{len(self.manifest["cases"])} {case["id"]}: {self.start_case(case)["status"]}',flush=True)
                if (index+1)%10==0:self.checkpoint(report=True)
            self.state['run_status']='COMPLETE'
        except KeyboardInterrupt:self.state.update(run_status='INTERRUPTED',stop_reason='Controlled cancellation; uncertain POSTs will not be resent.')
        except BudgetStop as exc:self.state.update(run_status='PARTIAL',stop_reason=str(exc))
        except Exception as exc:self.state.update(run_status='PARTIAL',stop_reason='Fatal campaign error: '+type(exc).__name__)
        finally:self.checkpoint(report=True)
        return 0 if self.state['run_status']=='COMPLETE' else 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('plan','execute','resume','preview'))
    parser.add_argument('--out',type=Path);parser.add_argument('--campaign',type=Path)
    parser.add_argument('--max-api-requests',type=int,default=600);parser.add_argument('--max-judge-requests',type=int,default=6000)
    parser.add_argument('--max-seconds',type=int,default=86400)
    parser.add_argument('--allow-draft-references',action='store_true')
    parser.add_argument('--retry-evaluators',action='store_true')
    parser.add_argument('--trace-export',type=Path)
    parser.add_argument('--evidence',type=Path,help='Exact-attempt annotated evidence export; frozen as a separate import')
    args=parser.parse_args(argv)
    if not 1<=args.max_api_requests<=1000 or not 1<=args.max_judge_requests<=10000 or not 60<=args.max_seconds<=172800:parser.error('Caps: API 1..1000, judge 1..10000, wall seconds 60..172800')
    if args.mode in ('execute','resume') and not args.allow_draft_references:parser.error('Exploratory campaign requires --allow-draft-references')
    config,env=load()
    if args.mode=='resume':
        if not args.campaign:parser.error('resume requires --campaign')
        directory=args.campaign
        manifest=json.loads((directory/'manifest.json').read_text());state=json.loads((directory/'checkpoint.json').read_text())
        key=(directory/'identity.private.key').read_bytes()
        if state['manifest_sha256']!=digest(directory/'manifest.json'):raise ValueError('Campaign manifest changed')
        if manifest['binding']!=runtime_binding(config,env,key):raise ValueError('Endpoint/configuration/QA binding drift; do not reuse a different deployment as this campaign')
        if manifest['code_sha256']!=code_fingerprints():raise ValueError('Evaluator/campaign code changed; preserve this run and use a new campaign')
        for name,sha in manifest['source_hashes'].items():
            if name not in ('business_faq','evaluator_profile') and digest(ROOT/name)!=sha:raise ValueError('Case catalogue changed')
        if digest(ROOT/'datasets/business_faq_v1.json')!=manifest['source_hashes']['business_faq']:raise ValueError('Frozen catalogue changed')
        if digest(ROOT/'reports/faq_expansion/first_live_selection/evaluator_profile.json')!=manifest['source_hashes']['evaluator_profile']:raise ValueError('Evaluator profile changed')
        for record in state['cases'].values():
            for relative,sha in record.get('response_artifact_sha256',{}).items():
                if digest(directory/relative)!=sha:raise ValueError('Captured response artifact changed')
                saved=json.loads((directory/relative).read_text())
                matches=[(i,r) for i,r in enumerate(record.get('responses',[])) if r['attempt_id']==saved['response']['attempt_id']]
                if len(matches)!=1 or matches[0][1]!=saved['response'] or record['payloads'][matches[0][0]]!=saved['payload']:raise ValueError('Checkpoint response/payload differs from immutable capture')
            for grade_history in record.get('metrics',{}).values():
                for grade in grade_history:
                    if grade.get('artifact_sha256'):
                        if digest(directory/grade['path'])!=grade['artifact_sha256']:raise ValueError('Completed metric artifact changed')
                        saved=json.loads((directory/grade['path']).read_text())
                        if any(grade.get(field)!=saved.get(field) for field in ('score','method','job_id','material_sha256','profile_sha256')):raise ValueError('Checkpoint metric differs from immutable evaluator result')
        for name in ('ragas','deepeval'):
            if package_versions(ROOT/f'.venv-{name}'/'bin/python')!=manifest['packages'][name]:raise ValueError('Evaluator package versions changed')
        if manifest['mode']!='execute':raise ValueError('Only an actual execute campaign can resume; do not promote a dry/synthetic plan')
    else:
        identifier=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        directory=args.out or ROOT/'reports/full_pack'/identifier
        directory.mkdir(parents=True,exist_ok=False)
        key=os.urandom(32);(directory/'identity.private.key').write_bytes(key);os.chmod(directory/'identity.private.key',0o600)
        manifest=build_manifest(config,env,key,identifier,now(),args)
        immutable(directory/'manifest.json',manifest)
        snapshot=directory/'dataset.snapshot.json';snapshot.write_bytes((ROOT/'datasets/business_faq_v1.json').read_bytes());os.chmod(snapshot,0o444)
        state={'manifest_sha256':digest(directory/'manifest.json'),'created_at':now(),'updated_at':now(),
            'expires_at':time.time()+args.max_seconds,'run_status':'PLANNED','cases':{},'ledger_counts':{}}
        Ledger(directory/'requests.sqlite',{'api':args.max_api_requests,'judge':args.max_judge_requests},state['expires_at'])
    lock=(directory/'campaign.lock').open('a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close();raise ValueError('Campaign already running')
    campaign=Campaign(directory,manifest,state,config,env,key)
    for selected_case in manifest['cases']:campaign.start_case(selected_case)
    try:
        if args.mode=='resume':
            try:campaign.ledger.acknowledge_recovery()
            except BudgetStop as exc:
                state.update(run_status='PARTIAL',stop_reason=str(exc));campaign.checkpoint(report=True);return 2
        if digest(directory/'dataset.snapshot.json')!=manifest['source_hashes']['business_faq']:raise ValueError('Campaign snapshot changed')
        if args.trace_export:
            frozen=json.loads(args.trace_export.read_text());sha=digest(args.trace_export)
            relative=Path('trace_imports')/(sha+'.json')
            if not (directory/relative).exists():immutable(directory/relative,frozen)
            state['trace_import']=str(relative)
        if args.evidence:
            content=json.loads(args.evidence.read_text());sha=digest(args.evidence)
            relative=Path('evidence_imports')/(sha+'.json')
            if not (directory/relative).exists():immutable(directory/relative,content)
            state['evidence_import']=str(relative)
        state['retry_evaluators']=args.retry_evaluators
        if args.mode in ('plan','preview'):
            for c in manifest['cases']:campaign.start_case(c)['status']='NOT_RUN' if c['executable'] else 'BLOCKED'
            if args.mode=='preview':
                manifest['synthetic']=True
                # Preview is explicitly labelled; immutable manifest stays a plan artifact.
                for case in manifest['cases'][:2]:
                    state['cases'][case['id']].update(status='INCONCLUSIVE',attempted=True,responses=[{
                        'attempt_id':'SYNTHETIC-PREVIEW','response_sha256':'0'*64,'status_code':200,'application_status':'success',
                        'complete':True,'answer':'SYNTHETIC ONLY: <script>alert("escaped")</script> No MTN data.',
                        'first_byte_ms':12,'first_event_ms':15,'elapsed_ms':40}])
                state['run_status']='SYNTHETIC_PREVIEW'
            campaign.checkpoint(report=True);print(json.dumps(manifest['estimates'],indent=2));code=0
        else:
            if manifest.get('execution_prerequisites'):
                state.update(run_status='BLOCKED',stop_reason='Complete dependency/evaluator prerequisites listed in the manifest before live execution.')
                campaign.checkpoint(report=True);return 2
            for package in ('ragas','deepeval'):
                if manifest['packages'][package].get(package)!=PINNED[package]:
                    state.update(run_status='BLOCKED',stop_reason=f'Install pinned {package} environment before live execution.')
                    campaign.checkpoint(report=True);print('Blocked: missing pinned evaluator environment.');return 2
            previous_handlers={}
            def cancel(*_):campaign.cancelled=True;raise KeyboardInterrupt
            for sig in (signal.SIGINT,signal.SIGTERM):previous_handlers[sig]=signal.signal(sig,cancel)
            try:code=campaign.run()
            finally:
                for sig,handler in previous_handlers.items():signal.signal(sig,handler)
        print('HTML report: '+str((directory/'report.html').resolve()),flush=True)
        return code
    finally:fcntl.flock(lock,fcntl.LOCK_UN);lock.close()
