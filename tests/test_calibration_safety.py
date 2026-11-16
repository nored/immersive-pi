import copy
import json

import pytest

from roommodel import RoomModel, default_node_entry
from shared.calibration import validate_solution


def test_invalid_scan_geometry_cannot_replace_or_save_working_calibration(tmp_path):
    path = tmp_path / 'room.json'
    room = RoomModel(path)
    room.ensure_node('node-1')
    room.save('baseline', commit=False)
    original = path.read_bytes()
    baseline = copy.deepcopy(room.model)
    for invalid in (float('nan'), float('inf')):
        mesh = copy.deepcopy(room.entry_for('node-1')['mesh'])
        mesh['points'][0]['u'] = invalid
        with pytest.raises(ValueError):
            room.update_field('node-1', 'mesh', mesh)
        assert room.model == baseline
        assert path.read_bytes() == original
    room.model['nodes'][0]['mesh']['points'][0]['u'] = float('nan')
    with pytest.raises(ValueError):
        room.save('must not replace file', commit=False)
    assert path.read_bytes() == original
    assert room.model['version'] == baseline['version']


def test_collapsed_or_folded_solutions_are_rejected_before_apply():
    entry = default_node_entry('node-1')
    validate_solution([entry], ['node-1'])
    collapsed = copy.deepcopy(entry)
    for p in collapsed['mesh']['points']:
        p['u'] = .5
    with pytest.raises(ValueError, match='collapsed'):
        validate_solution([collapsed], ['node-1'])
    folded = copy.deepcopy(entry)
    folded['mesh']['points'][3]['x'] = -2
    with pytest.raises(ValueError, match='folded'):
        validate_solution([folded], ['node-1'])
    with pytest.raises(ValueError, match='selected nodes'):
        validate_solution([entry], ['node-1', 'node-2'])


def test_invalid_drag_cannot_mutate_the_live_mesh(tmp_path):
    room = RoomModel(tmp_path/'room.json')
    room.ensure_node('node-1')
    before = copy.deepcopy(room.model)
    with pytest.raises(ValueError):
        room.apply_mesh_delta('node-1', 0, float('nan'), 1)
    assert room.model == before
    with pytest.raises(ValueError):
        room.set_mesh_resolution('node-1', 1, 4)
    assert room.model == before


@pytest.mark.parametrize('options', [{}, {'ring':False}])
def test_retired_legacy_scanner_cannot_stop_or_change_a_running_room(tmp_path, options):
    import asyncio
    from controller import Controller
    async def run():
        room=RoomModel(tmp_path/'room.json');room.ensure_node('node-1')
        before=copy.deepcopy(room.model)
        ctl=Controller(room,0,0,0,media_dir=tmp_path)
        ctl.show['playing']=True
        ctl._session='running'
        messages=[]
        async def forbidden(*a,**kw):raise AssertionError('Legacy scanner touched live playback')
        async def broadcast(message):messages.append(message)
        ctl.cmd_stop=forbidden;ctl._send_node=forbidden;ctl._broadcast_web=broadcast
        await ctl.cmd_autocalibrate(options)
        assert ctl.show['playing'] and ctl._session=='running'
        assert room.model==before
        assert len(messages)==1 and messages[0]['stage']=='error'
        assert 'retired' in messages[0]['msg']
        with pytest.raises(ValueError, match='retired'):
            await ctl._on_web_msg(json.dumps({'cmd':'scan','node':'node-1','on':True}))
    asyncio.run(run())


def test_retired_scanner_cli_has_no_camera_or_controller_side_effects():
    from pathlib import Path
    import subprocess,sys
    path=Path(__file__).resolve().parents[1]/'control-node/autocalib/scan.py'
    result=subprocess.run([sys.executable,str(path),'--self-test'],capture_output=True,text=True,timeout=5)
    assert result.returncode==2
    assert 'retired' in result.stderr and 'No camera' in result.stderr
    assert not result.stdout
