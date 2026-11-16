#!/usr/bin/env python3
"""Stage or activate a coordinated Dortmund runtime release, with fleet rollback.

Default mode only stages and validates. --activate-staged uses a previously
staged report and refuses to replace an activation attempt. Playback must be
stopped. This command never plays media or controls projector power.
"""
from pathlib import Path
import argparse
import sys,subprocess,concurrent.futures,shlex,base64,json,hashlib,time,urllib.request
repository=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(repository/'tools'))
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--addresses',type=Path,required=True)
parser.add_argument('--password-file',type=Path,required=True)
parser.add_argument('--admin-password-file',type=Path,required=True)
parser.add_argument('--report',type=Path,default=repository/'diagnostics/runtime-rollout-latest.json')
parser.add_argument('--archive',type=Path,default=repository/'diagnostics/runtime-release.tar.gz')
parser.add_argument('--activate',action='store_true')
parser.add_argument('--activate-staged',action='store_true')
args=parser.parse_args()
from runtime_release import build, classify_activation
report_path=args.report
report_path.parent.mkdir(parents=True,exist_ok=True)
archive=args.archive
package=build(repository,archive)
release=package['release']
hosts=json.loads(args.addresses.read_text())
installer=(repository/'tools/runtime_release.py').read_bytes()
archive_data=archive.read_bytes()
reuse = args.activate_staged
if reuse:
 previous = json.loads(report_path.read_text())
 assert previous['phase']=='staged' and previous['package']['release']==release,'Staged release changed or activation already attempted'
 report=previous
else:
 report={'package':package,'stage':{},'activate':{},'status':{},'errors':[],'phase':'staging'}
def save():report_path.write_text(json.dumps(report,indent=2)+'\n')
def ssh(host,command,code=None,timeout=35):
 command_args=['/opt/homebrew/bin/sshpass','-f',str(args.password_file),'ssh','-o','ControlMaster=no','-o','StrictHostKeyChecking=accept-new','-o','NumberOfPasswordPrompts=1','-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2','-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password','root@'+hosts[host],command]
 for attempt in range(3):
  r=subprocess.run(command_args,input=code,text=True,capture_output=True,timeout=timeout)
  if r.returncode==0:return r.stdout
  if attempt<2 and r.returncode==255 and 'Permission denied' in r.stderr:
   time.sleep(1)
   continue
  raise RuntimeError(host+': '+r.stderr[-5000:]+'\n'+r.stdout[-2000:])
 raise AssertionError('unreachable')
def remote(host,action):
 command=shlex.join(['python3','/tmp/immersive-runtime-release.py',action,release,'--host',host])
 return json.loads(ssh(host,command,timeout=100).strip().splitlines()[-1])
def stage(host):
 code='expected='+repr(host)+'\n'+'''import socket,base64,subprocess
from pathlib import Path
assert socket.gethostname()==expected,'Hostname mismatch'
'''
 code+="Path('/tmp/immersive-runtime-release.py').write_bytes(base64.b64decode("+repr(base64.b64encode(installer).decode())+"))\n"
 code+="Path('/tmp/immersive-runtime-release.tar.gz').write_bytes(base64.b64decode("+repr(base64.b64encode(archive_data).decode())+"))\n"
 code+="subprocess.run(['python3','/tmp/immersive-runtime-release.py','stage','/tmp/immersive-runtime-release.tar.gz','--host',expected],check=True)\n"
 return json.loads(ssh(host,'python3 -',code,timeout=70).strip().splitlines()[-1])
def batch(hostnames,function,field):
 failed=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
  jobs={pool.submit(function,h):h for h in hostnames}
  for job in concurrent.futures.as_completed(jobs):
   host=jobs[job]
   try:
    value=job.result();report[field][host]=value;print(json.dumps({'phase':field,**value}),flush=True)
   except Exception as exc:
    failed.append((host,str(exc)));print(json.dumps({'phase':field,'host':host,'error':str(exc)}),flush=True)
   save()
 if failed:raise RuntimeError(str(failed))
def systemctl(host,action):
 role='control' if host=='dortmund-control' else 'render'
 ssh(host,shlex.join(['systemctl',action,'immersive-'+role+'.service']),timeout=100)
 return {'host':host,'action':action}
password=args.admin_password_file.read_text().strip()
auth='Basic '+base64.b64encode(('admin:'+password).encode()).decode()
def api(path):
 req=urllib.request.Request('http://'+hosts['dortmund-control']+':8080'+path,headers={'Authorization':auth})
 with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
def assert_room_idle():
 health=api('/api/health')
 if health['playing']:raise RuntimeError('A show is running; refusing activation')
 if 'phone_scan_active' in health:
  busy=health['phone_scan_active']
 else:
  # Query older controllers through their existing capture protocol. Use the
  # master's installed WebSocket dependency; the rollout host needs none.
  code="import asyncio,json,websockets\nasync def main():\n async with websockets.connect('ws://127.0.0.1:8080/ws',open_timeout=5) as ws:\n  await ws.send(json.dumps({'role':'capture','hello':'runtime-rollout','protocol':1}))\n  await ws.send(json.dumps({'cmd':'capture_catalog','request_id':'rollout-check'}))\n  while True:\n   reply=json.loads(await asyncio.wait_for(ws.recv(),5))\n   if reply.get('request_id')=='rollout-check':\n    if reply.get('type')!='capture_catalog' or type(reply.get('busy')) is not bool:\n     raise RuntimeError('Cannot verify phone scan state')\n    print(json.dumps({'busy':reply['busy']}));return\nasyncio.run(main())\n"
  busy=json.loads(ssh('dortmund-control','python3 -',code))['busy']
 if busy:raise RuntimeError('A phone scan is running; refusing activation')
 return health
