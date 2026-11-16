import json
from pathlib import Path
import sys

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'test-media'))
from panorama_tiles import load_layout, source_size, tile_images
from make_ring_model import build


def test_overlapping_native_tiles_share_identical_pixels_across_the_ring_seam(tmp_path):
    path = tmp_path / 'room.json'
    model = build(3, .2, True, 0)
    path.write_text(json.dumps(model))
    layout = load_layout(path)
    assert [e['node'] for e in layout] == ['pi-01', 'pi-02', 'pi-03']
    assert source_size(layout, 10, 6) == (24, 6)
    panorama = Image.new('RGB', (24, 6))
    for y in range(6):
        for x in range(24):
            panorama.putpixel((x,y), (x*10,y*30,(x+y)*7))
    tiles = tile_images(panorama, layout, 10, 6)
    for i in range(3):
        assert tiles[i].size == (10,6)
        assert tiles[i].crop((8,0,10,6)).tobytes() == tiles[(i+1)%3].crop((0,0,2,6)).tobytes()
    assert tiles[0].tobytes() == panorama.crop((0,0,10,6)).tobytes()


def test_disjoint_layout_retains_original_native_pixels():
    layout = load_layout(nodes=5)
    assert source_size(layout, 8, 4) == (40,4)
    panorama = Image.new('RGB', (40,4))
    for x in range(40):
        for y in range(4):panorama.putpixel((x,y),(x,y,x+y))
    for i,tile in enumerate(tile_images(panorama,layout,8,4)):
        assert tile.tobytes() == panorama.crop((i*8,0,(i+1)*8,4)).tobytes()


def test_subpixel_source_window_uses_sufficient_source_resolution(tmp_path):
    path=tmp_path/'room.json'
    path.write_text(json.dumps({'nodes':[{'node':'custom-node','source_region':
        {'x':.2,'y':.1,'w':.4,'h':.5}}]}))
    layout=load_layout(path)
    assert source_size(layout, 12, 8) == (30,16)
    tiles=tile_images(Image.new('RGB',(30,16),(90,120,150)),layout,12,8)
    assert tiles[0].getextrema() == ((90,90),(120,120),(150,150))


def test_negative_wrapped_window_reads_the_end_then_start_of_the_panorama(tmp_path):
    path=tmp_path/'room.json'
    path.write_text(json.dumps({'nodes':[{'node':'seam-node','source_region':
        {'x':-.25,'y':0,'w':.5,'h':1,'wrap':True}}]}))
    layout=load_layout(path)
    panorama=Image.new('RGB',(8,2))
    for x in range(8):
        for y in range(2):panorama.putpixel((x,y),(x*20,y*30,0))
    tile=tile_images(panorama,layout,4,2)[0]
    assert [tile.getpixel((x,0))[0] for x in range(4)] == [120,140,0,20]


@pytest.mark.parametrize('changes', [
    {'x':-.1}, {'x':.8,'w':.3}, {'y':-.1}, {'y':.9,'h':.5},
    {'w':0}, {'w':1.1}, {'h':float('nan')}, {'x':True}, {'wrap':'yes'},
])
def test_unsupported_source_windows_are_rejected_before_encoding(tmp_path, changes):
    path=tmp_path/'room.json'
    region={'x':0,'y':0,'w':1,'h':1,**changes}
    path.write_text(json.dumps({'nodes':[{'node':'node-a','source_region':region}]}))
    with pytest.raises(ValueError):load_layout(path)


def test_duplicate_ids_and_conflicting_node_counts_are_rejected(tmp_path):
    path=tmp_path/'room.json';model=build(3,.2,True,0)
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError,match='disagrees'):load_layout(path,nodes=12)
    model['nodes'][1]['node']=model['nodes'][0]['node']
    path.write_text(json.dumps(model))
    with pytest.raises(ValueError,match='unique'):load_layout(path)
