#!/usr/bin/env python3
"""Generate a secret-free Postman v2.1 collection from the case catalogue."""
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from qa.config import ROOT
from qa.runner import cases_from


def main():
    variables={'base_url':'https://qa-serviceauth.mtn.co.za','key_A':'','key_B':'','cookie_header_A':'','cookie_header_B':'','authorization_A':'','authorization_B':'',
      'msisdn_A':'','msisdn_B':'','conversation_id_A':'','conversation_id_B':'',
      'subscriber_type_A':'POSTPAID','subscriber_type_B':'POSTPAID'}
    folders={}
    for case in cases_from():
        if case.get('manual_only'):
            continue
        # Keep write/fault cases out of the default Postman runner.
        if case.get('writes') or case.get('payment') or case.get('fixture'):
            continue
        requests=[]
        for n,turn in enumerate(case.get('turns',[{'message':''}]),1):
            actor=turn.get('account','A')
            session_owner='A' if turn.get('session')=='a' else actor
            target=turn.get('target_msisdn',actor)
            prompt=turn.get('message','').replace('{{msisdn_B}}','{{msisdn_B}}')
            body=case.get('body',{'conversationId':'{{conversation_id_'+session_owner+'}}','msisdn':'{{msisdn_'+target+'}}','subscriberType':'{{subscriber_type_'+actor+'}}','message':prompt})
            req={'method':case.get('method','POST'),'header':[
              {'key':'Accept','value':'text/event-stream'}, {'key':'Content-Type','value':'application/json'},
              {'key':'Accept-Language','value':'en-US,en;q=0.9,ar;q=0.8'},
              {'key':'Origin','value':'https://qa-serviceauth.mtn.co.za'},
              {'key':'Referer','value':'https://qa-serviceauth.mtn.co.za/morpheus-v5/?key='},
              {'key':'Cookie','value':'{{cookie_header_'+actor+'}}','disabled':False},
              {'key':'Authorization','value':'{{authorization_'+actor+'}}','disabled':True}],
              'url':{'raw':'{{base_url}}/morpheus-v5/api/prepaid/text?platform=whatsapp&key={{key_'+actor+'}}',
                'host':['{{base_url}}'],'path':['morpheus-v5','api','prepaid','text'],
                'query':[{'key':'platform','value':'whatsapp'},{'key':'key','value':'{{key_'+actor+'}}'}]},
              'description':case['expected']+'\nSemantic/safety scores require backend evidence. This request alone does not prove a pass.'}
            if req['method']!='HEAD':
                req['body']={'mode':'raw','raw':json.dumps(body,indent=2,ensure_ascii=False),'options':{'raw':{'language':'json'}}}
            checks=[f'const expected = {json.dumps(turn.get("expected_status",case.get("expected_status",[200])))};',
              'pm.test("Expected HTTP status", function () { pm.expect(expected).to.include(pm.response.code); });',
              'if (pm.response.code === 200) { pm.test("SSE content type", function () { pm.expect(pm.response.headers.get("Content-Type") || "").to.include("text/event-stream"); }); }']
            requests.append({'name':case['id']+f' turn {n} — '+case['category'],'request':req,'response':[],
              'event':[{'listen':'test','script':{'type':'text/javascript','exec':checks}}]})
        folders.setdefault(case['suite'],[]).extend(requests)
    collection={'info':{'name':'MTN Morpheus WhatsApp QA','schema':'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
      'description':'Secret-free QA collection. Configure local variables from the supplied request. Run baseline alone first. Do not run every folder with the same conversation ID: use the Python harness for fresh sessions and evidence scoring. Write, payment, injected-failure and manual-only scenarios are in the Python catalogue. B-authentication must be configured independently for session-isolation tests.'},
      'variable':[{'key':k,'value':v,'type':'string'} for k,v in variables.items()],
      'item':[{'name':name,'item':items} for name,items in folders.items()]}
    (ROOT/'postman/Morpheus_WhatsApp_QA.postman_collection.json').write_text(json.dumps(collection,indent=2,ensure_ascii=False)+'\n')
    environment={'id':'f9c77643-5331-46cb-8c51-d3ec4ada1a2b','name':'Morpheus QA — local values required','values':[{'key':k,'value':v,'enabled':True,'type':'secret' if k.startswith(('key_','cookie_','authorization_','msisdn_','conversation_id_')) else 'default'} for k,v in variables.items()],'_postman_variable_scope':'environment'}
    (ROOT/'postman/Morpheus_QA.postman_environment.json').write_text(json.dumps(environment,indent=2)+'\n')
    print('Built secret-free Postman collection and environment.')

if __name__=='__main__':
    main()
