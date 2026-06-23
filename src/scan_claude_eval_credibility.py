import json, re
from pathlib import Path
root=Path('output_data_batch/claude_code_claudeopus47')
eval_root=root/'eval_results'
log_root=root/'logs'
rows=[]
for p in sorted(eval_root.glob('*/*.json')):
    project=p.parent.name
    inst=p.stem
    try:
        data=json.loads(p.read_text(encoding='utf-8'))
    except Exception as e:
        rows.append({'project':project,'instance':inst,'json_error':str(e)})
        continue
    log=log_root/project/inst/'step2.log'
    txt=''
    if log.exists():
        try: txt=log.read_text(encoding='utf-8', errors='replace')
        except Exception as e: txt=f'LOG_READ_ERROR {e}'
    lower=txt.lower()
    external=[]
    pats=[
      ('patch_apply_fail', r'patch.*(does not apply|failed|corrupt patch|rejected|cannot apply)|agent_patch_applied.*false'),
      ('docker_fail', r'docker: error|cannot connect to the docker daemon|no such container|container.*not found|error response from daemon'),
      ('network', r'connection timed out|could not resolve|temporary failure|connection reset|network is unreachable|no cached version|could not get resource|could not download|read timed out|ssl|tls|proxy|remote host'),
      ('oom', r'outofmemory|out of memory|oom|killed'),
      ('timeout', r'timeout|timed out'),
      ('lock', r'could not create service of type filehasher|timeout waiting to lock|lock timeout|currently in use|resource temporarily unavailable|failed to lock'),
      ('infra', r'traceback|permission denied|no space left|input/output error|read-only file system'),
    ]
    for name,pat in pats:
        if re.search(pat, lower): external.append(name)
    rows.append({
      'project':project,'instance':inst,
      'passed':data.get('agent_patch_passed'),
      'applied':data.get('agent_patch_applied'),
      'infra_valid':data.get('eval_infrastructure_valid'),
      'log_exists':log.exists(),'external':sorted(set(external)),
      'log':str(log)
    })
from collections import defaultdict
summ=defaultdict(lambda:{'total':0,'pass':0,'false':0,'apply_fail':0,'infra_bad':0,'missing_log':0,'external':0})
for r in rows:
    s=summ[r['project']]
    s['total']+=1
    if r.get('passed') is True: s['pass']+=1
    if r.get('passed') is False: s['false']+=1
    if r.get('applied') is False: s['apply_fail']+=1
    if r.get('infra_valid') is False: s['infra_bad']+=1
    if not r.get('log_exists'): s['missing_log']+=1
    if r.get('external'): s['external']+=1
print('=== SUMMARY ===')
for proj in sorted(summ): print(proj, summ[proj])
print('=== APPLY/INFRA/MISSING/EXTERNAL SUSPECTS ===')
for r in rows:
    if r.get('applied') is False or r.get('infra_valid') is False or not r.get('log_exists') or r.get('external'):
        print(json.dumps(r, ensure_ascii=False))
print('=== FALSE COUNT ===')
false=[r for r in rows if r.get('passed') is False]
print(len(false))
for r in false:
    print(f"{r['project']}\t{r['instance']}\texternal={','.join(r.get('external') or [])}")
