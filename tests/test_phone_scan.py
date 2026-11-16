import asyncio
import copy
import json
from types import SimpleNamespace
import uuid

import pytest

from shared.phone_scan import Coverage, camera_position, make_targets
import phone_scan


def layout_for(nodes):
    return {'protocol':1,'layout_id':'0123456789abcdef','cols':4,'rows':3,
            'nodes':[{'node':node['node'],'width':node['width'],'height':node['height'],
                      'file':f'target-{i}.png','sha256':'a'*64,
                      'markers':[{'cell':j,'row':j//4,'col':j%4,'payload':f'IC1|0123456789abcdef|{i}|{j}'} for j in range(12)]}
                     for i,node in enumerate(nodes)]}


def observation(layout,index=0,x=0):
    pose=[1,0,0,x,0,1,0,0,0,0,1,0,0,0,0,1]
    markers=[]
    for m in layout['nodes'][0]['markers']:
        a,b=10+(m['cell']%4)*40,10+(m['cell']//4)*40
        markers.append({'payload':m['payload'],'corners_px':[[a,b],[a+20,b],[a+20,b+20],[a,b+20]]})
    return {'frame_index':index,'timestamp_s':10+index,'camera_to_world':pose,
            'image_resolution':[1920,1440],'tracking':'normal','eligible':True,'markers':markers}


def test_coverage_requires_spatially_distinct_views_and_never_claims_calibration():
    layout=layout_for([{'node':'arbitrary-projector','width':1280,'height':800}])
    tracker=Coverage(layout)
    tracker.observe(observation(layout),phase_kind='overview')
    tracker.observe(observation(layout,1,.19),phase_kind='overview')
    row=tracker.snapshot()['nodes'][0]
    assert row['state']=='needs_more_views' and row['markers_seen']==12
    assert row['markers_with_multiple_views']==0
    tracker.observe(observation(layout,2,.21),phase_kind='fiducials',target='arbitrary-projector')
    result=tracker.snapshot()
    assert result['nodes'][0]['state']=='coverage_recorded'
    assert result['nodes'][0]['markers_with_multiple_views']==12
    assert result['calibration_validated'] is False


@pytest.mark.parametrize('kind',['unknown','duplicate','pose','corner','nonconvex','tracking','timestamp','wrong_projector','wrong_phase'])
def test_invalid_coverage_packets_cannot_partially_accumulate_evidence(kind):
    layout=layout_for([{'node':'node-99','width':1280,'height':800}]);tracker=Coverage(layout)
    packet=observation(layout);phase='fiducials';target='node-99'
    if kind=='unknown':packet['markers'][-1]['payload']='stale-layout'
    elif kind=='duplicate':packet['markers'].append(packet['markers'][0])
    elif kind=='pose':packet['camera_to_world'][0]=2
    elif kind=='corner':packet['markers'][-1]['corners_px'][0][0]=float('nan')
    elif kind=='nonconvex':packet['markers'][-1]['corners_px'][1],packet['markers'][-1]['corners_px'][2]=packet['markers'][-1]['corners_px'][2],packet['markers'][-1]['corners_px'][1]
    elif kind=='tracking':packet['tracking']='limited'
    elif kind=='timestamp':packet['timestamp_s']=float('inf')
    elif kind=='wrong_projector':target='another-node'
    else:phase='white'
    before=copy.deepcopy(tracker.snapshot())
    with pytest.raises(ValueError):tracker.observe(packet,phase_kind=phase,target=target)
    assert tracker.snapshot()==before and tracker.last_frame==-1


def test_replayed_frame_cannot_increase_coverage():
    layout=layout_for([{'node':'n','width':1280,'height':800}]);tracker=Coverage(layout)
    packet=observation(layout);tracker.observe(packet,phase_kind='overview')
    packet['camera_to_world'][3]=1
    with pytest.raises(ValueError,match='stale'):tracker.observe(packet,phase_kind='overview')
    assert tracker.snapshot()['nodes'][0]['markers_with_multiple_views']==0


class FakeController:
    def __init__(self,path,n=3):
        names=[f'node-{i*7+1}' for i in range(n)]
        self.room=SimpleNamespace(nodes=lambda:names,model={'version':34,'nodes':[{'node':name} for name in names]})
        self.nodes={name:object() for name in names}
        self.node_info={name:{'capabilities':['phone_scan_v1']} for name in names}
        self.heartbeats={name:{'fb_ok':True,'graphics':{'output_width':1280,'output_height':800}} for name in names}
        self.media_dir=path;self.power_state='awake';self.messages=[];self.commands=[];self.stops=0
        self.ack=True;self.corrupt=False;self.pattern_sent=asyncio.Event()
        self.phone_scan=phone_scan.PhoneScan(self);self.phone_scan.phase_timeout=.05

    async def cmd_stop(self):self.stops+=1
    async def _send(self,owner,message):self.messages.append((owner,message))
    async def _send_node(self,node,command):
        self.commands.append((node,command));self.pattern_sent.set()
        if self.ack and command['kind']!='clear':
            ack={**command,'type':'capture_displayed','flip_monotonic_ns':123456}
            if self.corrupt:ack['sha256']='wrong'
            self.phone_scan.receipt(node,ack)


@pytest.fixture
def fake_targets(monkeypatch):
    monkeypatch.setattr(phone_scan,'make_targets',lambda directory,session,nodes:layout_for(nodes))


@pytest.mark.parametrize('n',[1,3,13])
def test_phone_session_waits_for_all_displays_and_preserves_room_config(tmp_path,fake_targets,n):
    async def run():
        ctl=FakeController(tmp_path,n);manager=ctl.phone_scan;owner=object()
        before=copy.deepcopy(ctl.room.model)
        await manager.process(owner,{'cmd':'capture_begin','capture_id':str(uuid.uuid4()),'request_id':'begin'})
        state=manager.state;started=ctl.messages[-1][1]
        assert started['type']=='capture_started' and started['context']['ready']
        assert len(started['context']['display_acks'])==n and ctl.stops==1
        selected=ctl.room.nodes()[-1]
        await manager.process(owner,{'cmd':'capture_phase','session':state['session'],'kind':'white','node':selected})
        phase=ctl.commands[-n:]
        assert [(node,c['kind']) for node,c in phase]==[(node,'white' if node==selected else 'black') for node in ctl.room.nodes()]
        await manager.process(owner,{'cmd':'capture_end','session':state['session']})
        assert not manager.active and ctl.room.model==before
        assert all(c['kind']=='clear' for _,c in ctl.commands[-n:])
        events=[json.loads(line) for line in state['journal'].read_text().splitlines()]
        assert [e['event'] for e in events]==['begin','phase_request','phase_ready','phase_request','phase_ready','end']
    asyncio.run(run())


def test_missing_capture_capability_cannot_stop_video(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);ctl.node_info[ctl.room.nodes()[0]]['capabilities']=[]
        with pytest.raises(ValueError,match='all configured'):
            await ctl.phone_scan.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        assert ctl.stops==0 and not ctl.commands and not ctl.phone_scan.active
    asyncio.run(run())


def test_projectors_reported_off_cannot_start_a_mislabeled_scan(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path)
        async def refresh():return None
        ctl.devices=SimpleNamespace(base='http://room-api',refresh=refresh,blockers=lambda:['9: aus'])
        with pytest.raises(ValueError,match='not confirmed ready'):
            await ctl.phone_scan.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        assert ctl.stops==0 and not ctl.commands and not ctl.phone_scan.active
    asyncio.run(run())


def test_nonowner_cannot_change_existing_scan_and_bad_receipts_never_ready(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);manager=ctl.phone_scan;owner=object()
        await manager.process(owner,{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        session=manager.state['session'];count=len(ctl.commands)
        with pytest.raises(ValueError,match='matching'):
            await manager.process(object(),{'cmd':'capture_phase','session':session,'kind':'white','node':ctl.room.nodes()[0]})
        assert manager.active and len(ctl.commands)==count
        ctl.corrupt=True
        with pytest.raises(ValueError,match='does not match'):
            await manager.process(owner,{'cmd':'capture_phase','session':session,'kind':'fiducials','node':ctl.room.nodes()[0]})
        assert not manager.active
        assert not any(m.get('type')=='capture_phase' for _,m in ctl.messages)
    asyncio.run(run())


def test_missing_display_ack_times_out_and_restores_every_node(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);ctl.ack=False
        with pytest.raises(ValueError):
            await ctl.phone_scan.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        assert not ctl.phone_scan.active
        assert all(c['kind']=='clear' for _,c in ctl.commands[-3:])
        assert not any(m.get('type')=='capture_started' for _,m in ctl.messages)
    asyncio.run(run())


def test_external_stop_interrupts_an_inflight_phase_without_deadlock(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);ctl.ack=False;ctl.phone_scan.phase_timeout=10
        begin=asyncio.create_task(ctl.phone_scan.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())}))
        await ctl.pattern_sent.wait()
        await asyncio.wait_for(ctl.phone_scan.cancel('operator stopped room'),1)
        with pytest.raises(ValueError,match='operator stopped'):await begin
        assert not ctl.phone_scan.active
    asyncio.run(run())


def test_node_pattern_change_invalidates_an_acknowledged_capture(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);manager=ctl.phone_scan
        await manager.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        manager.display_status(ctl.room.nodes()[0],None)
        assert manager.state['context']['ready'] is False
        await asyncio.sleep(0)
        assert not manager.active
    asyncio.run(run())


def test_renderer_acknowledges_only_after_the_native_scanout_flip():
    from agent import RenderNode
    import queue
    node=RenderNode.__new__(RenderNode)
    node.node='test-node';node._running=True;node._presenter=None;node._standby=False
    node._capture_display=None;node._last_presented_frame=None
    node._cmd_q=queue.Queue();node._out_q=queue.Queue();node.trace=SimpleNamespace(path=None)
    node._frame_count=0;node.hb_hz=1;node._emit_heartbeat=lambda:None
    uploaded=[]
    def render():assert node._out_q.empty()  # A render submission is not a receipt.
    node.stage=SimpleNamespace(width=64,height=32,upload_raw=lambda *args:uploaded.append(args),render_raw=render)
    def flip():
        assert node._out_q.empty()
        node._running=False
        return 987654321
    node.display=SimpleNamespace(make_current=lambda:None,swap_and_flip=flip)
    node._cmd_q.put({'cmd':'capture_pattern','session':'a'*32,'phase':3,'kind':'white','width':64,'height':32})
    node.render_loop()
    ack=node._out_q.get_nowait()
    assert ack['type']=='capture_displayed' and ack['flip_monotonic_ns']==987654321
    assert ack['phase']==3 and ack['kind']=='white' and uploaded


def test_qr_target_roundtrip_has_unique_native_pixel_ownership(tmp_path):
    import sys,shutil,subprocess
    from pathlib import Path
    pytest.importorskip('cv2')
    if sys.platform!='darwin' or not shutil.which('swift'):pytest.skip('Native Vision probe requires macOS and Swift')
    layout=make_targets(tmp_path,'1234567890abcdef'*2,[{'node':'wall-west-73','width':1280,'height':800}])
    script=Path(__file__).resolve().parents[1]/'tools/phone-capture/tests/vision_probe.swift'
    output=subprocess.run(['swift',str(script),str(tmp_path/layout['nodes'][0]['file'])],check=True,capture_output=True,text=True,timeout=30)
    decoded=json.loads(output.stdout)
    assert len(decoded)==12
    expected={m['payload']:m for m in layout['nodes'][0]['markers']}
    assert {m['payload'] for m in decoded}==set(expected)
    import numpy as np
    for observation in decoded:
        corners=np.array(observation['corners']);corners[:,0]*=1280;corners[:,1]=(1-corners[:,1])*800
        assert np.max(np.abs(np.array(expected[observation['payload']]['projector_corners_px'])-corners))<2


def test_late_display_failure_invalidates_ready_phase_immediately(tmp_path,fake_targets):
    async def run():
        ctl=FakeController(tmp_path);manager=ctl.phone_scan
        await manager.process(object(),{'cmd':'capture_begin','capture_id':str(uuid.uuid4())})
        state=manager.state
        assert state['context']['ready']
        manager.receipt(ctl.room.nodes()[0],{'type':'capture_error','session':state['session'],
            'phase':state['phase'],'error':'DRM flip failed'})
        assert state['context']['ready'] is False
        await asyncio.sleep(0)
        assert not manager.active
        assert any(m.get('type')=='capture_aborted' and 'DRM flip failed' in m['reason'] for _,m in ctl.messages)
        assert all(c['kind']=='clear' for _,c in ctl.commands[-3:])
    asyncio.run(run())
