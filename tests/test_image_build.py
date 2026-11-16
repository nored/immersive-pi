import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from stage_image_app import stage


def test_yocto_staging_excludes_live_data_and_old_generated_files(tmp_path):
    repo=tmp_path/'repo'
    for name in ('render-node/agent.py','control-node/controller.py','shared/__init__.py',
                 'control-node/room-model.json','render-node/config.json','control-node/media/private.mp4'):
        path=repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('private' if path.suffix!='.py' else 'pass\n')
    system=repo/'yocto/recipes-immersive/immersive/files';system.mkdir(parents=True)
    for name in ('immersive-config.sh','immersive-role.sh','immersive-render.service','immersive-control.service'):(system/name).write_text('example')
    target=tmp_path/'generated';(target/'control-node').mkdir(parents=True)
    (target/'control-node/room-model.json').write_text('stale calibration')
    stage(repo,target)
    assert (target/'render-node/agent.py').exists()
    assert (target/'shared/__init__.py').exists()
    assert (target/'runtime-manifest.json').exists()
    assert not (target/'control-node/room-model.json').exists()
    assert not (target/'render-node/config.json').exists()
    assert not (target/'control-node/media').exists()


@pytest.mark.parametrize('existing', ['boot','fallback','missing'])
def test_firmware_update_preserves_identity_and_refuses_missing_config(tmp_path,existing):
    staged=tmp_path/'staged';staged.mkdir()
    boot=tmp_path/'boot';boot.mkdir()
    fallback=tmp_path/'fallback.conf'
    (staged/'immersive.conf').write_text('role=render\nnode=pi-01\n')
    (staged/'firmware.bin').write_text('new firmware')
    (boot/'firmware.bin').write_text('old firmware')
    identity='role=control\nnode=dortmund-control\napi_token=private-value\n'
    if existing=='boot':(boot/'immersive.conf').write_text(identity)
    if existing=='fallback':fallback.write_text(identity)
    helper=ROOT/'yocto/recipes-immersive/immersive-updater/files/immersive-install-boot'
    result=subprocess.run(['sh',str(helper),str(staged),str(boot),str(fallback)],capture_output=True,text=True)
    if existing=='missing':
        assert result.returncode!=0
        assert (boot/'firmware.bin').read_text()=='old firmware'
    else:
        assert result.returncode==0,result.stderr
        assert (boot/'immersive.conf').read_text()==identity
        assert (boot/'firmware.bin').read_text()=='new firmware'


def load_helper(name):
    from importlib.machinery import SourceFileLoader
    path=ROOT/'yocto/recipes-immersive/immersive-updater/files'/name
    spec=importlib.util.spec_from_loader(name,SourceFileLoader(name,str(path)))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_carries_room_media_credentials_and_pending_markers_to_new_slot(tmp_path):
    helper=load_helper('immersive-preserve-state')
    old,new=tmp_path/'old',tmp_path/'new'
    data={'opt/immersive/control-node/room-model.json':'saved calibration',
          'opt/immersive/control-node/.git/HEAD':'saved history',
          'opt/immersive/control-node/media/room.mp4':'uploaded video',
          'opt/immersive/media/tile.mp4':'cached tile',
          'root/.ssh/authorized_keys':'device public key',
          'etc/dropbear/dropbear_ed25519_host_key':'device host key',
          'etc/default/dropbear':'existing login policy',
          'etc/shadow':'root:existing-root-record:1:2:3:4:5:6:7\nold-daemon:old\n',
          'opt/immersive/control-node/controller.py':'old code'}
    for name,body in data.items():
        p=old/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(body)
    (new/'etc').mkdir(parents=True)
    (new/'etc/shadow').write_text('root:locked:0:0:0:0:0:0:0\nnew-daemon:new\n')
    (new/'etc/shadow').chmod(0o600)
    (new/'opt/immersive/control-node').mkdir(parents=True)
    (new/'opt/immersive/control-node/controller.py').write_text('new code')
    helper.preserve(old,new,'pi-v1.0.8','/dev/mmcblk0p3')
    for name,body in data.items():
        if name in ('etc/shadow','opt/immersive/control-node/controller.py'):continue
        assert (new/name).read_text()==body
    assert (new/'opt/immersive/control-node/controller.py').read_text()=='new code'
    shadow=(new/'etc/shadow').read_text()
    assert shadow.startswith('root:existing-root-record:')
    assert 'new-daemon:new' in shadow and 'old-daemon:old' not in shadow
    assert (new/'etc/shadow').stat().st_mode & 0o777 == 0o600
    pending=new/'var/lib/immersive-updater'
    assert (pending/'pending-version').read_text()=='pi-v1.0.8\n'
    assert (pending/'pending-root').read_text()=='/dev/mmcblk0p3\n'
    assert not (old/'var/lib/immersive-updater/pending-version').exists()
    with pytest.raises(ValueError):helper.preserve(old,old,'pi-v1.0.8','/dev/mmcblk0p3')


def test_boot_health_accepts_idle_decoder_but_rejects_missing_hardware_or_bad_display():
    helper=load_helper('immersive-health-check')
    status={'role':'render','version':'pi-v1.0.8','fb_ok':True,'decoder_ok':False,'display_error':None}
    assert helper.healthy('render',status,True,'pi-v1.0.8')
    assert not helper.healthy('render',status,False,'pi-v1.0.8')
    assert not helper.healthy('render',{**status,'display_error':'EGL failed'},True,'pi-v1.0.8')
    assert not helper.healthy('render',{**status,'fb_ok':False},True,'pi-v1.0.8')
    assert not helper.healthy('render',status,True,'pi-v1.0.9')
    assert helper.healthy('control',{'role':'control','version':'pi-v1.0.8'},False,'pi-v1.0.8')
