import importlib.util
import sys
from pathlib import Path
import copy
import numpy as np
import pytest

LIVE=Path(__file__).resolve().parents[1]/'tools/phone-capture/live'
sys.path.insert(0,str(LIVE))
from geometry import LiveGeometry,corner_world,homography


def synthetic(view=0,drift=0,missing=False):
    markers=[];detected=[]
    camera=np.eye(4);camera[0,3]=view*.3
    k=np.array([[600.,0,960],[0,600,720],[0,0,1.]])
    for row in range(3):
        for col in range(4):
            if missing and col==0:continue
            cell=row*4+col
            x,y=40+col*320,40+row*260
            uv=np.array([[x,y],[x+160,y],[x+160,y+160],[x,y+160]],float)
            world=np.c_[uv[:,0]/400-1.6,1-uv[:,1]/400,np.full(4,-2.)]
            local=(world-camera[:3,3])*[1,-1,-1]
            pixels=local@k.T;pixels=pixels[:,:2]/pixels[:,2:]
            payload=f'm{cell}'
            markers.append({'payload':payload,'cell':cell,'projector_corners_px':uv.tolist()})
            detected.append({'payload':payload,'corners_px':pixels.tolist()})
    camera[0,3]+=drift
    layout={'nodes':[{'node':'wall','width':1280,'height':800,'markers':markers}]}
    record={'index':view,'intrinsics':k.ravel().tolist(),'image_resolution':[1920,1440],
            'camera_to_world':camera.ravel().tolist(),'tracking':'normal','backend_pattern_settled':True,
            'backend_scan_context':{'kind':'fiducials','node':'wall'},'recognized_projector_markers':detected}
    depth=np.full((192,256),2.,dtype='f4');confidence=np.full(depth.shape,2,dtype='u1')
    return layout,record,depth,confidence


def test_live_geometry_uses_held_out_views_not_counts():
    layout,*_=synthetic();engine=LiveGeometry(layout)
    for _ in range(50):
        _,r,d,c=synthetic();result=engine.add(r,d,c)
    assert result['nodes'][0]['distinct_views']==1
    assert not result['geometry_sufficient']
    for i in range(1,6):
        _,r,d,c=synthetic(i);result=engine.add(r,d,c)
    row=result['nodes'][0]
    assert row['geometry_sufficient'] and row['held_out_p95_px']<1e-8
    assert row['held_out_points']>=100
    assert not result['acquisition_complete'] and not result['calibration_validated']


def test_drifted_held_out_views_cannot_pass():
    layout,*_=synthetic();engine=LiveGeometry(layout)
    for i in range(6):
        _,r,d,c=synthetic(i,drift=.03 if i%2 else 0);result=engine.add(r,d,c)
    assert result['nodes'][0]['held_out_p95_px']>10
    assert not result['geometry_sufficient']


def test_missing_projection_edges_cannot_pass():
    layout,*_=synthetic();engine=LiveGeometry(layout)
    for i in range(8):
        _,r,d,c=synthetic(i,missing=True);result=engine.add(r,d,c)
    assert result['nodes'][0]['cells_seen']==9
    assert not result['geometry_sufficient']


def test_unstable_depth_and_tracking_do_not_add_views():
    layout,r,d,c=synthetic();engine=LiveGeometry(layout)
    c[:]=0
    result=engine.add(r,d,c)
    assert result['nodes'][0]['distinct_views']==0
    assert 'depth' in result['instruction']
    c[:]=2;r['tracking']='limited_relocalizing'
    result=engine.add(r,d,c)
    assert result['nodes'][0]['distinct_views']==0
    assert 'tracking' in result['instruction']


def test_planar_fit_rejects_degenerate_points():
    with pytest.raises(ValueError,match='degenerate'):
        homography(np.zeros((20,2)),np.zeros((20,2)))


def test_new_cells_at_same_position_expand_coverage_without_extra_views():
    layout,*_=synthetic();engine=LiveGeometry(layout)
    _,r,d,c=synthetic(missing=True);engine.add(r,d,c)
    assert engine.report()['nodes'][0]['cells_seen']==9
    _,r,d,c=synthetic();result=engine.add(r,d,c)
    assert result['nodes'][0]['cells_seen']==12
    assert result['nodes'][0]['distinct_views']==1
    assert not result['geometry_sufficient']