# First stage code and validate all devices without stopping their applications.
save()
if not reuse:batch(list(hosts),stage,'stage')
health=assert_room_idle()
slaves=[h for h in hosts if h!='dortmund-control']
report['phase']='staged';save()
if not args.activate and not reuse:
 print('All thirteen hosts staged. Activation not requested.',flush=True);sys.exit(0)
def probe(host):
 code="from pathlib import Path;import json;p=Path('/opt/immersive/runtime-manifest.json');print(json.dumps({'installed_release':json.loads(p.read_text()).get('release') if p.exists() else None,'snapshot_exists':Path('/var/backups/immersive-release-"+release+"/restore.json').exists()}))"
 return json.loads(ssh(host,'python3 -c '+shlex.quote(code)))
report['probe']={}
batch(list(hosts),probe,'probe')
plan=classify_activation(list(report['probe'].values()),release)
if plan=='already_active':
 batch(list(hosts),lambda h:remote(h,'status'),'status')
 report['phase']='already_active_verified';save()
 print('This release is already active; services were not restarted.',flush=True)
 sys.exit(0)
if plan=='recovery_required':
 report['phase']='recovery_required';save()
 raise RuntimeError('A partial/prior attempt exists for this release. Inspect backups before activation; no services were changed.')
assert_room_idle()
try:
 report['phase']='stopping';report['stop']={};save()
 systemctl('dortmund-control','stop')
 batch(slaves,lambda h:systemctl(h,'stop'),'stop')
 report['phase']='activating';save()
 batch(slaves,lambda h:remote(h,'activate'),'activate')
 report['activate']['dortmund-control']=remote('dortmund-control','activate');save()
 report['start']={}
 batch(slaves,lambda h:systemctl(h,'start'),'start')
 systemctl('dortmund-control','start')
 report['phase']='verifying';save()
 deadline=time.monotonic()+60
 while True:
  try:
   diagnostics=api('/api/diagnostics');health=api('/api/health')
   expected=set(slaves);info=diagnostics.get('node_info',{});hb=diagnostics.get('heartbeats',{})
   ready=set(diagnostics['nodes_up'])==expected and all(info.get(n,{}).get('protocol')==2 and 'per_node_video_v1' in info.get(n,{}).get('capabilities',[]) and hb.get(n,{}).get('fb_ok') and hb[n].get('graphics',{}).get('output_width')==1280 and hb[n].get('graphics',{}).get('output_height')==800 and not hb[n].get('display_error') and not hb[n].get('playback_error') for n in slaves)
   if ready and not health['playing']:break
  except Exception as exc:last_error=str(exc)
  if time.monotonic()>=deadline:raise RuntimeError('Fleet did not become ready: '+str(locals().get('last_error','check heartbeat diagnostics')))
  time.sleep(1)
 (report_path.parent/'runtime-production-diagnostics.json').write_text(json.dumps(diagnostics,indent=2)+'\n')
 batch(list(hosts),lambda h:remote(h,'status'),'status')
 for host,item in report['status'].items():
  state=dict(line.split('=',1) for line in item['service'].splitlines())
  if state.get('ActiveState')!='active' or state.get('NRestarts')!='0':raise RuntimeError('Unstable service: '+host)
 report['phase']='active_idle_verified';report['health']={k:health.get(k) for k in ('nodes_up','playing','media','version')};save()
 print(json.dumps({'phase':report['phase'],'release':release,'nodes':len(slaves),'media':health['media'],'playing':health['playing']}),flush=True)
except BaseException as error:
 report['errors'].append(str(error));report['phase']='rolling_back';save()
 print('Activation failed; restoring the previous runtime: '+str(error),flush=True)
 # Inspect remote snapshots before rolling back: interrupted activation is not
 # blindly retried, and untouched hosts simply restart their previous service.
 rollback_failed=[]
 for host in list(hosts):
  try:
   probe="from pathlib import Path;print(int(Path('/var/backups/immersive-release-"+release+"/restore.json').exists()))"
   exists=ssh(host,'python3 -c '+shlex.quote(probe)).strip()=='1'
   value=remote(host,'rollback') if exists else systemctl(host,'start')
   report.setdefault('rollback',{})[host]=value
   print(json.dumps({'rollback_host':host,'result':value}),flush=True)
  except Exception as exc:
   rollback_failed.append(host);report['errors'].append(str(exc))
   print(json.dumps({'rollback_host':host,'error':str(exc)}),flush=True)
  save()
 report['phase']='rollback_incomplete' if rollback_failed else 'rolled_back';save()
 raise
