"""Standalone developer HTML, CSV and JSON; captured text is always escaped."""
import csv
import html
import json
import math
from collections import Counter,defaultdict
from pathlib import Path
from .campaign_io import atomic
from .campaign_privacy import sanitize


def performance(manifest,records):
    groups=defaultdict(lambda:{'attempts':0,'failures':0,'responses':[]})
    cases={c['id']:c for c in manifest['cases']}
    for case_id,record in records.items():
        group=groups[cases[case_id].get('workload','other')]
        for response in record.get('responses',[]):
            group['attempts']+=1
            if response.get('status_code')!=200 or response.get('error') or not response.get('complete') or response.get('application_status') not in ('success',):group['failures']+=1
            else:group['responses'].append(response)
    output={};settings=manifest['performance'];minimum=settings['performance_min_samples']
    for name,group in groups.items():
        result={k:group[k] for k in ('attempts','failures')};responses=group['responses'];result['n']=len(responses)
        result['completion_rate']=len(responses)/group['attempts'] if group['attempts'] else None
        for key in ('first_byte_ms','first_event_ms','elapsed_ms'):
            values=sorted(r[key] for r in responses if isinstance(r.get(key),(float,int)))
            result[key]={'n':len(values),'p50':values[math.ceil(.5*len(values))-1] if values else None,
                         'p95':values[math.ceil(.95*len(values))-1] if values else None,'min':min(values) if values else None,'max':max(values) if values else None}
        budget=settings['performance_response_budget_ms'].get('rag' if name=='faq' else 'tool') if name in ('faq','multi_tool') else None
        result['budget_total_ms']=budget;result['minimum_samples']=minimum
        if name=='single_tool':result['status']='DESCRIPTIVE_ONLY — not a multi-tool workload'
        elif len(responses)<minimum or budget is None:result['status']='INCONCLUSIVE'
        elif result['elapsed_ms']['n']!=len(responses) or result['first_event_ms']['n']!=len(responses):result['status']='INCONCLUSIVE'
        else:result['status']='FAIL' if group['failures'] or result['elapsed_ms']['p95']>budget or result['first_event_ms']['p95']>settings['first_event_budget_ms'] else 'PASS'
        output[name]=result
    return {'method':'Nearest-rank descriptive P50/P95 on successful complete application responses, failures reported separately. Serial functional samples, not a load/capacity test. Workload labels indicate catalogue intent, not verified backend tool-call cardinality.',
        'scope':'Direct API only; excludes Meta delivery/WhatsApp typing. First loader event is progress, not necessarily first useful answer.', 'groups':output}


