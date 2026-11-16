import hashlib
import importlib.util
import importlib
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

spec = importlib.util.spec_from_file_location('phone_capture', Path(__file__).resolve().parents[1]/'tools/phone-capture/validate_capture.py')
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


@pytest.fixture
def bundle(tmp_path):
    Image.new('RGB',(8,6),(100,120,140)).save(tmp_path/'000000.jpg')
    np.ones((2,4),dtype='<f4').tofile(tmp_path/'000000-depth.f32le')
    np.full((2,4),2,dtype=np.uint8).tofile(tmp_path/'000000-confidence.u8')
    def asset(name):
        data=(tmp_path/name).read_bytes()
        return {'file':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    frame={'index':0,'timestamp_s':12.5,'timestamp_clock':'ARFrame','tracking':'normal',
        'image_resolution':[8,6],'intrinsics':[4,0,4,0,4,3,0,0,1],
        'camera_to_world':np.eye(4).ravel().tolist(), 'capture_eligible':True,
        'rgb':asset('000000.jpg'),'depth':asset('000000-depth.f32le'),
        'confidence':asset('000000-confidence.u8'),'depth_resolution':[4,2],
        'confidence_resolution':[4,2],'depth_encoding':'float32_little_endian_packed_rows',
        'confidence_encoding':'ARConfidenceLevel_uint8_packed_rows'}
    session={'format':'immersive-arkit-capture-v1','session_id':'test','status':'complete','errors':[],
        'matrix_storage':'row_major','pose_convention':'ARKit_camera_to_world_meters',
        'intrinsics_coordinates':'native_RGB_pixels','rgb_orientation':'native_sensor_unrotated',
        'camera_axes':'x_right_y_up_z_back; forward_is_negative_z',
        'depth_convention':'ARKit_sceneDepth_axial_meters','world_alignment':'gravity',
        'frames':['000000.json']}
    def save():
        (tmp_path/'session.json').write_text(json.dumps(session))
        (tmp_path/'000000.json').write_text(json.dumps(frame))
    save()
    return tmp_path,frame,session,asset,save


def test_valid_capture_passes_integrity_without_claiming_calibration(bundle):
    root,*_=bundle
    before={p.name:p.read_bytes() for p in root.iterdir()}
    result=capture.inspect_bundle(root)
    assert result['capture_checks_passed'] and result['eligible_observations']==1
    assert not result['calibration_validated']
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}


@pytest.mark.parametrize('kind',['black','tracking','depth'])
def test_bad_observations_stay_archived_but_cannot_pass_on_a_claimed_eligible_flag(bundle,kind):
    root,frame,session,asset,save=bundle
    if kind=='black':
        Image.new('RGB',(8,6),(0,0,0)).save(root/'000000.jpg')
        frame['rgb']=asset('000000.jpg')
    elif kind=='tracking':frame['tracking']='limited_relocalizing'
    else:
        np.full((2,4),np.nan,dtype='<f4').tofile(root/'000000-depth.f32le')
        frame['depth']=asset('000000-depth.f32le')
    save();result=capture.inspect_bundle(root)
    assert not result['capture_checks_passed']
    assert not result['errors'] and result['eligible_observations']==0
    assert result['frames'][0]['review_reasons']
    assert (root/'000000.jpg').is_file()


@pytest.mark.parametrize('kind',['integrity','image_size','confidence_size','pose_scale','timestamp'])
def test_mismatched_or_corrupted_sensor_records_are_rejected(bundle,kind):
    root,frame,session,asset,save=bundle
    if kind=='integrity':(root/'000000-depth.f32le').write_bytes(b'tampered')
    elif kind=='image_size':frame['image_resolution']=[6,8]
    elif kind=='confidence_size':frame['confidence_resolution']=[2,4]
    elif kind=='pose_scale':frame['camera_to_world'][0]=2
    else:frame['timestamp_clock']='unknown'
    save();result=capture.inspect_bundle(root)
    assert result['errors'] and not result['capture_checks_passed']


