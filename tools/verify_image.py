#!/usr/bin/env python3
"""Inspect an ext4 rootfs and boot tar without mounting or modifying either."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile


class Ext4:
    def __init__(self, image):
        self.image = image

    def command(self, command):
        result = subprocess.run(['debugfs', '-R', command, str(self.image)],
                                capture_output=True, check=True)
        return result.stdout

    def exists(self, path):
        return b'Inode:' in self.command('stat ' + path)

    def read(self, path):
        if not self.exists(path):
            raise ValueError('Image file missing: ' + path)
        return self.command('cat ' + path)


def verify(root, boot, release, version, public_key=None):
    manifest = json.loads(root.read('/opt/immersive/runtime-manifest.json'))
    if manifest['release'] != release:
        raise ValueError('Image contains stale runtime ' + manifest['release'] + '; expected ' + release)
    actual_release = hashlib.sha256(json.dumps(manifest['files'], sort_keys=True).encode()).hexdigest()[:16]
    assert actual_release == release, 'Runtime manifest release does not match its file hashes'
    assert root.read('/etc/immersive-version').decode().strip() == version, 'Wrong image version'
    checked = 0
    for name, expected in manifest['files'].items():
        if name.startswith('app/'):
            candidates = ['/opt/immersive/' + name[4:]]
        elif name.startswith('system/'):
            leaf = Path(name).name
            candidates = (['/usr/bin/' + leaf] if leaf.endswith('.sh') else
                          ['/usr/lib/systemd/system/' + leaf, '/lib/systemd/system/' + leaf])
        else:
            raise ValueError('Unknown manifest path: ' + name)
        path = next((p for p in candidates if root.exists(p)), None)
        assert path is not None, 'Runtime file missing: ' + name
        assert hashlib.sha256(root.read(path)).hexdigest() == expected, 'Runtime hash mismatch: ' + name
        checked += 1
    assert checked > 0, 'Empty runtime manifest'
    assert b'--no-block' in root.read('/usr/bin/immersive-role.sh'), 'Blocking role activation'
    assert not root.exists('/opt/immersive/control-node/room-model.json'), 'Embedded room calibration'
    assert not root.exists('/opt/immersive/render-node/config.json'), 'Embedded node identity'
    keys = root.read('/root/.ssh/authorized_keys')
    assert keys.strip(), 'Missing operator SSH public key'
    if public_key is not None:
        assert keys == public_key, 'Operator SSH key mismatch'
    for path in ('/root/.ssh', '/root/.ssh/authorized_keys'):
        mode = b'0700' if path.endswith('.ssh') else b'0600'
        assert re.search(rb'Mode:\s+' + mode + rb'\b', root.command('stat ' + path)), 'Unsafe SSH permissions'
    for plugin in ('video4linux2', 'videoparsersbad', 'isomp4', 'app', 'coreelements'):
        data = root.read('/usr/lib/gstreamer-1.0/libgst' + plugin + '.so')
        assert data[:4] == b'\x7fELF' and data[18:20] == (183).to_bytes(2, 'little'), 'Non-AArch64 plugin: ' + plugin
    for module in ('EGL', 'GLES2'):
        root.read('/usr/lib/python3.12/site-packages/OpenGL/' + module + '/__init__.py')
    for namespace in ('Gst', 'GstApp', 'GstVideo', 'GstNet'):
        assert root.read('/usr/lib/girepository-1.0/' + namespace + '-1.0.typelib'), 'Missing GStreamer introspection data: ' + namespace
    config = boot['config.txt'].decode()
    cmdline = boot['cmdline.txt'].decode()
    assert re.search(r'^dtoverlay=rpi-poe(?:-plus)?\s*$', config, re.M), 'PoE cooling overlay missing'
    assert re.search(r'(?:^|\s)cma=512M(?:\s|$)', cmdline), '512 MB CMA missing'
    assert boot['immersive.conf'].strip(), 'Boot provisioning template missing'
    return {'runtime_release': release, 'image_version': version, 'runtime_files_verified': checked,
            'decoder_plugins_aarch64': True, 'poe_configured': True, 'cma_mb': 512,
            'limitation': 'Static artifact inspection; spare-card boot and hardware playback remain required.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rootfs', type=Path, required=True)
    parser.add_argument('--boot', type=Path, required=True)
    parser.add_argument('--release', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--public-key', type=Path)
    args = parser.parse_args()
    with tarfile.open(args.boot) as archive:
        files = {m.name.removeprefix('./'): archive.extractfile(m).read()
                 for m in archive.getmembers() if m.isfile()
                 and m.name.removeprefix('./') in ('config.txt', 'cmdline.txt', 'immersive.conf')}
    result = verify(Ext4(args.rootfs), files, args.release, args.version,
                    args.public_key.read_bytes() if args.public_key else None)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
