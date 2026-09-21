import json, subprocess, base64, re, pathlib, sys
K=subprocess.run(['gcloud','secrets','versions','access','latest','--secret=anthropic_compliance_key','--project','strong-hue-507702-k7'],capture_output=True,text=True).stdout.strip()
B='https://api.anthropic.com/v1/compliance'
OUT=pathlib.Path('/home/paulo/slashid/slashid-ai-forwarder/anthropic/tests/fixtures/compliance')
WORKING='2fe4f004-d4ca-4dd8-a630-85cb8089a518'   # this conversation: never recorded

def call(path, params=''):
    url=f'{B}{path}'+(('?'+params) if params else '')
    r=subprocess.run(['curl','-s','-w','\n%{http_code}',url,'-H',f'x-api-key: {K}','-H','anthropic-version: 2023-06-01'],capture_output=True,text=True)
    body,code=r.stdout.rsplit('\n',1)
    return {"request":{"method":"GET","path":path,"params":params},"status":int(code),"body":json.loads(body)}

SUB=[
 (r'/home/paulo/\.claude/jobs/[0-9a-f]+/tmp/fixtures-ws','/workspace'),
 (r'/home/paulo/\.claude/jobs/[0-9a-f]+','/workspace'),
 (r'2fe4f004[0-9a-f-]*','00000000-0000-4000-8000-000000000000'),
 (r'34936bc5-3c79-4380-9a6a-c3ada9ae6608','11111111-1111-1111-1111-111111111111'),
 (r'org_01[A-Za-z0-9]{18,}','org_01AAAAAAAAAAAAAAAAAAAAAA'),
 (r'user_01[A-Za-z0-9]{18,}','user_01AbCdEfGhIjKlMnOpQrStUv'),
 (r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}','alice@example.com'),
 (r'/home/paulo','/home/user'),
 (r'\bpaulo\b','user'),
 (r'4d621dc4-bee0-4c32-a825-f9770f17db47','22222222-2222-2222-2222-222222222222'),
]
SESS={}   # real session uuid -> synthetic
def scrub_text(t):
    for pat,rep in SUB: t=re.sub(pat,rep,t)
    for real,fake in SESS.items(): t=t.replace(real,fake)
    return t
def scrub_clls(cid):
    raw=cid[5:]; d=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
    d['o']='11111111-1111-1111-1111-111111111111'
    d['p']='22222222-2222-2222-2222-222222222222'
    d['s']=SESS.get(d['s'],d['s'])
    return 'clls_'+base64.urlsafe_b64encode(json.dumps(d,separators=(',',':')).encode()).decode().rstrip('=')
def walk(o):
    # ip_address is scrubbed by key, never by pattern: a global IP regex also
    # rewrites the Chrome version in user_agent, and the real client agent is
    # the one field the denial activity carries that no frame does.
    if isinstance(o,dict):
        return {k: ('2001:db8::1' if k=='ip_address' else walk(v)) for k,v in o.items()}
    if isinstance(o,list): return [walk(v) for v in o]
    if isinstance(o,str):
        o=re.sub(r'clls_[A-Za-z0-9_-]+', lambda m: scrub_clls(m.group(0)), o)
        return scrub_text(o)
    return o

# map real session uuids to synthetic ones, excluding the working session
raw=call('/apps/sessions/local','limit=30')
keep=[]
n=0
for s in raw['body']['data']:
    d=json.loads(base64.urlsafe_b64decode(s['id'][5:]+'='*(-len(s['id'][5:])%4)))
    if d['s']==WORKING: continue
    n+=1; SESS[d['s']]=f'0000000{n}-0000-4000-8000-000000000000'
    keep.append(s)
raw['body']['data']=keep
print(f"sessions kept: {len(keep)} (working session excluded)")
(OUT/'sessions_list.json').write_text(json.dumps(walk(raw),indent=2)+'\n')

for i,s in enumerate(keep,1):
    m=call(f"/apps/sessions/local/{s['id']}/messages",'limit=1000&tool_result_max_bytes=-1&tool_use_input_max_bytes=-1')
    (OUT/f'session_messages_{i}.json').write_text(json.dumps(walk(m),indent=2)+'\n')
    print(f"  session_messages_{i}.json  {len(m['body'].get('data',[]))} messages")

for name,path,params in [
 ('organizations_me','/organizations/me',''),
 ('chats_list','/apps/chats','limit=100'),
 ('activities','/activities','limit=1000&order=asc'),
]:
    d=call(path,params)
    if name=='activities':
        d['body']['data']=[a for a in d['body']['data'] if a['type']!='compliance_api_accessed'][:40]
    (OUT/f'{name}.json').write_text(json.dumps(walk(d),indent=2)+'\n'); print(f"  {name}.json")

for i,c in enumerate(call('/apps/chats','limit=100')['body']['data'],1):
    m=call(f"/apps/chats/{c['id']}/messages",'')
    (OUT/f'chat_messages_{i}.json').write_text(json.dumps(walk(m),indent=2)+'\n')
    print(f"  chat_messages_{i}.json  {len(m['body'].get('chat_messages',[]))} messages")

rej=[call('/apps/chats','limit=3&updated_at.gte=2026-09-20T00:00:00Z'),
     call('/apps/sessions/local','limit=3&order=asc'),
     call('/apps/sessions/local','limit=3&order_by=updated_at'),
     call('/activities','limit=3&created_at%5Bgte%5D=2026-09-21T04:00:00Z'),
     call('/apps/chats','limit=3&organization_uuid=11111111-1111-1111-1111-111111111111'),
     call('/organizations','')]
(OUT/'filters_rejected.json').write_text(json.dumps(walk({"cases":rej}),indent=2)+'\n')
print("  filters_rejected.json:", [r['status'] for r in rej])