def test_interrupted_capture_and_unsupported_coordinate_conventions_are_not_accepted(bundle):
    root,frame,session,asset,save=bundle
    session['status']='incomplete';session['errors']=['ARSession interrupted']
    save();assert not capture.inspect_bundle(root)['capture_checks_passed']
    session['matrix_storage']='column_major';save()
    with pytest.raises(ValueError,match='convention'):capture.inspect_bundle(root)


def test_bundle_cannot_read_files_outside_capture_directory(bundle):
    root,frame,session,asset,save=bundle
    frame['rgb']['file']='../outside.jpg';save()
    result=capture.inspect_bundle(root)
    assert result['errors'] and 'plain local names' in result['errors'][0]['error']


@pytest.fixture
def geometry(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'tools/phone-capture'))
    return importlib.import_module('reconstruct_capture')


def test_axial_depth_native_intrinsics_and_camera_axes(geometry):
    depth = np.full((3, 4), 2.0)
    # RGB is 3x wider and 2x taller: intrinsics must scale on each axis.
    k = np.array([[6,0,6],[0,8,2],[0,0,1]], dtype=float)
    points, pixels = geometry.unproject(depth, np.full_like(depth, 2), k, np.eye(4),
                                        (12,6), stride=1)
    center = np.flatnonzero((pixels == [2,1]).all(axis=1))[0]
    assert points[center] == pytest.approx([0,0,-2])
    assert points[0] == pytest.approx([-2,.5,-2])
    # Off-axis distances exceed 2m; normalizing each ray would bend a flat wall.
    assert np.linalg.norm(points[0]) > 2
    assert np.allclose(points[:, 2], -2)


def test_rotated_and_translated_camera_preserves_a_known_wall(geometry):
    k = np.array([[5,0,2],[0,5,1],[0,0,1]], dtype=float)
    theta = np.deg2rad(25)
    pose = np.array([[np.cos(theta),0,np.sin(theta),.4],
                     [0,1,0,.1],[-np.sin(theta),0,np.cos(theta),.2],[0,0,0,1]])
    # Independently ray-cast an infinite world wall z=-3 into this camera.
    depth = np.empty((3,5))
    expected = []
    for y in range(3):
        for x in range(5):
            direction = pose[:3,:3] @ [(x-2)/5, -(y-1)/5, -1]
            distance = (-3 - pose[2,3]) / direction[2]
            depth[y,x] = distance
            expected.append(pose[:3,3] + direction * distance)
    points, _ = geometry.unproject(depth, np.full_like(depth,2), k, pose, (5,3), stride=1)
    assert points == pytest.approx(np.array(expected))
    assert points[:,2] == pytest.approx(np.full(15,-3.0))


def test_unreliable_depth_samples_cannot_enter_the_point_cloud(geometry):
    depth = np.array([[1,2,np.nan,np.inf,0,-1,.1,9]], dtype=float)
    confidence = np.array([[2,1,2,2,2,2,2,2]])
    points, pixels = geometry.unproject(depth, confidence, np.eye(3), np.eye(4), (8,1), stride=1)
    assert pixels.tolist() == [[0,0]]
    assert points.tolist() == [[0,0,-1]]
    _, medium = geometry.unproject(depth, confidence, np.eye(3), np.eye(4), (8,1),
                                    stride=1, min_confidence=1)
    assert medium.tolist() == [[0,0],[1,0]]


def test_cloud_export_is_read_only_and_ply_payload_matches_header(bundle,geometry):
    root,*_=bundle
    before={p.name:p.read_bytes() for p in root.iterdir()}
    output=root.parent/(root.name+'-cloud')
    report=geometry.reconstruct(root,output,stride=1)
    assert report['points']==8 and report['frames_used']==1
    assert report['camera_path_length_m']==0
    assert not report['calibration_validated'] and not report['registration_performed']
    header, payload=(output/'scene.ply').read_bytes().split(b'end_header\n',1)
    assert b'element vertex 8\n' in header
    points=np.frombuffer(payload,dtype=geometry.VERTEX)
    assert len(points)==8 and (points['xyz'][:,2]==-1).all()
    assert np.all(points['rgb']>80)
    assert json.loads((output/'report.json').read_text())==report
    assert len(json.loads((output/'trajectory.json').read_text()))==1
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ValueError,match='already exists'):
        geometry.reconstruct(root,output)


