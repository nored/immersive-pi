import hashlib
import json

from controller import Controller
from roommodel import RoomModel


def package(tmp_path):
    assets={}
    for i in range(2):
        filename=f'node-{i}.mp4'
        (tmp_path/filename).write_bytes(b'clip')
        assets[f'node-{i}']={'file':filename,'sha256':hashlib.sha256(b'clip').hexdigest(),
            'width':1280,'height':800,'region':{'x':i/2,'y':0,'w':.5,'h':1}}
    value={'format':'immersive-tiled-v1','fps_num':30,'fps_den':1,'frames':100,'nodes':assets}
    path=tmp_path/'show.immersive.json';path.write_text(json.dumps(value))
    return path,value


def test_complete_tiled_shows_group_their_clips_without_changing_playback(tmp_path):
    path,value=package(tmp_path)
    (tmp_path/'panorama.mp4').write_bytes(b'panorama')
    (tmp_path/'directory.mp4').mkdir()
    master=Controller(RoomModel(tmp_path/'room.json'),0,0,0,media_dir=tmp_path)
    master.show['media']='node-0.mp4'
    listed=master.playlist()
    assert 'panorama.mp4' in listed['videos'] and path.name in listed['videos']
    assert not {'node-0.mp4','node-1.mp4','directory.mp4'}.intersection(listed['videos'])
    assert listed['current']==path.name
    assert master.show['media']=='node-0.mp4'


def test_broken_or_incomplete_manifest_cannot_hide_standalone_clips(tmp_path):
    path,value=package(tmp_path)
    master=Controller(RoomModel(tmp_path/'room.json'),0,0,0,media_dir=tmp_path)
    (tmp_path/'node-1.mp4').unlink()
    assert 'node-0.mp4' in master.playlist()['videos']
    path.write_text('{broken JSON')
    assert 'node-0.mp4' in master.playlist()['videos']
    path.write_text(json.dumps(['not a manifest object']))
    assert 'node-0.mp4' in master.playlist()['videos']