def exports(directory,manifest,state,private=()):
    directory=Path(directory);records=state['cases'];definitions=manifest['metric_definitions']
    suites=defaultdict(Counter);metrics=defaultdict(list);defects=[];details=[]
    for case in manifest['cases']:
        rec=records.get(case['id'],{});status=rec.get('status','NOT_RUN');counts=suites[case['suite']];counts['planned']+=1
        counts['executable']+=bool(case['executable']);counts['attempted']+=bool(rec.get('attempted'))
        counts['captured']+=any(r.get('status_code') is not None for r in rec.get('responses',[]));counts[status.lower()]+=1
        grades=[g for histories in rec.get('metrics',{}).values() for g in histories if g.get('score') is not None]
        counts['validly_scored']+=bool(grades);counts['provisional']+=bool(grades and case.get('faq_reference'))
        for name,histories in rec.get('metrics',{}).items():
            for grade in histories:
                if grade.get('score') is not None:metrics[name].append(grade['score'])
                elif grade.get('error'):
                    defects.append({'test_id':case['id'],'category':'evaluator error','impact':'metric unassessed','strength':'observed evaluator failure','area':'Evaluator service/adapter','description':name+': '+grade['error']})
                flags=grade.get('candidate_flags') or {}
                if any(flags.get(k) for k in ('critical_fabrication_candidate','instruction_hijack_candidate','disclosure_candidate')):
                    defects.append({'test_id':case['id'],'category':'candidate critical LLM finding','impact':'CRITICAL CANDIDATE — human evidence review required','strength':'model-based, unverified','area':'Agent policy, source/tool trace review','description':str(grade.get('reason',''))[:500]})
                if grade.get('score') is not None and name in definitions:
                    d=definitions[name];threshold=d['threshold'];bad=threshold is not None and ((grade['score']<threshold) if d['direction']=='higher' else (grade['score']>threshold))
                    if bad:defects.append({'test_id':case['id'],'category':'candidate LLM/document finding','impact':'quality review','strength':'provisional native metric','area':'Answer or retrieval quality','description':f'{name}: {grade["score"]} vs {threshold}; '+grade.get('reason','')})
        if rec.get('trace_import_error'):
            defects.append({'test_id':case['id'],'category':'evidence import error','impact':'retrieval metrics unassessed','strength':'binding/provenance validation failure','area':'Trace export integration','description':'Trace does not meet exact-attempt provenance/rank contract; no retrieved context was inferred.'})
        if not case['executable']:
            source=bool(case.get('faq_reference'))
            defects.append({'test_id':case['id'],'category':'source-data issue' if source else 'prerequisite','impact':'blocked','strength':'document review flag' if source else 'configuration/fixture absent','area':'Business reference approval' if source else 'QA fixtures/channel/APIM','description':'; '.join(case['campaign_blockers'])})
        elif status in ('FAIL','BLOCKED','INCONCLUSIVE'):
            environment=all(r.get('status_code') is None for r in rec.get('responses',[])) and bool(rec.get('responses'))
            if status in ('FAIL','BLOCKED') or rec.get('uncertain_submission'):
                defects.append({'test_id':case['id'],'category':'environment error' if environment else 'deterministic failure' if status=='FAIL' else 'missing evidence',
                    'impact':'critical' if rec.get('critical_observed') else status.lower(),'strength':'exact-attempt evidence' if rec.get('critical_observed') else 'observed protocol/application behaviour' if status=='FAIL' else 'unassessed',
                    'area':'QA connectivity' if environment else 'API/backend/session handling','description':'; '.join(rec.get('reasons',[]))})
        details.append({'case':case,'result':rec,'status':status})
    defects.sort(key=lambda d:0 if d['impact']=='critical' else 1 if d['category']=='candidate critical LLM finding' else 2 if d['category']=='deterministic failure' else 3)
    critical_summary={'recorded_observations':sum(bool(r.get('critical_observed')) for r in records.values()),'model_candidates':sum(d['category']=='candidate critical LLM finding' for d in defects),'note':'Counts of recorded findings only; unassessed outcomes are unknown.'}
    metric_summary={name:{**definition,'valid_n':len(metrics[name]),'valid_score_average':sum(metrics[name])/len(metrics[name]) if metrics[name] else None,
        'judge_errors':sum(d['category']=='evaluator error' and d['description'].startswith(name+':') for d in defects)} for name,definition in definitions.items()}
    report={'manifest':manifest,'run_status':state['run_status'],'updated_at':state['updated_at'],'stop_reason':state.get('stop_reason'),
        'coverage':{k:dict(v) for k,v in suites.items()},'metrics':metric_summary,'performance':performance(manifest,records),
        'ledger_counts':state.get('ledger_counts',{}),'critical_summary':critical_summary,'defects':defects,'tests':details,
        'limitations':['No unsupported overall pass rate. Missing backend evidence stays null.','Harness tests are separate from live cases.',
            'Prior ten-question pilot average 0.90 is provisional/mixed-settings; it is not this campaign result.',
            'Native frameworks execute only when a recorded worker result says so; no Garak/DeepTeam execution claimed.','Not a regulatory certification or exhaustive security audit.']}
    safe=sanitize(report,private);atomic(directory/'results.json',safe)
    def csvfile(name,fields,rows):
        with (directory/name).open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader()
            for row in rows:
                # CSV exports can be opened in spreadsheets; never execute a captured formula.
                writer.writerow({k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    csvfile('defects.csv',['test_id','category','impact','strength','area','description'],safe['defects'])
    csvfile('cases.csv',['test_id','suite','status','attempted','captured','provisional','reason'],[
        {'test_id':d['case']['id'],'suite':d['case']['suite'],'status':d['status'],'attempted':bool(d['result'].get('attempted')),
         'captured':len(d['result'].get('responses',[])),'provisional':bool(d['case'].get('faq_reference')),
         'reason':'; '.join(d['result'].get('reasons',d['case']['campaign_blockers']))} for d in safe['tests']])
    e=lambda v:html.escape(str(v),quote=True)
    def pre(value):return '<pre>'+e(json.dumps(value,indent=2,ensure_ascii=False))+'</pre>'
    coverage_rows=''.join('<tr><td>'+e(suite)+'</td>'+''.join('<td>'+str(values.get(k,0))+'</td>' for k in ('planned','executable','attempted','captured','validly_scored','pass','fail','provisional','blocked','inconclusive','not_run'))+'</tr>' for suite,values in safe['coverage'].items())
    metric_rows=''.join('<tr><td>'+e(k)+'</td><td>'+e(v['method'])+'</td><td>'+e(v['direction'])+'</td><td>'+str(v['valid_n'])+'</td><td>'+e(v['valid_score_average'])+'</td><td>'+e(v['threshold'])+' — '+e(v['threshold_status'])+'</td><td>'+str(v['judge_errors'])+'</td></tr>' for k,v in safe['metrics'].items())
    defects_html=''.join('<tr class="filterable" data-search="'+e(' '.join(str(v) for v in d.values()).lower())+'">'+''.join('<td>'+e(d[k])+'</td>' for k in ('test_id','category','impact','strength','area','description'))+'</tr>' for d in safe['defects'])
    detail_html=''
    for detail in safe['tests']:
        c,r=detail['case'],detail['result'];ref=c.get('faq_reference')
        item={'question(s)':[t.get('message','') for t in c.get('turns',[])],
            'redacted_payloads':r.get('payloads',[]),'expected_behaviour':c.get('expected'),
            'reference':ref.get('reference_answer') if ref else None,'exact_reference_passages':ref.get('reference_passages') if ref else None,
            'reference_review_status':ref.get('reference_review_status') if ref else None,
            'responses':[{k:v for k,v in x.items() if k not in ('raw_body','events','headers')} for x in r.get('responses',[])],
            'metrics':r.get('metrics',{}),'backend_evidence_scores':r.get('evidence_scores',{}),'verified_critical_observed':r.get('critical_observed',False),'missing_evidence':r.get('missing_evidence',[]),
            'blockers':c['campaign_blockers'],'observations':r.get('reasons',[]),
            'trace_provenance':r.get('trace_provenance'),
            'reproduce':'Inspect immutable responses/ and metrics/ artifacts for these attempt IDs. A new independent request requires a new campaign, not overwriting this observation.'}
        detail_html+='<details class="filterable" data-search="'+e((c['id']+' '+c['suite']+' '+detail['status']+' '+c['category']).lower())+'"><summary>'+e(c['id']+' · '+c['suite']+' · '+detail['status'])+'</summary>'+pre(item)+'</details>'
    title=('SYNTHETIC PREVIEW — ' if manifest.get('synthetic') else '')+'Morpheus QA campaign '+manifest['campaign_id']
    text='''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'''+e(title)+'''</title><style>
body{font:15px system-ui,sans-serif;background:#f3f5f8;color:#15243a;margin:0}main{max-width:1450px;margin:auto;padding:28px}h1{font-size:30px}h2{margin-top:32px}header,.box,details{background:white;border:1px solid #dae0e8;border-radius:8px;padding:16px;margin:12px 0}.banner{background:#fff2bd;padding:12px;border-left:5px solid #b17c00}table{width:100%;border-collapse:collapse;background:white;font-size:13px}td,th{text-align:left;padding:9px;border:1px solid #dce2eb;vertical-align:top}th{background:#172e4e;color:white;position:sticky;top:0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.5}summary{cursor:pointer;font-weight:600}input{width:min(700px,90%);padding:12px;font:inherit}nav a{margin-right:16px}.scroll{overflow:auto;max-height:540px}.muted{color:#52647d}footer{padding:20px}</style></head><body><main><header><h1>'''+e(title)+'''</h1><p><strong>Run status: '''+e(state['run_status'])+'''</strong> · Updated '''+e(state['updated_at'])+'''</p><p>'''+e(state.get('stop_reason') or 'No stop reason recorded')+'''</p><p><strong>'''+e(manifest['estimates']['planned_cases'])+''' planned · '''+e(manifest['estimates']['executable_cases'])+''' eligible · '''+e(manifest['estimates']['blocked_cases'])+''' blocked</strong></p><p>Hard caps: '''+e(manifest['limits']['api_requests'])+''' QA requests · '''+e(manifest['limits']['judge_requests'])+''' judge HTTP requests · '''+e(manifest['limits']['campaign_seconds']/3600)+''' hours. Serial execution.</p><nav><a href="#coverage">Coverage</a><a href="#metrics">Metrics</a><a href="#defects">Defects</a><a href="#tests">Tests</a><a href="#blocked">Prerequisites</a></nav></header><p class="banner">Draft references and LLM findings are provisional. HTTP success, politeness and keywords do not prove grounding, safety or authorization. No unrelated metrics are combined into one overall score.</p><h2>Critical finding register</h2>'''+pre(safe['critical_summary'])+'''<details><summary>Run configuration, package/model versions, fingerprints and request limits</summary>'''+pre({k:v for k,v in safe['manifest'].items() if k!='cases'})+'''</details><h2 id="coverage">Suite coverage — counts / planned denominator</h2><p>Columns overlap: scored/provisional are coverage indicators, not mutually exclusive outcomes. A dry plan has no live observations. Harness unit-test counts belong to the separate build-validation record.</p><div class="scroll"><table><tr><th>Suite</th>'''+''.join('<th>'+e(k)+'</th>' for k in ('Planned','Executable','Attempted','Captured','Scored','Passed','Failed','Provisional','Blocked','Inconclusive','Not run'))+'''</tr>'''+coverage_rows+'''</table></div><h2 id="metrics">Per-metric results</h2><p>Valid n counts evaluated turn/metric results, not cases. Failed evaluator attempts stay outside the valid-score mean and remain visible as judge errors. Source/reference provenance and safety/authorization limitations still apply.</p><table><tr><th>Metric</th><th>Method</th><th>Direction</th><th>Valid n</th><th>Valid mean</th><th>Threshold / approval</th><th>Judge errors</th></tr>'''+metric_rows+'''</table><h2>Performance observations</h2><details><summary>Direct API timing distributions and minimum-sample gates</summary>'''+pre(safe['performance'])+'''</details><h2 id="defects">Developer defect and prerequisite register</h2><p>Critical LLM candidates require review; verified critical evidence overrides aggregate results. Filter applies to defects and test details.</p><input id="filter" placeholder="Filter by test ID, suite, status or defect category" aria-label="Filter report"><div class="scroll"><table><tr><th>Test</th><th>Category</th><th>Impact</th><th>Evidence strength</th><th>Investigation area</th><th>Observation</th></tr>'''+defects_html+'''</table></div><h2 id="tests">Expandable per-test evidence</h2>'''+detail_html+'''<h2 id="blocked">Blocked tests and prerequisites</h2><details><summary>Show prerequisite gaps and all blocked test IDs</summary>'''+pre({'prerequisites':manifest['missing_prerequisites'],'blocked_test_ids':[d['case']['id'] for d in safe['tests'] if not d['case']['executable']]})+'''</details><h2>Scope and limitations</h2>'''+pre(safe['limitations'])+'''<footer>Self-contained report. No CDN, remote scripts, telemetry or dashboard login.</footer></main><script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('.filterable').forEach(function(x){x.hidden=!x.dataset.search.includes(q)})});</script></body></html>'''
    (directory/'report.html').write_text(text)
    (directory/'summary.md').write_text('# '+title+'\n\nStatus: '+state['run_status']+'\n\n'+str(manifest['estimates'])+'\n\nActual outbound requests: '+str(state.get('ledger_counts',{}))+'\n\nNo campaign-wide aggregate score. Draft scores provisional; unsupported metrics unassessed. See report.html.\n')
    return safe