@pytest.mark.parametrize('kind',['incomplete','corrupt','inside','empty'])
def test_reconstruction_rejects_bad_or_unsafe_inputs_without_output(bundle,geometry,kind):
    root,frame,session,asset,save=bundle
    output=root.parent/(root.name+'-cloud')
    kwargs={}
    if kind=='incomplete':session['status']='incomplete'
    elif kind=='corrupt':(root/'000000-depth.f32le').write_bytes(b'bad')
    elif kind=='inside':output=root/'derived'
    else:kwargs={'near':3,'far':4}
    save()
    with pytest.raises(ValueError):geometry.reconstruct(root,output,**kwargs)
    assert not output.exists()


@pytest.fixture
def agreement(geometry):
    return importlib.import_module('check_depth_agreement')


def wall_view(name='a', translation=(0,0,0), depth=3):
    pose=np.eye(4);pose[:3,3]=translation
    return {'name':name,'pose':pose,'rgb_size':(640,480),
            'k':np.array([[500,0,320],[0,500,240],[0,0,1]]),
            'depth':np.full((48,64),depth,dtype=float),
            'confidence':np.full((48,64),2,dtype=np.uint8)}


def test_translated_views_of_the_same_wall_agree_bidirectionally(agreement):
    a=wall_view()
    b=wall_view('b',(.2,0,.3),depth=3.3)
    result=agreement.compare_pair(a,b)
    assert result['outcome']=='consistent_coarse_depth'
    for direction in ['forward','backward']:
        assert result[direction]['compared_points']>=100
        assert result[direction]['p95_absolute_depth_error_m']<1e-6


def test_wrong_recorded_pose_cannot_pass_on_one_occluded_direction(agreement):
    a=wall_view()
    b=wall_view('b',(.2,0,.4),depth=3)
    result=agreement.compare_pair(a,b)
    assert result['outcome']=='needs_review'
    assert result['forward']['occluded_points']>100
    assert result['forward']['compared_points']==0
    assert result['backward']['free_space_contradictions']>100


def test_turning_in_place_is_not_independent_translation_evidence(agreement):
    result=agreement.compare_pair(wall_view(),wall_view('b'))
    assert result['outcome']=='insufficient_translation'


@pytest.mark.parametrize('kind',['opposite_view','unreliable_target','depth_edge'])
def test_absent_overlap_or_depth_evidence_does_not_count_as_agreement(agreement,kind):
    a=wall_view();b=wall_view('b',(.2,0,0))
    if kind=='opposite_view':b['pose'][:3,:3]=np.diag([-1,1,-1])
    elif kind=='unreliable_target':b['confidence'][:]=0
    else:b['depth'][:,::2]=1  # Every 3x3 patch crosses a discontinuity.
    result=agreement.compare_pair(a,b)
    assert result['outcome']!='consistent_coarse_depth'
    assert result['forward']['compared_points']==0


def test_foreground_occlusion_is_excluded_from_directional_residuals(agreement):
    a=wall_view();b=wall_view('b',(.2,0,0))
    b['depth'][:,:32]=1
    result=agreement.compare_direction(a,b)
    assert result['occluded_points']>0 and result['compared_points']>0
    assert result['free_space_contradictions']==0
    assert result['agreement_fraction']==1


def test_single_frame_bundle_cannot_establish_cross_view_consistency(bundle,agreement):
    root,*_=bundle
    before={p.name:p.read_bytes() for p in root.iterdir()}
    result=agreement.inspect_agreement(root)
    assert result['frames_sampled']==1 and result['pairs']==[]
    assert sum(result['pair_counts'].values())==0
    assert not result['calibration_validated'] and not result['poses_modified']
    assert before=={p.name:p.read_bytes() for p in root.iterdir()}


