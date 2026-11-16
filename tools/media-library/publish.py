#!/usr/bin/env python3
"""Upload a complete native package, pre-cache node assets, then publish it.

Manifest is uploaded after all clips are present and hash-verified on every
node. Optionally replace a specific currently playing test; preserve other
operator choices. No calibration, services, or power settings are changed.
"""
import argparse,asyncio,concurrent.futures,hashlib,http.client,json,shlex,subprocess,time
from pathlib import Path
from urllib.parse import quote
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from shared.media_package import load_plan


def upload(host,path):
    connection=http.client.HTTPConnection(host,8080,timeout=180,blocksize=1024*1024)
    try:
        with path.open('rb') as stream:
            connection.request('POST','/api/upload?name='+quote(path.name),body=stream,
                headers={'Content-Type':'application/octet-stream','Content-Length':str(path.stat().st_size)})
            response=connection.getresponse();reply=json.loads(response.read())
        if response.status!=200 or reply.get('uploaded')!=path.name:raise RuntimeError(str(reply))
    finally:connection.close()


def stage(host,address,master,asset,password):
    code='expected='+repr(host)+'\nasset='+repr(asset)+'\nmaster='+repr(master)+'\n'+'''import socket,json,hashlib,urllib.request,urllib.parse,shutil
from pathlib import Path
assert socket.gethostname()==expected,'Hostname mismatch'
cfg=json.loads(Path('/run/immersive/config.json').read_text());directory=Path(cfg['media_dir']);directory.mkdir(parents=True,exist_ok=True)
name=asset['file'];assert Path(name).name==name
path=directory/name
if path.exists() and hashlib.file_digest(path.open('rb'),'sha256').hexdigest()==asset['sha256']:
 print(json.dumps({'host':expected,'cached':True,'sha256':asset['sha256']}))
else:
 partial=path.with_name(path.name+'.precache-part')
 with urllib.request.urlopen('http://'+master+':8080/media/'+urllib.parse.quote(name),timeout=120) as response:
  size=int(response.headers['Content-Length'])
  assert shutil.disk_usage(directory).free>size+256*1024*1024,'Insufficient media storage'
  with partial.open('wb') as target:shutil.copyfileobj(response,target,1024*1024)
 assert hashlib.file_digest(partial.open('rb'),'sha256').hexdigest()==asset['sha256'],'Media checksum mismatch'
 partial.replace(path)
 print(json.dumps({'host':expected,'cached':True,'sha256':asset['sha256'],'bytes':size}))
'''
    command=['/opt/homebrew/bin/sshpass','-f',str(password),'ssh','-o','ControlMaster=no','-o','NumberOfPasswordPrompts=1',
        '-o','PubkeyAuthentication=no','-o','PreferredAuthentications=password','-o','ConnectTimeout=8','root@'+address,'python3 -']
    for attempt in range(3):
        result=subprocess.run(command,input=code,text=True,capture_output=True,timeout=240)
        if result.returncode==0:return json.loads(result.stdout.strip().splitlines()[-1])
        if result.returncode==255 and 'Permission denied' in result.stderr and attempt<2:time.sleep(1);continue
        raise RuntimeError(host+': '+result.stderr[-1500:]+' '+result.stdout[-1000:])


async def play_if_current(host,old,new,expected):
    import websockets
    report={'requested':new,'switched':False,'progress':[],'events':[]}
    async with websockets.connect('ws://'+host+':8080/ws',max_size=8*1024*1024) as ws:
        await ws.send(json.dumps({'role':'web','hello':'native-package-publisher'}))
        async def wait(predicate,timeout=180):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                message=json.loads(await asyncio.wait_for(ws.recv(),deadline-time.monotonic()))
                if message.get('type')=='error':raise RuntimeError(message.get('message','Playback failed'))
                if message.get('type') in ('show','prepare_progress'):report['events'].append(message)
                if predicate(message):return message
            raise RuntimeError('Room response timed out')
        snapshot=await wait(lambda m:m.get('type')=='snapshot',10)
        before=snapshot['show'];report['before']=before
        if not before.get('playing') or before.get('media')!=old:
            report['reason']=('Previous playback failed: '+str(before.get('error')) if before.get('error') and before.get('state')=='error' else 'Operator changed or stopped playback; new package is available without switching.');return report
        if set(snapshot['nodes_up'])!=set(expected):raise RuntimeError('Missing render nodes')
        await ws.send(json.dumps({'cmd':'capture_catalog','request_id':'publish-idle'}))
        catalog=await wait(lambda m:m.get('request_id')=='publish-idle',10)
        if catalog.get('busy') is not False:raise RuntimeError('Phone scan is active or unavailable')
        await ws.send(json.dumps({'cmd':'stop'}));await wait(lambda m:m.get('type')=='show' and not m['show'].get('playing'),15)
        await ws.send(json.dumps({'cmd':'play_media','media':new}))
        await wait(lambda m:m.get('type')=='show' and m['show'].get('playing') and m['show'].get('media')==new)
        report['switched']=True;deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            try:message=json.loads(await asyncio.wait_for(ws.recv(),max(.1,deadline-time.monotonic())))
            except asyncio.TimeoutError:break
            if message.get('type')=='error':raise RuntimeError(message.get('message','Playback failed'))
            if message.get('type')=='frame_status':report['progress'].append(message)
        if not report['progress'] or set(report['progress'][-1].get('presented',{}))!=set(expected):raise RuntimeError('Not all nodes presented the new video')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('manifest',type=Path);p.add_argument('--addresses',type=Path,required=True);p.add_argument('--password-file',type=Path,required=True);p.add_argument('--report',type=Path,required=True);p.add_argument('--replace-current');a=p.parse_args()
    package=json.loads(a.manifest.read_text());nodes=package['nodes'];load_plan(a.manifest.parent,a.manifest.name,list(nodes))
    addresses=json.loads(a.addresses.read_text());master=addresses['dortmund-control']
    report={'manifest':a.manifest.name,'phase':'uploading_clips','uploaded':[],'precache':{},'errors':[]}
    def save():a.report.write_text(json.dumps(report,indent=2)+'\n')
    save()
    try:
        for asset in nodes.values():
            path=a.manifest.parent/asset['file'];upload(master,path);report['uploaded'].append(path.name);save()
            print(json.dumps({'uploaded_clips':len(report['uploaded']),'total':len(nodes)}),flush=True)
        report['phase']='precaching_nodes';save()
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures={pool.submit(stage,node,addresses[node],master,asset,a.password_file):node for node,asset in nodes.items()}
            for future in concurrent.futures.as_completed(futures):
                node=futures[future];report['precache'][node]=future.result();save();print(json.dumps({'cached_node':node}),flush=True)
        upload(master,a.manifest);report['phase']='published';save()
        if a.replace_current:
            report['playback']=asyncio.run(play_if_current(master,a.replace_current,a.manifest.name,list(nodes)))
            report['phase']='playing_verified' if report['playback']['switched'] else 'published_operator_state_preserved';save()
        print(json.dumps({'phase':report['phase'],'nodes':len(nodes)}),flush=True)
    except Exception as exc:
        report['phase']='failed';report['errors'].append(str(exc));save();raise


if __name__=='__main__':main()
