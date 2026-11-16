#!/usr/bin/env python3
"""Run image binaries in a disposable extracted root, on an ARM64 Linux host.

Requires root in a container, debugfs, and an OpenSSH client. The input ext4
image is never mounted or modified. GPU, PoE and HDMI still require a real Pi.
"""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import shlex
import socket
import stat
import subprocess
import tempfile
import time

COMPONENT_TEST = '''
import json,sys,time
sys.path[:0]=['/opt/immersive/render-node','/opt/immersive/control-node']
import cv2,numpy,gi
import controller
import agent,drm_kms,presenter
from OpenGL import EGL,GLES2
from player import Player,Gst
assert bool(EGL.eglInitialize) and bool(GLES2.glCreateShader)
player=Player(decoder='avdec_h264',buffer_frames=3)
try:
 spec=player.prepare('/tmp/test.mp4')
 deadline=time.monotonic()+15
 for sequence in range(65):
  frame=None
  while frame is None and time.monotonic()<deadline:
   frame=player.take_frame(sequence)
   if frame is None: time.sleep(.001)
  assert frame is not None and frame.sequence==sequence,(sequence,player.error)
  assert len(frame.planes[0])==frame.width*frame.height
  assert player.error is None,player.error
finally:
 player.stop()
print(json.dumps({'python':sys.version.split()[0],'opencv':cv2.__version__,
 'numpy':numpy.__version__,'gstreamer':Gst.version_string(),
 'egl_gles_bindings':True,'controller_import':True,'renderer_import':True,'software_decoded_frames':65,
 'video_size':[spec['width'],spec['height']]}))
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs',type=Path,required=True)
    parser.add_argument('--media',type=Path,required=True)
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise SystemExit('Run as root inside a disposable ARM64 Linux container')
    with tempfile.TemporaryDirectory(prefix='immersive-image-smoke-') as temporary:
        work=Path(temporary);root=work/'root';root.mkdir()
        subprocess.run(['debugfs','-R','rdump / '+str(root),str(args.rootfs)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        (root/'tmp').mkdir(exist_ok=True)
        for name,minor in [('null',3),('random',8),('urandom',9)]:
            path=root/'dev'/name
            if not path.exists():os.mknod(path,stat.S_IFCHR|0o666,os.makedev(1,minor))
        shutil.copyfile(args.media,root/'tmp/test.mp4')
        (root/'tmp/components.py').write_text(COMPONENT_TEST)
        env={**os.environ,'PYOPENGL_PLATFORM':'egl'}
        components=subprocess.run(['chroot',str(root),'/usr/bin/python3','/tmp/components.py'],
                                  env=env,capture_output=True,text=True,timeout=60)
        if components.returncode:
            raise RuntimeError('Image component test failed: '+components.stderr+components.stdout)
        result=json.loads(components.stdout.strip().splitlines()[-1])
        client_key=work/'client-key'
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(client_key)],check=True)
        with (root/'root/.ssh/authorized_keys').open('ab') as output:
            output.write(b'\n'+client_key.with_suffix('.pub').read_bytes())
        keygen=next(p for p in ('/usr/sbin/dropbearkey','/usr/bin/dropbearkey') if (root/p.lstrip('/')).exists())
        subprocess.run(['chroot',str(root),keygen,'-t','ed25519','-f','/tmp/host-key'],
                       check=True,capture_output=True,timeout=15)
        policy=next(line.split('=',1)[1] for line in (root/'etc/default/dropbear').read_text().splitlines()
                    if line.startswith('DROPBEAR_EXTRA_ARGS='))
        options=shlex.split(shlex.split(policy)[0])
        with (work/'dropbear.log').open('w') as log:
            server=subprocess.Popen(['chroot',str(root),'/usr/sbin/dropbear','-F','-E',*options,
                                     '-p','127.0.0.1:22022','-r','/tmp/host-key'],stdout=log,stderr=log)
            try:
                deadline=time.monotonic()+10
                while True:
                    if server.poll() is not None:raise RuntimeError('Image SSH server exited')
                    try:
                        with socket.create_connection(('127.0.0.1',22022),timeout=.2):break
                    except OSError:
                        if time.monotonic()>deadline:raise
                        time.sleep(.1)
                client=subprocess.run(['ssh','-T','-p','22022','-i',str(client_key),
                    '-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new',
                    '-o','UserKnownHostsFile='+str(work/'known_hosts'),'root@127.0.0.1','id -u'],
                    capture_output=True,text=True,timeout=15)
                if client.returncode or client.stdout.strip()!='0':
                    raise RuntimeError('Image root public-key login failed: '+client.stderr)
                anonymous=subprocess.run(['ssh','-T','-p','22022','-o','PubkeyAuthentication=no',
                    '-o','PreferredAuthentications=none','-o','PasswordAuthentication=no',
                    '-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new',
                    '-o','UserKnownHostsFile='+str(work/'known_hosts'),'root@127.0.0.1','id -u'],
                    capture_output=True,text=True,timeout=15)
                assert anonymous.returncode==255 and 'Permission denied' in anonymous.stderr, 'Anonymous SSH check did not prove authentication rejection'
            except Exception:
                log.flush()
                raise RuntimeError((work/'dropbear.log').read_text())
            finally:
                server.terminate()
                server.wait(timeout=10)
        # Exercise the same label rewrite as first-boot resize, on a disposable
        # regular file. A changed MBR identifier would invalidate PARTUUIDs.
        disk=root/'tmp/layout.img'
        with disk.open('wb') as output:output.truncate(64*1024*1024)
        sfdisk=next(p for p in ('/usr/sbin/sfdisk','/sbin/sfdisk') if (root/p.lstrip('/')).exists())
        prefix=['chroot',str(root),sfdisk,'--no-reread','/tmp/layout.img']
        initial='label: dos\nlabel-id: 0x1234abcd\nunit: sectors\n\nstart=8192,size=8192,type=c,bootable\nstart=16384,size=16384,type=83\nstart=32768,size=16384,type=83\nstart=49152,size=2048,type=83\n'
        subprocess.run(prefix,input=initial,text=True,capture_output=True,check=True,timeout=15)
        helper=(root/'usr/bin/immersive-resize.sh').read_text()
        table=re.search(r'sfdisk --no-reread.*?<<EOF\n(.*?)\nEOF',helper,re.S).group(1)
        # Substitute only sysfs reads; execute the installed helper's real table
        # template and arithmetic with the disposable disk's test geometry.
        table=table.replace('$(cat /sys/class/block/mmcblk0p1/start)','$BOOT_START')
        table=table.replace('$(cat /sys/class/block/mmcblk0p1/size)','$BOOT_SIZE')
        disk_id=subprocess.run(['chroot',str(root),sfdisk,'--disk-id','/tmp/layout.img'],
                               capture_output=True,text=True,check=True).stdout.strip()
        test_env={**os.environ,'DISK_ID':disk_id,'BOOT_START':'8192','BOOT_SIZE':'8192',
                  'P2_START':'16384','P2_END_NEW':'49151','P3_START_NEW':'49152','P3_END_NEW':'81919'}
        rendered=subprocess.run(['chroot',str(root),'/bin/sh','-c','cat <<EOF\n'+table+'\nEOF'],
                                env=test_env,capture_output=True,text=True,check=True).stdout
        subprocess.run(prefix,input=rendered,text=True,capture_output=True,check=True,timeout=15)
        with disk.open('rb') as source:source.seek(440);identifier=source.read(4)
        assert identifier==(0x1234abcd).to_bytes(4,'little'), 'First-boot partition rewrite changes the MBR identifier'
        result['partition_rewrite_preserves_disk_id']=True
        result['root_public_key_login']='passed with disposable test key; operator seed checked separately'
        result['anonymous_root_login']='rejected'
        result['limitation']='Container smoke test only; no real boot, hardware decode, HDMI or PoE validation.'
        print(json.dumps(result,indent=2))


if __name__=='__main__':main()