@pytest.fixture
def wall_draft(geometry):
    return importlib.import_module('fit_wall_draft')


def test_surface_extraction_does_not_force_four_walls_or_close_missing_corners(wall_draft):
    random=np.random.default_rng(914)
    segments=[([0,0],[6,1]),([6,1],[7,5]),([7,5],[1,4])]
    points=[]
    for start,end in segments:
        t=np.linspace(0,1,600)[:,None]
        points.append(np.array(start)*(1-t)+np.array(end)*t+random.normal(0,.004,(600,2)))
    points.append(random.uniform([-2,-2],[9,7],(100,2)))
    surfaces=wall_draft.extract_surfaces(np.concatenate(points),trials=500)
    assert len(surfaces)==3
    for start,end in segments:
        tangent=np.array(end)-start;tangent=tangent/np.linalg.norm(tangent)
        normal=np.array([-tangent[1],tangent[0]])
        matching=[s for s in surfaces if abs(np.dot(s['normal_xz'],normal))>.999
                  and abs(np.dot(s['normal_xz'],start)-s['offset_m'])<.03]
        assert len(matching)==1
        for endpoint in matching[0]['observed_segment_xz']:
            assert abs(np.dot(np.array(endpoint)-start,normal))<.02


def test_repeated_views_of_one_patch_do_not_manufacture_surface_support(wall_draft):
    points=np.tile([[1,2]],(10000,1))
    assert wall_draft.extract_surfaces(points)==[]


def test_collinear_patches_do_not_silently_fill_an_unobserved_wall_gap(wall_draft):
    x=np.concatenate([np.linspace(0,1,300),np.linspace(3,4,300)])
    surfaces=wall_draft.extract_surfaces(np.stack([x,np.zeros_like(x)],axis=1),trials=100)
    assert len(surfaces)==1
    assert .45<surfaces[0]['along_coverage_fraction']<.55
    assert wall_draft.assess_frame([[2,0]],surfaces)['associated_points']==0


def test_held_out_surface_offset_is_reported_without_refitting_it_away(wall_draft):
    x=np.linspace(0,5,600)
    surfaces=wall_draft.extract_surfaces(np.stack([x,np.zeros_like(x)],axis=1),trials=100)
    assert len(surfaces)==1
    result=wall_draft.assess_frame(np.stack([x,np.full_like(x,.14)],axis=1),surfaces)
    assert result['associated_points']>500
    assert result['surfaces'][0]['p95_absolute_error_m']==pytest.approx(.14)
    assert abs(result['surfaces'][0]['median_signed_offset_m'])==pytest.approx(.14)


def test_unobserved_extensions_and_distant_points_stay_unassigned(wall_draft):
    x=np.linspace(0,5,600)
    surfaces=wall_draft.extract_surfaces(np.stack([x,np.zeros_like(x)],axis=1),trials=100)
    result=wall_draft.assess_frame([[6,0],[2,1],[2,.02]],surfaces)
    assert result['slice_points']==3 and result['associated_points']==1


def test_competing_surface_hypotheses_are_marked_as_ambiguous(wall_draft):
    x=np.linspace(0,5,600)
    surface=wall_draft.extract_surfaces(np.stack([x,np.zeros_like(x)],axis=1),trials=100)[0]
    duplicate=dict(surface,id=1,offset_m=surface['offset_m']+.01)
    result=wall_draft.assess_frame([[2,.005],[2,.2]],[surface,duplicate])
    assert result['associated_points']==2 and result['ambiguous_points']==2


def test_empty_depth_slice_cannot_generate_a_room_shape(bundle,wall_draft):
    root,*_=bundle
    with pytest.raises(ValueError,match='no training points'):
        wall_draft.inspect_walls(root,y_min=10,y_max=11)


