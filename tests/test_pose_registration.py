import json
from pathlib import Path
import sys
import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools/phone-capture'))
from align_pose_segments import rigid,robust_rigid
from review_pose_segments import review


def test_registration_recovers_known_pose_with_wrong_anchor_matches():
    rng=np.random.default_rng(11)
    source=rng.uniform(-2,2,(80,3));source[:,1]=0
    angle=.7;r=np.array([[np.cos(angle),0,np.sin(angle)],[0,1,0],[-np.sin(angle),0,np.cos(angle)]])
    t=np.array([4.,.1,10.]);target=source@r.T+t
    target[:20]+=np.array([7.,2.,4.])
    rotation,translation,inliers,errors=robust_rigid(source,target)
    assert np.allclose(rotation,r) and np.allclose(translation,t)
    assert inliers.sum()==60 and np.max(errors[inliers])<1e-8


def test_insufficient_or_collinear_anchors_never_produce_registration():
    source=np.c_[np.arange(30),np.zeros((30,2))]
    with pytest.raises(ValueError,match='collinear'):rigid(source,source)
    with pytest.raises(ValueError,match='insufficient'):robust_rigid(source[:8],source[:8])
    with pytest.raises(ValueError,match='supported'):robust_rigid(source,source)


def test_pose_jump_detection_respects_elapsed_time_and_keeps_normal_tracking_evidence(tmp_path):
    # A 3m movement over 4s can be walking; a 5m change over .5s is a
    # discontinuity even if the producer reports normal tracking throughout.
    metadata={'session_id':'test','backend_scan':{'layout':{'nodes':[]}}}
    (tmp_path/'phone-session.json').write_text(json.dumps(metadata))
    index=[]
    for i,(time,x) in enumerate([(0,0),(4,3),(4.5,8),(5,8.1)]):
        pose=np.eye(4);pose[0,3]=x;name=f'{i:06d}.json'
        record={'index':i,'timestamp_s':time,'camera_to_world':pose.ravel().tolist(),'tracking':'normal','capture_eligible':False}
        (tmp_path/name).write_text(json.dumps(record));index.append({'file':name})
    (tmp_path/'live-index.json').write_text(json.dumps({'frames':index}))
    result=review(tmp_path)
    assert len(result['segments'])==2 and len(result['discontinuities'])==1
    jump=result['discontinuities'][0]
    assert jump['before_frame']==1 and jump['after_frame']==2 and jump['tracking_after']=='normal'
    assert not result['calibration_validated'] and not result['room_model_applied']


def test_capture_grid_represents_same_world_lines_after_projector_pixel_shift():
    pytest.importorskip('cv2')
    from make_capture_grid_test import grid_image
    origin=np.zeros(3);basis=np.array([[1.,0,0],[0,1.,0]])
    first=np.array([[100.,0,0],[0,100.,0],[0,0,1.]])
    second=first.copy();second[0,2]=10
    support=np.array([[0,0],[127,0],[127,79],[0,79]])
    a,coverage=grid_image(first,origin,basis,support,128,80)
    b,_=grid_image(second,origin,basis,support,128,80)
    assert coverage==1 and np.array_equal(a[:,1:117],b[:,11:127])


def test_capture_grid_masks_unmeasured_projection_regions():
    pytest.importorskip('cv2')
    from make_capture_grid_test import grid_image
    h=np.array([[100.,0,0],[0,100.,0],[0,0,1.]])
    image,fraction=grid_image(h,np.zeros(3),np.array([[1.,0,0],[0,1.,0]]),np.array([[20,20],[60,20],[60,60],[20,60]]),128,80)
    assert 0<fraction<.3
    assert not image[:20].any() and not image[:,61:].any()
    assert image[20:61,20:61].any()


def test_identity_baseline_mesh_does_not_change_calculated_grid():
    pytest.importorskip('cv2')
    from make_capture_grid_test import compensate_baseline_mesh
    image=np.random.default_rng(4).integers(0,256,(80,128,3),dtype='u1')
    entry={'mesh':{'cols':2,'rows':2,'points':[{'u':u,'v':v,'x':2*u-1,'y':1-2*v} for u,v in [(0,0),(1,0),(0,1),(1,1)]]}}
    assert np.array_equal(compensate_baseline_mesh(image,entry),image)


