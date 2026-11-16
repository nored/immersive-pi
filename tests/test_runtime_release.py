import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tarfile

import pytest

spec = importlib.util.spec_from_file_location('runtime_release', Path(__file__).resolve().parents[1]/'tools/runtime_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_contains_only_runtime_and_changes_identity_with_code(tmp_path):
    repo=tmp_path/'repo'
    for name,body in {'render-node/agent.py':'value = 1\n','shared/__init__.py':'',
                      'control-node/web/app.js':'const test = 1;',
                      'control-node/room-model.json':'private calibration',
                      'render-node/config.json':'private config',
                      'control-node/media/private.mp4':'private media'}.items():
        path=repo/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body)
    system=repo/'yocto/recipes-immersive/immersive/files';system.mkdir(parents=True)
    for name in ('immersive-config.sh','immersive-role.sh','immersive-render.service','immersive-control.service'):
        (system/name).write_text('example')
    archive=tmp_path/'release.tar.gz'
    first=release.build(repo,archive)
    second=release.build(repo,tmp_path/'different-name.tar.gz')
    assert second['sha256']==first['sha256']
    with tarfile.open(archive) as source:
        names=source.getnames()
        assert 'app/control-node/web/app.js' in names
        assert not any('private' in n or n.endswith('config.json') or n.endswith('room-model.json') for n in names)
    (repo/'render-node/config.json').write_text('different private config')
    assert release.build(repo,archive)['release']==first['release']
    (repo/'render-node/agent.py').write_text('value = 2\n')
    assert release.build(repo,archive)['release']!=first['release']


def test_failed_activation_restores_existing_files_and_removes_new_files(tmp_path,monkeypatch):
    active=tmp_path/'active';active.mkdir()
    (active/'agent.py').write_text('old runtime')
    (active/'runtime-manifest.json').write_text('old manifest')
    staged=tmp_path/'staged';(staged/'app').mkdir(parents=True)
    (staged/'app/agent.py').write_text('new runtime')
    (staged/'app/new.py').write_text('new module')
    manifest={'files':{n:hashlib.sha256((staged/n).read_bytes()).hexdigest() for n in ('app/agent.py','app/new.py')}}
    mapping={'app/agent.py':active/'agent.py','app/new.py':active/'new.py'}
    monkeypatch.setattr(release,'ROOT',active)
    monkeypatch.setattr(release,'BACKUPS',tmp_path/'backups')
    monkeypatch.setattr(release,'host_role',lambda expected:'render')
    monkeypatch.setattr(release,'targets',lambda identifier,role:(staged,manifest,mapping))
    calls=[]
    failed=False
    def run(command,**kwargs):
        nonlocal failed
        calls.append(command)
        if command==['/usr/bin/immersive-config.sh'] and not failed:
            failed=True
            raise RuntimeError('configuration failed')
        return SimpleNamespace(returncode=3 if 'is-active' in command else 0)
    monkeypatch.setattr(release.subprocess,'run',run)
    with pytest.raises(RuntimeError,match='configuration failed'):
        release.activate('test-release','node')
    assert (active/'agent.py').read_text()=='old runtime'
    assert (active/'runtime-manifest.json').read_text()=='old manifest'
    assert not (active/'new.py').exists()
    assert ['systemctl','start','immersive-render.service'] in calls


def test_duplicate_and_partial_deployments_do_not_reactivate_old_snapshots():
    assert release.classify_activation([{'installed_release':'old','snapshot_exists':False}], 'new') == 'new'
    assert release.classify_activation([{'installed_release':'new','snapshot_exists':True}]*13, 'new') == 'already_active'
    assert release.classify_activation([{'installed_release':'new','snapshot_exists':True},
                                        {'installed_release':'old','snapshot_exists':False}], 'new') == 'recovery_required'
    assert release.classify_activation([{'installed_release':'old','snapshot_exists':True}], 'new') == 'recovery_required'