@pytest.mark.parametrize('points',[np.array([[float('nan'),0]]),np.zeros((3,3))])
def test_invalid_surface_points_are_rejected(wall_draft,points):
    with pytest.raises(ValueError):wall_draft.extract_surfaces(points)


@pytest.fixture
def guided_bundle(bundle):
    root,frame,session,asset,save=bundle
    session['session_id']='11111111-2222-3333-4444-555555555555'
    backend_session='a'*32
    node={'node':'beamer-west','width':1280,'height':800,'sha256':'b'*64}
    ack={'session':backend_session,'phase':1,'kind':'fiducials','width':1280,'height':800,
         'sha256':'b'*64,'flip_monotonic_ns':1234567}
    context={'session':backend_session,'phase':1,'kind':'overview','node':None,
             'layout_id':backend_session[:16],'ready':True,'display_acks':{'beamer-west':ack}}
    session['backend_scan']={'protocol':1,'session':backend_session,'capture_id':session['session_id'],
        'layout':{'protocol':1,'layout_id':backend_session[:16],'nodes':[node]},'context':context}
    frame['backend_scan_context']=json.loads(json.dumps(context))
    frame['backend_pattern_settled']=True
    save()
    return bundle


def test_guided_recording_requires_matching_saved_display_receipts(guided_bundle):
    root,*_=guided_bundle
    result=capture.inspect_bundle(root)
    assert result['capture_checks_passed'] and not result['calibration_validated']


@pytest.mark.parametrize('kind',['session','layout','sha','dimensions','flip','missing_ack','missing_context','capture_id'])
def test_wrong_or_missing_guided_context_cannot_be_accepted(guided_bundle,kind):
    root,frame,session,asset,save=guided_bundle
    context=frame['backend_scan_context'];ack=context['display_acks']['beamer-west']
    if kind=='session':context['session']='c'*32
    elif kind=='layout':context['layout_id']='c'*16
    elif kind=='sha':ack['sha256']='c'*64
    elif kind=='dimensions':ack['width']=1920
    elif kind=='flip':ack['flip_monotonic_ns']=None
    elif kind=='missing_ack':context['display_acks']={}
    elif kind=='missing_context':del frame['backend_scan_context']
    else:session['backend_scan']['capture_id']='66666666-7777-8888-9999-000000000000'
    save()
    if kind=='capture_id':
        with pytest.raises(ValueError,match='capture ID'):capture.inspect_bundle(root)
    else:
        result=capture.inspect_bundle(root)
        assert result['errors'] and not result['capture_checks_passed']


@pytest.mark.parametrize('kind',['not_ready','not_settled'])
def test_phase_transition_frames_remain_archived_and_ineligible(guided_bundle,kind):
    root,frame,session,asset,save=guided_bundle
    if kind=='not_ready':frame['backend_scan_context']['ready']=False
    else:frame['backend_pattern_settled']=False
    frame['capture_eligible']=True  # Recompute quality instead of trusting this.
    save();result=capture.inspect_bundle(root)
    assert not result['capture_checks_passed'] and not result['errors']
    assert result['eligible_observations']==0 and (root/'000000.jpg').is_file()


