import hashlib
import json
from pathlib import Path
import sys
import shutil
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from verify_image import Ext4, verify


class Image:
    def __init__(self):
        self.files = {
            '/etc/immersive-version': b'pi-v1.0.8\n',
            '/usr/bin/immersive-role.sh': b'systemctl start --no-block immersive-render.service\n',
            '/root/.ssh/authorized_keys': b'operator-public-key\n',
        }
        self.directories = {'/root/.ssh'}

    def exists(self, path):
        return path in self.files or path in self.directories

    def read(self, path):
        if not self.exists(path):
            raise ValueError('Image file missing: ' + path)
        return self.files[path]

    def command(self, command):
        path = command.removeprefix('stat ')
        return b'Inode: 10 Type: directory Mode: 0700' if path in self.directories else b'Inode: 11 Type: regular Mode: 0600'


@pytest.fixture
def image():
    root = Image()
    hashes = {'system/immersive-role.sh': hashlib.sha256(root.files['/usr/bin/immersive-role.sh']).hexdigest()}
    release = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16]
    root.files['/opt/immersive/runtime-manifest.json'] = json.dumps({'release': release, 'files': hashes}).encode()
    for name in ('video4linux2', 'videoparsersbad', 'isomp4', 'app', 'coreelements'):
        root.files['/usr/lib/gstreamer-1.0/libgst' + name + '.so'] = b'\x7fELF' + bytes(14) + (183).to_bytes(2, 'little')
    for module in ('EGL', 'GLES2'):
        root.files['/usr/lib/python3.12/site-packages/OpenGL/' + module + '/__init__.py'] = b''
    for namespace in ('Gst', 'GstApp', 'GstVideo', 'GstNet'):
        root.files['/usr/lib/girepository-1.0/' + namespace + '-1.0.typelib'] = b'introspection-data'
    boot = {'config.txt': b'dtoverlay=rpi-poe\n', 'cmdline.txt': b'root=/dev/mmcblk0p2 cma=512M quiet\n',
            'immersive.conf': b'role=render\nnode=pi-01\n'}
    return root, boot, release


def test_image_provenance_boot_and_decoder_requirements(image):
    root, boot, release = image
    result = verify(root, boot, release, 'pi-v1.0.8', b'operator-public-key\n')
    assert result['decoder_plugins_aarch64'] and result['cma_mb'] == 512


@pytest.mark.parametrize('fault', ['stale', 'changed-file', 'missing-decoder', 'wrong-architecture',
                                    'missing-cma', 'missing-poe', 'private-calibration', 'wrong-key', 'missing-clock-typelib'])
def test_image_rejects_incomplete_or_stale_artifacts(image, fault):
    root, boot, release = image
    if fault == 'stale': release = 'newer-runtime'
    if fault == 'changed-file': root.files['/usr/bin/immersive-role.sh'] += b'changed'
    if fault == 'missing-decoder': del root.files['/usr/lib/gstreamer-1.0/libgstvideo4linux2.so']
    if fault == 'wrong-architecture': root.files['/usr/lib/gstreamer-1.0/libgstvideo4linux2.so'] = b'\x7fELF' + bytes(14) + (62).to_bytes(2, 'little')
    if fault == 'missing-cma': boot['cmdline.txt'] = b'quiet'
    if fault == 'missing-poe': boot['config.txt'] = b''
    if fault == 'private-calibration': root.files['/opt/immersive/control-node/room-model.json'] = b'private'
    if fault == 'wrong-key': root.files['/root/.ssh/authorized_keys'] = b'another-key'
    if fault == 'missing-clock-typelib': del root.files['/usr/lib/girepository-1.0/GstNet-1.0.typelib']
    with pytest.raises((AssertionError, ValueError)):
        verify(root, boot, release, 'pi-v1.0.8', b'operator-public-key\n')


@pytest.mark.skipif(not shutil.which('debugfs') or not shutil.which('mke2fs'),
                    reason='ext4 inspection tools unavailable')
def test_ext4_reader_uses_real_image_without_mounting(tmp_path):
    tree = tmp_path / 'tree'
    (tree / 'etc').mkdir(parents=True)
    (tree / 'etc/version').write_bytes(b'example-version\n')
    (tree / 'etc/empty').touch()
    image = tmp_path / 'rootfs.ext4'
    with image.open('wb') as stream:
        stream.truncate(8 * 1024 * 1024)
    subprocess.run(['mke2fs', '-q', '-F', '-t', 'ext4', '-d', str(tree), str(image)], check=True)
    before = hashlib.sha256(image.read_bytes()).digest()
    reader = Ext4(image)
    assert reader.read('/etc/version') == b'example-version\n'
    assert reader.read('/etc/empty') == b''
    assert not reader.exists('/etc/missing')
    assert hashlib.sha256(image.read_bytes()).digest() == before