def test_baseline_compensation_uses_renderers_actual_triangle_diagonal():
    pytest.importorskip('cv2')
    from make_capture_grid_test import compensate_baseline_mesh
    image=np.repeat(np.tile(np.arange(101,dtype='u1'),(101,1))[:,:,None],3,axis=2)
    points=[{'u':u,'v':v,'x':2*u-1,'y':1-2*v} for u,v in [(0,0),(1,0),(0,1),(1,1)]]
    points[1]['x']+=.1
    entry={'mesh':{'cols':2,'rows':2,'points':points}}
    compensated=compensate_baseline_mesh(image,entry)
    assert compensated[70,20,0]==20  # TR has no influence in lower-left triangle.
    assert compensated[20,70,0]>70


def test_scan_meteor_mapping_agrees_for_same_physical_points_on_two_projectors():
    pytest.importorskip('cv2')
    from make_capture_meteor import world_map,panorama_mapping
    a={'plane_origin':[0,0,-2],'plane_axes':[[1,0,0],[0,1,0]],'plane_to_projector':[[100,0,0],[0,100,0],[0,0,1]]}
    b={**a,'plane_to_projector':[[100,0,10],[0,100,0],[0,0,1]]}
    wa=world_map(a,128,80);wb=world_map(b,128,80)
    assert np.allclose(wa[:,10:100],wb[:,20:110])
    xa,ya=panorama_mapping(wa,np.zeros(3),1,.3,15360,800,-1,2)
    xb,yb=panorama_mapping(wb,np.zeros(3),1,.3,15360,800,-1,2)
    assert np.allclose(xa[:,10:100],xb[:,20:110]) and np.allclose(ya[:,10:100],yb[:,20:110])


def test_scan_meteor_overlap_weights_form_partition_for_common_world_point():
    pytest.importorskip('cv2')
    from make_capture_meteor import coverage_weight
    a={'plane_origin':[0,0,-2],'plane_axes':[[1,0,0],[0,1,0]],'plane_to_projector':[[100,0,0],[0,100,0],[0,0,1]]}
    b={**a,'plane_to_projector':[[100,0,20],[0,100,0],[0,0,1]]}
    world=np.array([[[.5,.4,-2.]]])
    first=coverage_weight(world,a,128,80);second=coverage_weight(world,b,128,80)
    assert first.item()>0 and second.item()>0
    assert np.isclose((first/(first+second)+second/(first+second)).item(),1)
    outside=coverage_weight(np.array([[[.5,.4,-3.]]]),a,128,80)
    assert outside.item()==0  # Do not blend a physically different wall.


def test_observer_flight_has_coherent_parallax_and_continuous_loop():
    pytest.importorskip('cv2')
    from flight_scene import FlightScene
    scene=FlightScene(64,64,4,120)
    first,a=scene.centers(.2);second,b=scene.centers(.205)
    keep=(a>.99)&(b>.99)
    expected=-.005*scene.course_length*scene.forward
    assert keep.sum()>20 and np.allclose(second[keep]-first[keep],expected)
    start,alpha=scene.centers(0);end,end_alpha=scene.centers(1)
    assert np.allclose(start,end) and np.allclose(alpha,end_alpha)
    assert np.array_equal(np.asarray(scene.render(0)),np.asarray(scene.render(120)))
    assert not np.array_equal(np.asarray(scene.render(0)),np.asarray(scene.render(10)))


def test_falling_has_upward_flow_and_slower_moving_twinkling_stars():
    pytest.importorskip('cv2')
    from flight_scene import FallScene
    scene=FallScene(64,64,4,120)
    first,a=scene.centers(.2);second,b=scene.centers(.201)
    keep=(a>.99)&(b>.99)
    assert keep.sum()>50
    assert np.allclose((second-first)[keep],[0,0,.06])
    stars=scene.star_positions(.2);moved=scene.star_positions(.201)
    valid=abs(moved[:,1]-stars[:,1])<10
    assert np.all(moved[valid,1]<stars[valid,1])
    radii=np.linalg.norm(first[:,:2],axis=1);near=keep&(radii<10)
    near_asteroid_flow=.06/radii[near]*scene.width/(2*np.pi)
    assert np.median(abs(moved[valid,1]-stars[valid,1]))<np.median(near_asteroid_flow)
    assert not np.allclose(moved[:,2],stars[:,2])
    assert np.array_equal(np.asarray(scene.render(0)),np.asarray(scene.render(120)))
    assert not np.array_equal(np.asarray(scene.render(0)),np.asarray(scene.render(10)))