def live_receiver():
    import sys
    folder=Path(__file__).resolve().parents[1]/'tools/phone-capture/live'
    if str(folder) not in sys.path:sys.path.insert(0,str(folder))
    spec=importlib.util.spec_from_file_location('immersive_live_server',folder/'server.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def live_packet(root,frame):
    import base64
    return {'record':frame,'assets':{frame[k]['file']:base64.b64encode((root/frame[k]['file']).read_bytes()).decode()
                                    for k in ('rgb','depth','confidence') if frame.get(k)}}


def test_live_receiver_commits_assets_and_quality_before_ack(guided_bundle,tmp_path_factory):
    root,frame,session,asset,save=guided_bundle
    session['backend_scan']['layout']['nodes'][0]['markers']=[]
    server=live_receiver();receiver=server.LiveSession(tmp_path_factory.mktemp('receiver'),session)
    result=receiver.ingest(live_packet(root,frame))
    assert result['frames_stored']==1 and result['frame_index']==0
    assert (receiver.directory/'000000.json').is_file()
    assert (receiver.directory/frame['rgb']['file']).read_bytes()==(root/frame['rgb']['file']).read_bytes()
    assert json.loads((receiver.directory/'quality.json').read_text())==result
    assert not result['acquisition_complete'] and not result['calibration_validated']
    with pytest.raises(ValueError,match='stale'):receiver.ingest(live_packet(root,frame))
    assert len(receiver.frames)==1


def test_corrupted_live_asset_never_adds_geometry_or_receipt(guided_bundle,tmp_path_factory):
    root,frame,session,asset,save=guided_bundle
    session['backend_scan']['layout']['nodes'][0]['markers']=[]
    receiver=live_receiver().LiveSession(tmp_path_factory.mktemp('receiver'),session)
    packet=live_packet(root,frame);packet['assets'][frame['rgb']['file']]='YQ=='
    with pytest.raises(ValueError,match='integrity'):receiver.ingest(packet)
    assert not receiver.frames and receiver.last_index==-1
    assert not (receiver.directory/'000000.json').exists()


def test_dark_live_reference_is_archived_without_geometric_credit(guided_bundle,tmp_path_factory):
    root,frame,session,asset,save=guided_bundle
    session['backend_scan']['layout']['nodes'][0]['markers']=[]
    Image.new('RGB',(8,6),(0,0,0)).save(root/'000000.jpg')
    frame['rgb']=asset('000000.jpg')
    context=frame['backend_scan_context'];context.update(kind='black',node='beamer-west')
    context['display_acks']['beamer-west'].update(kind='black',sha256=None)
    receiver=live_receiver().LiveSession(tmp_path_factory.mktemp('receiver'),session)
    result=receiver.ingest(live_packet(root,frame))
    assert result['frames_stored']==1 and not receiver.frames[0]['eligible']
    assert not result['geometry_sufficient']


def test_full_resolution_live_depth_reaches_independent_geometry_checks(guided_bundle,tmp_path_factory):
    from test_live_scan import synthetic
    import time
    root,frame,session,asset,save=guided_bundle
    layout,*_=synthetic()
    node=layout['nodes'][0];node['sha256']='b'*64
    session['backend_scan']['layout']['nodes']=[node]
    receiver=live_receiver().LiveSession(tmp_path_factory.mktemp('receiver'),session)
    elapsed=[]
    for i in range(6):
        _,record,depth,confidence=synthetic(i)
        record.update(index=i,timestamp_s=i+.5,timestamp_clock='ARFrame',
                      depth_resolution=[256,192],confidence_resolution=[256,192],
                      depth_encoding='float32_little_endian_packed_rows',
                      confidence_encoding='ARConfidenceLevel_uint8_packed_rows')
        context=json.loads(json.dumps(frame['backend_scan_context']))
        ack=context['display_acks'].pop('beamer-west');context['display_acks']['wall']=ack
        record['backend_scan_context']=context
        stem=f'{i:06d}'
        Image.new('RGB',(1920,1440),(120,120,120)).save(root/(stem+'.jpg'))
        depth.astype('<f4').tofile(root/(stem+'-depth.f32le'))
        confidence.tofile(root/(stem+'-confidence.u8'))
        record.update(rgb=asset(stem+'.jpg'),depth=asset(stem+'-depth.f32le'),confidence=asset(stem+'-confidence.u8'))
        started=time.monotonic();report=receiver.ingest(live_packet(root,record));elapsed.append(time.monotonic()-started)
    assert report['geometry_sufficient']
    assert report['nodes'][0]['held_out_p95_px']<1e-8
    assert not report['acquisition_complete'] and not report['calibration_validated']
    assert report['frames_stored']==6
    print('Full-resolution ingest max seconds:',max(elapsed))
