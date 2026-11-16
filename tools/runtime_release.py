#!/usr/bin/env python3
"""Build, stage, activate and roll back one versioned immersive runtime release.

Staging is read-only with respect to the active runtime. Activation requires the
coordinator to stop the role service first. No command starts media or switches
projectors. Room geometry, media, boot config and image version are preserved.
"""
import argparse
import hashlib
import gzip
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tarfile
import time

ROOT = Path('/opt/immersive')
RELEASES = Path('/opt/immersive-releases')
BACKUPS = Path('/var/backups')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(repository, destination):
    files = {}
    for folder in ('render-node', 'control-node', 'shared'):
        for path in sorted((repository/folder).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix in ('.py', '.html', '.js', '.css', '.vert', '.frag'):
                files['app/'+path.relative_to(repository).as_posix()] = path.read_bytes()
    system = repository/'yocto/recipes-immersive/immersive/files'
    for name in ('immersive-config.sh', 'immersive-role.sh', 'immersive-render.service', 'immersive-control.service'):
        files['system/'+name] = (system/name).read_bytes()
    hashes = {name:digest(data) for name,data in files.items()}
    release = digest(json.dumps(hashes,sort_keys=True).encode())[:16]
    manifest = {'release':release, 'protocol':2, 'output_mode':'1280x800@60', 'live_previews':False, 'files':hashes}
    files['manifest.json'] = (json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('wb') as output, gzip.GzipFile(fileobj=output,mode='wb',filename='',mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as archive:
            for name,data in sorted(files.items()):
                entry=tarfile.TarInfo(name);entry.size=len(data);entry.mode=0o644
                archive.addfile(entry,io.BytesIO(data))
    return {'release':release,'archive':str(destination),'sha256':digest(destination.read_bytes()),'files':len(hashes)}


def classify_activation(probes, release):
    """Never turn a duplicate or partial rollout into an unintended downgrade."""
    if not probes:
        raise ValueError('No deployment targets')
    if all(p.get('installed_release') == release for p in probes):
        return 'already_active'
    if any(p.get('snapshot_exists') or p.get('installed_release') == release for p in probes):
        return 'recovery_required'
    return 'new'


def host_role(expected):
    if socket.gethostname() != expected:
        raise RuntimeError('SSH target hostname does not match deployment plan')
    role = 'control' if expected == 'dortmund-control' else 'render'
    if Path('/run/immersive/role').read_text().strip() != role:
        raise RuntimeError('Boot role does not match deployment plan')
    return role


def stage(archive, expected):
    role=host_role(expected)
    with tarfile.open(archive) as source:
        manifest=json.load(source.extractfile('manifest.json'))
        release=manifest['release']
        if not release.isalnum() or len(release)!=16:
            raise ValueError('Invalid release id')
        if release != digest(json.dumps(manifest['files'],sort_keys=True).encode())[:16]:
            raise ValueError('Release id does not match its manifest')
        target=RELEASES/release
        target.mkdir(parents=True,exist_ok=True)
        for name,sha in manifest['files'].items():
            relative=Path(name)
            if relative.is_absolute() or '..' in relative.parts:
                raise ValueError('Unsafe release path')
            data=source.extractfile(name).read()
            if digest(data)!=sha:
                raise ValueError('Release content hash mismatch: '+name)
            if name.endswith('.py'):compile(data,name,'exec')
            path=target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        (target/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    candidate='immersive-candidate-'+('control' if role=='control' else 'render')+'.service'
    if subprocess.run(['systemctl','is-active','--quiet',candidate]).returncode==0:
        raise RuntimeError('A trial runtime is still active')
    app=target/'app'
    env={**os.environ,'PYOPENGL_PLATFORM':'egl','PYTHONPATH':':'.join(str(app/p) for p in ('render-node','control-node','.'))}
    modules='import controller,roommodel,room_devices,start_operation' if role=='control' else 'import agent,player,gl_pipeline,presenter,encoded_stream'
    result=subprocess.run(['python3','-c',modules],env=env,capture_output=True,text=True,timeout=20)
    if result.returncode:raise RuntimeError('Staged imports failed: '+result.stderr[-4000:])
    boot=next((p for p in (Path('/boot/firmware/immersive.conf'),Path('/boot/immersive.conf'),Path('/etc/immersive.conf')) if p.is_file()),None)
    config=dict(line.split('=',1) for line in boot.read_text().splitlines() if '=' in line and not line.startswith('#')) if boot else {}
    if role=='render' and config.get('display_mode')!='1280x800@60':
        raise RuntimeError('Persisted output mode is not 1280x800@60')
    if config.get('hostname',expected)!=expected or config.get('role','render')!=role:
        raise RuntimeError('Persisted hostname/role does not match the deployment plan')
    if config.get('node',expected)!=expected:
        raise RuntimeError('Configured node identity does not match hostname')
    if role=='control':
        room=json.loads((ROOT/'control-node/room-model.json').read_text())
        media='native-ring-1280x800.immersive.json'
        # Run validation against the staged source, preserving live room data.
        check="from roommodel import RoomModel;from shared.calibration import validate_field;from shared.media_package import load_plan;r=RoomModel('"+str(ROOT/'control-node/room-model.json')+"');[(validate_field(f,n[f])) for n in r.model['nodes'] for f in ('mesh','source_region','color','blend')];p=load_plan('"+str(ROOT/'control-node/media')+"','"+media+"',r.nodes());assert all((a['width'],a['height'])==(1280,800) for a in p['nodes'].values());import hashlib;from pathlib import Path;assert all(hashlib.file_digest((Path('/opt/immersive/control-node/media')/a['file']).open('rb'),'sha256').hexdigest()==a['sha256'] for a in p['nodes'].values());assert len(r.nodes())==12"
        result=subprocess.run(['python3','-c',check],env=env,capture_output=True,text=True,timeout=20)
        if result.returncode:raise RuntimeError('Room/media validation failed: '+result.stderr[-4000:])
    return {'host':expected,'role':role,'release':release,'staged':True}


def targets(release,role):
    root=RELEASES/release
    manifest=json.loads((root/'manifest.json').read_text())
    result={}
    for name,sha in manifest['files'].items():
        if name.startswith('app/shared/') or name.startswith('app/'+('control-node/' if role=='control' else 'render-node/')):
            result[name]=ROOT/name[len('app/'):]
        elif name in ('system/immersive-config.sh','system/immersive-role.sh'):
            result[name]=Path('/usr/bin')/Path(name).name
        elif name=='system/immersive-'+role+'.service':
            result[name]=Path('/etc/systemd/system')/Path(name).name
    return root,manifest,result


def activate(release,expected):
    role=host_role(expected);root,manifest,mapping=targets(release,role)
    unit='immersive-'+role+'.service'
    if subprocess.run(['systemctl','is-active','--quiet',unit]).returncode==0:
        raise RuntimeError('Stop the role service before activating a release')
    backup=BACKUPS/('immersive-release-'+release)
    if (backup/'restore.json').exists():
        raise RuntimeError('This release already has a rollback snapshot; do not overwrite it')
    backup.mkdir(parents=True,exist_ok=True,mode=0o700)
    extra=[ROOT/'runtime-manifest.json']
    if role=='control':extra.append(ROOT/'control-node/room-model.json')
    previous={}
    for destination in list(mapping.values())+extra:
        name=str(destination);previous[name]=destination.exists()
        if destination.exists():
            saved=backup/name.lstrip('/');saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,saved)
    (backup/'restore.json').write_text(json.dumps(previous,indent=2))
    try:
        for name,destination in mapping.items():
            data=(root/name).read_bytes()
            if digest(data)!=manifest['files'][name]:raise ValueError('Staged release changed')
            destination.parent.mkdir(parents=True,exist_ok=True)
            temporary=destination.with_name(destination.name+'.release-new');temporary.write_bytes(data)
            temporary.chmod(0o755 if str(destination).startswith('/usr/bin/') else 0o644);temporary.replace(destination)
        (ROOT/'runtime-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        if role=='control':
            path=ROOT/'control-node/room-model.json';room=json.loads(path.read_text())
            room['media']='native-ring-1280x800.immersive.json'
            temporary=path.with_name(path.name+'.release-new');temporary.write_text(json.dumps(room,indent=2,allow_nan=False)+'\n');temporary.replace(path)
        subprocess.run(['systemctl','daemon-reload'],check=True)
        subprocess.run(['/usr/bin/immersive-config.sh'],check=True,capture_output=True)
    except BaseException:
        rollback(release,expected)
        raise
    return {'host':expected,'role':role,'release':release,'activated':True,'backup':str(backup)}


def rollback(release,expected):
    role=host_role(expected);backup=BACKUPS/('immersive-release-'+release)
    previous=json.loads((backup/'restore.json').read_text())
    subprocess.run(['systemctl','stop','immersive-'+role+'.service'],check=True)
    for name,existed in previous.items():
        path=Path(name)
        if existed:shutil.copy2(backup/name.lstrip('/'),path)
        else:path.unlink(missing_ok=True)
    subprocess.run(['systemctl','daemon-reload'],check=True)
    subprocess.run(['/usr/bin/immersive-config.sh'],check=True,capture_output=True)
    subprocess.run(['systemctl','start','immersive-'+role+'.service'],check=True)
    return {'host':expected,'rolled_back':True}


def status(release,expected):
    role=host_role(expected);root,manifest,mapping=targets(release,role)
    mismatches=[str(dst) for src,dst in mapping.items() if not dst.is_file() or digest(dst.read_bytes())!=manifest['files'][src]]
    unit='immersive-'+role+'.service'
    state=subprocess.check_output(['systemctl','show',unit,'-p','ActiveState','-p','NRestarts'],text=True)
    result={'host':expected,'release':release,'mismatches':mismatches,'service':state.strip()}
    if role=='render':
        config=json.loads(Path('/run/immersive/config.json').read_text())
        result['configured_mode']=config.get('drm',{}).get('mode')
        result['drm_state']=Path('/sys/kernel/debug/dri/0/state').read_text()
        import re
        result['active_modes']=re.findall(r'mode: "(\d+x\d+)"',result.pop('drm_state'))
        if result['active_modes']!=['1280x800']:raise RuntimeError('Unexpected active scanout mode: '+str(result['active_modes']))
    if mismatches:raise RuntimeError('Installed release hashes do not match')
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('build');p.add_argument('--repository',type=Path,default=Path.cwd());p.add_argument('--out',type=Path,required=True)
    p=sub.add_parser('stage');p.add_argument('archive',type=Path);p.add_argument('--host',required=True)
    for command in ('activate','rollback','status'):
        p=sub.add_parser(command);p.add_argument('release');p.add_argument('--host',required=True)
    args=parser.parse_args()
    if args.command=='build':result=build(args.repository,args.out)
    elif args.command=='stage':result=stage(args.archive,args.host)
    else:result=globals()[args.command](args.release,args.host)
    print(json.dumps(result),flush=True)


if __name__=='__main__':main()