def test_enclosing_plunge_accelerates_continuously_and_fills_peripheral_view():
    pytest.importorskip('cv2')
    from flight_scene import PlungeScene
    scene=PlungeScene(64,64,4,120)
    step=.0001
    initial=(scene.progress(step)-scene.progress(0))/step
    middle=(scene.progress(.5+step)-scene.progress(.5))/step
    assert initial>0 and middle>15*initial
    assert np.isclose(scene.progress(1),2)
    first=np.asarray(scene.render_background(0));moved=np.asarray(scene.render_background(.05))
    # Dark fissures are part of the enclosing surface; at least 90% of the
    # peripheral image must still carry visible texture rather than empty sky.
    assert (first.mean(2)>15).mean()>.9
    assert np.mean(abs(first.astype(float)-moved.astype(float)))>5
    assert np.array_equal(np.asarray(scene.render(0)),np.asarray(scene.render(120)))
    start,a=scene.centers(0);end,b=scene.centers(1)
    assert np.allclose(start,end) and np.allclose(a,b)


def test_real_equirectangular_footage_uses_spherical_latitude_and_preserves_horizon():
    pytest.importorskip('cv2')
    from make_capture_meteor import equirectangular_mapping
    world=np.array([[[0.,0,1],[0,1,1],[0,-1,1],[1,0,0]]])
    x,y=equirectangular_mapping(world,np.zeros(3),1,np.pi,2000,1000,250,500)
    assert np.allclose(x,[[1000,1000,1000,1500]])
    assert np.allclose(y,[[249.5,0,499,249.5]])
    with pytest.raises(ValueError,match='latitude'):
        equirectangular_mapping(np.array([[[0.,10,1]]]),np.zeros(3),1,0,2000,1000,250,500)


def test_real_footage_decoder_delivers_complete_ordered_cropped_frames(tmp_path):
    import shutil,subprocess
    from fractions import Fraction
    if not shutil.which('ffmpeg'):pytest.skip('Native footage decoder needs ffmpeg')
    from video_scene import RealVideoScene
    path=tmp_path/'source.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=blue:size=128x64:rate=30',
                    '-frames:v','6','-c:v','libx264','-pix_fmt','yuv420p',str(path)],check=True)
    decoder=RealVideoScene(path,3,Fraction(30))
    try:
        with pytest.raises(ValueError,match='sequentially'):decoder.render(1)
        for i in range(3):
            image=decoder.render(i)
            assert image.shape==(32,128,3) and image[:,:,2].mean()>200 and image[:,:,0].mean()<10
    finally:decoder.close()


def test_opencl_mapping_and_blending_match_cpu_including_panorama_wrap():
    cv2=pytest.importorskip('cv2')
    cv2.ocl.setUseOpenCL(True)
    if not cv2.ocl.useOpenCL():pytest.skip('An OpenCL GPU is required for this numerical comparison')
    source=np.random.default_rng(7).integers(0,256,(48,96,3),dtype='u1')
    y,x=np.mgrid[:32,:64].astype('f4');x=(x*1.7+80.25).astype('f4');y=(y*.9+3.125).astype('f4')
    gain=np.repeat((.2+.8*np.mgrid[:32,:64][1]/63).astype('f4')[:,:,None],3,axis=2)
    expected=cv2.multiply(cv2.remap(source,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP),gain,dtype=cv2.CV_8U)
    actual=cv2.multiply(cv2.remap(cv2.UMat(source),cv2.UMat(x),cv2.UMat(y),cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP),cv2.UMat(gain),dtype=cv2.CV_8U).get()
    assert np.max(abs(actual.astype('i2')-expected.astype('i2')))<=1
