import copy
import hashlib
import json

import pytest

from roommodel import default_node_entry
from shared.media_package import load_plan, validate_tile_coverage
from gl_pipeline import build_mesh_arrays


def test_tile_uv_mapping_preserves_geometry_and_uses_entire_local_video():
    entry = default_node_entry('node-2')
    region = {'x': 1/12, 'y': 0, 'w': 1/12, 'h': 1}
    entry['source_region'] = region
    global_vertices, _ = build_mesh_arrays(entry)
    local_vertices, _ = build_mesh_arrays(entry, region)
    assert local_vertices.reshape(-1,6)[:,0:2].tolist() == global_vertices.reshape(-1,6)[:,0:2].tolist()
    assert local_vertices.reshape(-1,6)[:,2:4].tolist() == [[0,0],[1,0],[0,1],[1,1]]


def test_missing_node_tile_and_unsafe_paths_are_rejected(tmp_path):
    (tmp_path/'tile.mp4').write_bytes(b'test')
    asset = {'file':'tile.mp4', 'sha256':hashlib.sha256(b'test').hexdigest(),
             'width':1920, 'height':1080, 'region':{'x':0,'y':0,'w':1,'h':1}}
    package = {'format':'immersive-tiled-v1','fps_num':30,'fps_den':1,'frames':900,
               'nodes':{'node-1':asset}}
    path = tmp_path/'show.immersive.json'
    path.write_text(json.dumps(package))
    plan = load_plan(tmp_path, path.name, ['node-1'])
    assert plan['nodes']['node-1']['width'] == 1920
    with pytest.raises(ValueError, match='no tile'):
        load_plan(tmp_path, path.name, ['node-1','node-2'])
    package['nodes']['node-1']['file'] = '../tile.mp4'
    path.write_text(json.dumps(package))
    with pytest.raises(ValueError, match='plain MP4'):
        load_plan(tmp_path, path.name, ['node-1'])


def test_tile_coverage_accepts_overlapping_wrapped_windows_and_rejects_missing_pixels():
    entry = default_node_entry('last-node')
    entry['source_region'] = {'x':.9,'y':0,'w':.2,'h':1,'wrap':True}
    tile = {'x':.85,'y':0,'w':.3,'h':1}
    validate_tile_coverage(entry, tile)
    validate_tile_coverage(entry, None)
    with pytest.raises(ValueError, match='does not cover'):
        validate_tile_coverage(entry, {'x':.9,'y':0,'w':.1,'h':1})
    entry['mesh']['points'][1]['u'] = 2
    with pytest.raises(ValueError, match='does not cover'):
        validate_tile_coverage(entry, tile)


def test_rejected_active_source_edit_leaves_room_and_projector_unchanged(tmp_path):
    import asyncio
    from controller import Controller
    from roommodel import RoomModel

    async def run():
        room = RoomModel(tmp_path/'room.json')
        room.ensure_node('node-1'); room.ensure_node('node-2')
        room.update_field('node-1', 'source_region', {'x':0,'y':0,'w':.5,'h':1})
        room.save('baseline', commit=False)
        before = copy.deepcopy(room.model); saved = room.path.read_bytes()
        master = Controller(room, 0, 0, 0, media_dir=tmp_path)
        master._session = 'active'
        master._media_plan = {'nodes':{'node-1':{'region':{'x':0,'y':0,'w':.5,'h':1}}}}
        sent = []
        async def send(node, message):sent.append((node,message))
        master._send_node = send
        with pytest.raises(ValueError, match='does not cover'):
            await master._edit('set_source_region', {'node':'node-1',
                'source_region':{'x':0,'y':0,'w':.6,'h':1}})
        assert room.model == before and room.path.read_bytes() == saved
        assert not sent
        master._session = None  # stopped layout edits can plan new media coverage
        await master._edit('set_source_region', {'node':'node-1',
            'source_region':{'x':0,'y':0,'w':.6,'h':1}})
        assert room.entry_for('node-1')['source_region']['w'] == .6
        assert room.entry_for('node-2') == before['nodes'][1]
        assert room.path.read_bytes() == saved  # explicit Save persists the draft
    asyncio.run(run())


def test_prepare_rejects_incompatible_tile_before_requesting_any_decoder(tmp_path):
    import asyncio
    from controller import Controller
    from roommodel import RoomModel
    from types import SimpleNamespace

    async def run():
        room = RoomModel(tmp_path/'room.json'); room.ensure_node('node-1')
        master = Controller(room, 0, 0, 0, media_dir=tmp_path)
        sent = []
        async def send(data):sent.append(json.loads(data))
        master.nodes['node-1'] = SimpleNamespace(send=send)
        master.node_info['node-1'] = {'protocol':2,'capabilities':['per_node_video_v1']}
        (tmp_path/'tile.mp4').write_bytes(b'test')
        asset={'file':'tile.mp4','sha256':hashlib.sha256(b'test').hexdigest(),
               'width':1280,'height':800,'region':{'x':0,'y':0,'w':.5,'h':1}}
        (tmp_path/'show.immersive.json').write_text(json.dumps({
            'format':'immersive-tiled-v1','fps_num':30,'fps_den':1,'frames':30,
            'nodes':{'node-1':asset}}))
        with pytest.raises(ValueError, match='does not cover'):
            await master.cmd_prepare('test', 'show.immersive.json')
        assert not any(m['cmd']=='prepare' for m in sent)
        assert master._session is None
    asyncio.run(run())


def test_renderer_rejects_incompatible_live_edit_before_changing_its_entry():
    from agent import RenderNode
    from types import SimpleNamespace
    node = RenderNode.__new__(RenderNode)
    node.node = 'node-1'; node._entry = default_node_entry('node-1')
    node._entry['source_region'] = {'x':0,'y':0,'w':.5,'h':1}
    node._session = 'active'
    calls = []
    node.stage = SimpleNamespace(_video_region={'x':0,'y':0,'w':.5,'h':1}, set_entry=calls.append)
    before = copy.deepcopy(node._entry)
    with pytest.raises(ValueError, match='does not cover'):
        node._merge_entry('source_region', {'x':0,'y':0,'w':.6,'h':1})
    assert node._entry == before and not calls
