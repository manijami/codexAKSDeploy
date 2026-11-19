"""Separate bounded guardrail campaign; historical campaigns and scores are read-only."""
import argparse,copy,csv,fcntl,hashlib,html,json,math,os,signal,sqlite3,subprocess,time
from collections import Counter
from pathlib import Path
from contextlib import closing
from .config import ROOT,load,secrets,target
from .campaign import Campaign,now
from .campaign_io import Ledger,BudgetStop,atomic,immutable,fingerprint
from .campaign_plan import runtime_binding,code_fingerprints,package_versions,PROFILE
from .campaign_privacy import sanitize
from .faq_pilot import digest

DATA=ROOT/'datasets/safety_guardrails_v1.json'
DEFAULT=ROOT/'reports/safety_guardrails/v1'
OWN=('qa/safety_pack.py','scripts/run_safety_guardrails.py','scripts/safety_guardrail_judge.py','run_safety_guardrails.command','datasets/safety_guardrails_v1.json')
def hashes():return {**code_fingerprints(),**{p:digest(ROOT/p) for p in (*OWN,'config/payload.template.json',str(PROFILE.relative_to(ROOT)))}}
def read(p):return json.loads(p.read_text())

def make_plan(directory,config,env,clock=None):
    if directory.exists():raise ValueError('Plan directory already exists')
    profile=read(PROFILE);expected={'max_tokens':4096,'temperature':0.01,'top_p':0.1,'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}
    if profile['model']!=env.get('MORPHEUS_EVAL_MODEL') or profile['generation_parameters']!=expected:raise ValueError('Recorded evaluator configuration mismatch')
    endpoint=target(config,env,include_key=False)
    if endpoint!='https://qa-serviceauth.mtn.co.za/morpheus-v5/api/prepaid/text?platform=whatsapp':raise ValueError('QA endpoint mismatch')
    if not config.get('response_mapping_confirmed') or not env.get('QA_MSISDN_A') or not(env.get('MORPHEUS_API_KEY') or env.get('QA_API_KEY_A')):raise ValueError('Configured QA identity/mapping missing')
    cases=copy.deepcopy(read(DATA)['cases']);eligible=[c for c in cases if c['executable']]
    turns=sum(len(c['turns']) for c in eligible)
    if len(cases)!=114 or len(eligible)!=96 or turns!=102:raise ValueError('Unexpected safety catalogue scope')
    for i,c in enumerate(eligible):c['batch']='guardrails:'+str(i//10)
    directory.mkdir(parents=True);key=os.urandom(32);(directory/'identity.private.key').write_bytes(key);os.chmod(directory/'identity.private.key',0o600)
    start=time.time() if clock is None else clock
    m={'schema_version':1,'campaign_id':'GUARDRAILS-V1-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime(start)),'created_at':now(),
       'mode':'new_safety_campaign','cases':cases,'endpoint':endpoint,'binding':runtime_binding(config,env,key),'code_sha256':hashes(),
       'source_sha256':digest(DATA),'expires_at':start+21600,'packages':package_versions(ROOT/'.venv-deepeval/bin/python'),
       'evaluator':{'model':profile['model'],'generation_parameters':expected,'sdk_retries':0,'structured_attempts':1},
       'limits':{'api_requests':102,'judge_requests':510,'campaign_seconds':21600,'batch_cases':10,'batch_api_requests':min(20,config['max_live_requests']),
           'concurrency':1,'min_interval_seconds':config['min_interval_seconds'],'http_seconds':min(45,config['timeout_seconds']),
           'metric_worker_seconds':75,'max_evaluator_attempts':1,'auth_failure_stop':2,'connectivity_failure_stop':1},
       'subscriber_route_contract':'Existing POSTPAID payload on prepaid QA route preserved; owner applicability remains unconfirmed.',
       'scope':'96 synthetic visible-response cases (102 turns) plus 18 blocked backend/fixture areas; no writes, payment initiation, A/B tampering or fabricated retrieved chunks.',
       'rubric_status':'DRAFT_REQUIRES_BUSINESS_SECURITY_REVIEW','metric_definitions':{'guardrail_review':{'max_requests':2},'deepeval_toxicity':{'max_requests':3}},
       'safety_pass_rate':None,'safety_score':None,'retrieval_grounding_authorization_metrics':None}
    immutable(directory/'manifest.json',m);immutable(directory/'catalogue.snapshot.json',read(DATA))
    state={'manifest_sha256':digest(directory/'manifest.json'),'expires_at':m['expires_at'],'created_at':now(),'updated_at':now(),'run_status':'PLANNED_NOT_EXECUTED','ledger_counts':{'api':0,'judge':0},'retry_evaluators':False,'cases':{}}
    for c in cases:state['cases'][c['id']]={'status':'NOT_RUN' if c['executable'] else 'BLOCKED','responses':[],'payloads':[],'metrics':{},'reasons':list(c['campaign_blockers']),'missing_evidence':['Draft rubric lacks business/security sign-off.','Exact-attempt backend authorization, search chunks, tool state and human labels unavailable; no verified safety score.']}
    atomic(directory/'checkpoint.json',state)
    render(directory,m,state,secrets(env));return m,state

def validate(directory,m,s,config,env):
    if digest(directory/'manifest.json')!=s['manifest_sha256'] or m['code_sha256']!=hashes() or m['source_sha256']!=digest(DATA) or digest(directory/'catalogue.snapshot.json')!=digest(DATA):raise ValueError('Safety manifest/code/catalogue integrity failed')
    key=(directory/'identity.private.key').read_bytes()
    if runtime_binding(config,env,key)!=m['binding']:raise ValueError('Private configuration fingerprint changed')
    if m['packages']!=package_versions(ROOT/'.venv-deepeval/bin/python'):raise ValueError('Native adapter package versions changed')
    if s['expires_at']!=m['expires_at'] or s.get('retry_evaluators') or set(s['cases'])!={c['id'] for c in m['cases']}:raise ValueError('Safety checkpoint policy changed')
    known=set();case_lookup={c['id']:c for c in m['cases']}
    for cid,rec in s['cases'].items():
        for path,sha in rec.get('response_artifact_sha256',{}).items():
            if digest(directory/path)!=sha:raise ValueError('Captured evidence changed')
            saved=read(directory/path);matches=[(i,r) for i,r in enumerate(rec['responses']) if r['attempt_id']==saved['response']['attempt_id']]
            if len(matches)!=1 or matches[0][1]!=saved['response'] or rec['payloads'][matches[0][0]]!=saved['payload']:raise ValueError('Captured checkpoint no longer matches artifact')
        if len(rec.get('response_artifact_sha256',{}))!=len(rec['responses']):raise ValueError('Missing response artifact')
        for metric,hs in rec.get('metrics',{}).items():
            if metric not in m['metric_definitions']:raise ValueError('Unexpected evaluator job')
            for g in hs:
                job=read(directory/'jobs'/(g['job_id']+'.json'));known.add(g['job_id'])
                if job['material_sha256']!=fingerprint(job['material']) or job['profile']!=m['evaluator'] or job['request_limit']!=m['metric_definitions'][metric]['max_requests'] or job['material_sha256']!=g['material_sha256'] or job['ledger']!=str((directory/'requests.sqlite').resolve()):raise ValueError('Evaluator material/profile/limits changed')
                response=next((r for r in rec['responses'] if r['attempt_id']==g['attempt_id']),None)
                if not response or job['material']['response']!=response['answer'] or job['material']['response_sha256']!=response['response_sha256']:raise ValueError('Evaluator response binding changed')
                turn=int(g['attempt_id'].rsplit(':',1)[1])-1;case=case_lookup[cid]
                if job['metric']!=metric or job['job_id']!=g['job_id'] or job['material']['test_id']!=cid or job['material']['user_input']!=sanitize(case['turns'][turn]['message'],secrets(env)) or job['material']['expected']!=sanitize(case['expected'],secrets(env)):raise ValueError('Question/rubric binding changed')
                expected_job=fingerprint({'metric':metric,'material':job['material_sha256'],'code':m['code_sha256'],'profile':m['evaluator']})
                if expected_job!=g['job_id']:raise ValueError('Evaluator identifier binding changed')
                if g.get('artifact_sha256'):
                    if digest(directory/g['path'])!=g['artifact_sha256'] or any(read(directory/g['path']).get(k)!=g.get(k) for k in ('score','completed','reviews','error','material_sha256','profile_sha256','job_id')):raise ValueError('Evaluator result changed')
                elif g.get('score') is not None or g.get('completed'):raise ValueError('Completed evaluation lacks artifact')
    if (directory/'requests.sqlite').exists():
        with closing(sqlite3.connect(directory/'requests.sqlite')) as db:
            caps={k:(maximum,expires) for k,maximum,expires in db.execute('SELECT * FROM caps')}
            if caps!={k:(m['limits'][k+'_requests'],m['expires_at']) for k in ('api','judge')}:raise ValueError('Request/time limits changed')
            if dict(db.execute('SELECT kind,count(*) FROM calls GROUP BY kind'))!={k:v for k,v in s['ledger_counts'].items() if v}:raise ValueError('Ledger/checkpoint counts changed')
            allowed={m['campaign_id']+':'+c['id']+':'+str(i+1) for c in m['cases'] if c['executable'] for i in range(len(c['turns']))}
            for kind,job in db.execute('SELECT kind,job FROM calls'):
                if kind=='api' and job not in allowed or kind=='judge' and job not in known:raise ValueError('Out-of-scope reservation')
    return key

def render(directory,m,s,private=()):
    rows=[];reviewrows=[];areas=Counter();coverage={};errors=[]
    for c in m['cases']:
        r=s['cases'][c['id']];a=coverage.setdefault(c['category'],{'planned':0,'executable':0,'captured_cases':0,'captured_turns':0,'http_success':0,'application_success':0,'provisional_review_turns':0,'concern_turns':0,'unclear_turns':0,'evaluator_errors':0})
        a['planned']+=1;a['executable']+=c['executable'];a['captured_cases']+=bool(r['responses']);a['captured_turns']+=len(r['responses'])
        for response in r['responses']:
            a['http_success']+=response.get('status_code')==200;a['application_success']+=response.get('application_status')=='success'
            result={'test_id':c['id'],'category':c['category'],'case_status':r['status'],'question':c['turns'][int(response['attempt_id'].rsplit(':',1)[1])-1]['message'],'answer':response.get('answer'),'draft_expected':c['expected'],'http_status':response.get('status_code'),'application_status':response.get('application_status'),'first_event_ms':response.get('first_event_ms'),'total_ms':response.get('elapsed_ms'),'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],'safety_score':None,'rubric_status':c['rubric_review_status']}
            rows.append(result)
            grades=[g for hs in r['metrics'].values() for g in hs if g['attempt_id']==response['attempt_id']]
            for g in grades:
                reviewrows.append({'test_id':c['id'],'metric':g['metric'],**g})
                if g.get('error'):a['evaluator_errors']+=1;errors.append({'test_id':c['id'],'metric':g['metric'],'exception_type':g.get('exception_type'),'error':g['error']})
                if g.get('completed'):
                    a['provisional_review_turns']+=1;decisions=[x['decision'] for x in g['reviews']]
                    a['concern_turns']+='CONCERN' in decisions;a['unclear_turns']+='UNCLEAR' in decisions or len(set(decisions))>1
    safe=sanitize({'run_status':s['run_status'],'updated_at':s['updated_at'],'stop_reason':s.get('stop_reason'),'coverage':coverage,'ledger_counts':s['ledger_counts'],'safety_score':None,'safety_pass_rate':None,'evaluator_profile':m['evaluator'],'cases':rows,'evaluations':reviewrows,'errors':errors,'blocked_cases':[{'test_id':c['id'],'area':c['category'],'prerequisites':c['campaign_blockers']} for c in m['cases'] if not c['executable']]},private)
    atomic(directory/'results.json',safe)
    def csvout(name,data,fields):
        with (directory/name).open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
            for row in data:w.writerow({k:("'"+str(row.get(k,'')) if str(row.get(k,'')).lstrip().startswith(('=','+','-','@')) else row.get(k)) for k in fields})
    csvout('captured_cases.csv',safe['cases'],list(rows[0]) if rows else ['test_id','question','answer','safety_score'])
    csvout('coverage.csv',[{'area':k,**v} for k,v in coverage.items()],['area',*next(iter(coverage.values()))])
    esc=lambda v:html.escape(str(v));tr=lambda values:'<tr>'+''.join('<td>'+esc(v)+'</td>' for v in values)+'</tr>'
    contents='<!doctype html><html><head><meta charset="utf-8"><title>Guardrail QA developer report</title><style>body{font:15px system-ui;margin:30px;color:#173140}table{border-collapse:collapse;width:100%;margin:20px 0}td,th{border:1px solid #bdccd5;padding:8px;vertical-align:top}pre{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;padding:12px;background:#edf3f6;margin-top:8px}.note{background:#fff5d6;padding:15px}</style></head><body><h1>Safety and security guardrail QA</h1><p>Status: '+esc(s['run_status'])+'; '+esc(s.get('stop_reason') or '')+'</p><div class="note">All rubrics and model reviews are provisional. NO_VISIBLE_CONCERN is an observation, never a safety PASS. No comprehensive safety, authorization, fairness, retrieval or grounding score is assigned. Native DeepEval toxicity (lower is less toxic) is a separate language metric, not a safety certification. Backend cases remain blocked without their fixtures and exact-attempt traces. No Garak/DeepTeam suite execution is claimed.</div><p>Serial; no automatic Morpheus POST retries. Original campaigns are separate and unchanged. Request counts: '+esc(s['ledger_counts'])+'</p><p>Evaluator: '+esc(m['evaluator'])+'</p><h2>Coverage</h2><table>'+tr(['Area',*next(iter(coverage.values()))])+''.join(tr([k,*v.values()]) for k,v in coverage.items())+'</table><h2>Captured turns and provisional reviews</h2>'
    for row in safe['cases']:
        observations=[g for g in safe['evaluations'] if g['attempt_id']==row['attempt_id']]
        contents+='<details><summary>'+esc(row['test_id']+' — '+row['category'])+'</summary><pre>'+esc(json.dumps({'capture':row,'evaluations':observations},indent=2,ensure_ascii=False))+'</pre></details>'
    contents+='<h2>Remaining evaluator errors</h2><pre>'+esc(json.dumps(safe['errors'],indent=2))+'</pre><h2>Backend / fixture gaps</h2><pre>'+esc(json.dumps(safe['blocked_cases'],indent=2))+'</pre></body></html>'
    (directory/'report.html').write_text(contents)
    return safe

class SafetyCampaign(Campaign):
    def checkpoint(self,report=False):
        super().checkpoint(False)
        if report:render(self.directory,self.manifest,self.state,self.private)
    def capture_case(self,case):
        rec=self.start_case(case)
        if not rec.get('capture_done') and not rec.get('inflight') and case['executable']:
            turn=len(rec['responses'])+1;aid=self.manifest['campaign_id']+':'+case['id']+':'+str(turn)
            with closing(self.ledger.connect()) as db:reserved=db.execute("SELECT count(*) FROM calls WHERE kind='api' AND job=?",(aid,)).fetchone()[0]
            if reserved:
                path=Path('responses')/case['id']/f'turn_{turn:02d}.json'
                if (self.directory/path).exists():rec['inflight']={'attempt_id':aid,'path':str(path)}
                else:rec.update(capture_done=True,status='INCONCLUSIVE',uncertain_submission=True);rec['reasons'].append('Reserved POST has unknown outcome; never resent.');self.checkpoint();return
        try:return super().capture_case(case)
        except BudgetStop:
            if rec['responses']:
                last=rec['responses'][-1]
                if last.get('status_code') is None:
                    rec.update(status='BLOCKED',capture_done=True,connectivity_stop_recorded=True)
                    rec['reasons'].append('No HTTP response: connectivity blocked. This captured submission is not retried.')
                elif last.get('status_code') in (401,403):rec.update(status='FAIL',capture_done=True)
                self.checkpoint()
            raise
    def grade_case(self,case):
        rec=self.start_case(case)
        if any(r.get('status_code') is None for r in rec['responses']):
            rec['status']='BLOCKED';self.checkpoint();return
        for index,response in enumerate(rec['responses']):
            if response.get('status_code')!=200 or response.get('application_status')!='success' or not response.get('complete') or response.get('error') or not response.get('answer'):continue
            material=sanitize({'test_id':case['id'],'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],'user_input':case['turns'][index]['message'],'response':response['answer'],'expected':case['expected'],'prior_transcript':[{'question':case['turns'][i]['message'],'answer':r.get('answer')} for i,r in enumerate(rec['responses'][:index])],'reference_review_status':case['rubric_review_status']},self.private)
            for metric in ('guardrail_review','deepeval_toxicity'):
                self.stopped();histories=rec['metrics'].setdefault(metric,[])
                old=next((g for g in histories if g['attempt_id']==response['attempt_id']),None)
                if old:
                    if old.get('inflight'):
                        path=self.directory/old['path']
                        if path.exists():self.accept(old,read(path),path)
                        else:old.update(error='Interrupted evaluator not automatically retried',completed=False);old.pop('inflight',None)
                        self.checkpoint()
                    continue
                material_sha=fingerprint(material);job_id=fingerprint({'metric':metric,'material':material_sha,'code':self.manifest['code_sha256'],'profile':self.manifest['evaluator']})
                out=Path('metrics')/(job_id+'.json');job={'job_id':job_id,'metric':metric,'material':material,'material_sha256':material_sha,'profile':self.manifest['evaluator'],'request_limit':self.manifest['metric_definitions'][metric]['max_requests'],'threshold':None,'ledger':str((self.directory/'requests.sqlite').resolve())}
                immutable(self.directory/'jobs'/(job_id+'.json'),job)
                entry={'job_id':job_id,'metric':metric,'attempt_id':response['attempt_id'],'response_sha256':response['response_sha256'],'material_sha256':material_sha,'path':str(out),'inflight':True,'score':None,'provisional':True}
                histories.append(entry);self.checkpoint()
                try:result=self.worker(job,self.directory/out)
                except (KeyboardInterrupt,BudgetStop):raise
                except Exception as exc:result={'score':None,'completed':False,'error':'Evaluator worker incomplete','exception_type':type(exc).__name__,'job_id':job_id,'material_sha256':material_sha,'profile_sha256':fingerprint(job['profile'])};atomic(self.directory/out,result)
                self.accept(entry,result,self.directory/out);self.checkpoint()
                print(case['id']+' '+metric+': '+('reviewed (provisional)' if result.get('completed') else 'toxicity scored' if result.get('score') is not None else 'unassessed'),flush=True)
    def accept(self,entry,result,path):
        if result.get('job_id')!=entry['job_id'] or result.get('material_sha256')!=entry['material_sha256'] or result.get('profile_sha256')!=fingerprint(self.manifest['evaluator']):raise ValueError('Evaluator output binding mismatch')
        if read(path)!=result:raise ValueError('Evaluator output differs from immutable artifact')
        if result.get('score') is not None:
            score=result['score']
            if entry['metric']!='deepeval_toxicity' or type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:raise ValueError('Invalid toxicity result')
        if result.get('completed'):
            if entry['metric']!='guardrail_review' or len(result.get('reviews',[]))!=2:raise ValueError('Both visible-review perspectives required')
        if result.get('score') is not None or result.get('completed'):
            if result.get('generation_parameters')!=self.manifest['evaluator']['generation_parameters'] or result.get('evaluator_version')!='3.8.4':raise ValueError('Completed evaluator provenance mismatch')
        entry.update(result,artifact_sha256=digest(path),evaluated_at=now(),provisional=True);entry.pop('inflight',None)
    def worker_process(self,job,out):
        jobpath=self.directory/'jobs'/(job['job_id']+'.json')
        child=dict(os.environ);child.update({k:v for k,v in self.env.items() if k.startswith(('MORPHEUS_','QA_'))});child.update(DEEPEVAL_TELEMETRY_OPT_OUT='YES',OTEL_SDK_DISABLED='true')
        p=subprocess.Popen([str(ROOT/'.venv-deepeval/bin/python'),str(ROOT/'scripts/safety_guardrail_judge.py'),'--job',str(jobpath),'--out',str(out)],env=child,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:p.wait(timeout=max(.1,min(75,self.ledger.remaining_seconds())))
        except (subprocess.TimeoutExpired,KeyboardInterrupt) as exc:
            p.terminate()
            try:p.wait(timeout=3)
            except subprocess.TimeoutExpired:p.kill();p.wait()
            if isinstance(exc,KeyboardInterrupt):raise
            result={'score':None,'completed':False,'error':'Evaluator worker timeout','exception_type':'TimeoutError','job_id':job['job_id'],'material_sha256':job['material_sha256'],'profile_sha256':fingerprint(job['profile'])}
            if not out.exists():atomic(out,result)
        if not out.exists():raise ValueError('Evaluator produced no result')
        return read(out)

def execute(directory,m,s,config,env,key,max_new_cases=96,transport=None,worker=None):
    if not (directory/'requests.sqlite').exists():Ledger(directory/'requests.sqlite',{'api':m['limits']['api_requests'],'judge':m['limits']['judge_requests']},m['expires_at'])
    engine=SafetyCampaign(directory,m,s,config,env,key,transport,worker);engine.limits['connectivity_failure_stop']=1
    # A bounded invocation may visit already captured work for evaluator reconciliation, never resend it.
    new=0;stop=False
    old_handlers={}
    def cancel(*_):engine.cancelled=True;raise KeyboardInterrupt
    for sig in (signal.SIGINT,signal.SIGTERM):old_handlers[sig]=signal.signal(sig,cancel)
    try:
        s['run_status']='RUNNING';engine.checkpoint()
        for i,c in enumerate(m['cases']):
            engine.stopped();r=engine.start_case(c)
            if c['executable'] and not r.get('capture_done'):
                if new>=max_new_cases:stop=True;break
                new+=1
            before=len(r['responses']);engine.capture_case(c)
            if len(r['responses'])>before:
                healthy=r['responses'][-1].get('application_status')=='success'
                s['consecutive_application_failures']=0 if healthy else s.get('consecutive_application_failures',0)+1
                if s['consecutive_application_failures']>=3:raise BudgetStop('Three consecutive application failures/unavailable responses; remaining cases withheld without retry.')
            if c['executable']:engine.grade_case(c)
            if (i+1)%10==0:engine.checkpoint(True)
        s['run_status']='PARTIAL_BATCH_LIMIT' if stop else 'COMPLETE_CAPTURE_SCOPE'
        s.pop('stop_reason',None)
    except BudgetStop as exc:s.update(run_status='PARTIAL',stop_reason=str(exc))
    except Exception as exc:s.update(run_status='PARTIAL',stop_reason='Campaign stopped: '+type(exc).__name__+'; no automatic retries.')
    except KeyboardInterrupt:s.update(run_status='INTERRUPTED',stop_reason='Interrupted; uncertain POSTs and evaluator jobs will not be automatically retried.')
    finally:
        for sig,handler in old_handlers.items():signal.signal(sig,handler)
        engine.checkpoint(True)
    return 0 if s['run_status']=='COMPLETE_CAPTURE_SCOPE' else 2

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute',action='store_true');p.add_argument('--max-new-cases',type=int,default=96);p.add_argument('--directory',type=Path,default=DEFAULT);a=p.parse_args(argv)
    if not 1<=a.max_new_cases<=96:p.error('max-new-cases must be 1..96')
    directory=a.directory.resolve()
    if ROOT not in directory.parents:raise ValueError('Safety campaign must remain in this project')
    config,env=load()
    if not (directory/'manifest.json').exists():m,s=make_plan(directory,config,env)
    else:m,s=read(directory/'manifest.json'),read(directory/'checkpoint.json')
    with (directory/'campaign.lock').open('a+') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Safety campaign already active')
        key=validate(directory,m,s,config,env)
        if a.execute:
            code=execute(directory,m,s,config,env,key,a.max_new_cases)
        else:render(directory,m,s,secrets(env));print('Offline plan verified: 96 executable cases, 102 maximum QA POSTs, 510 maximum judge requests, 18 fixture-blocked areas.');code=0
        print('Safety report: '+str(directory/'report.html'));return code
