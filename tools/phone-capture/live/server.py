#!/usr/bin/env python3
"""Local live scan receiver/quality evaluator. Never sends room commands.

Run with --advertise-address WIFI_IP to announce to the phone via Bonjour.
The output directory contains selected full-resolution measurements, not a
browser video feed. No external AI service receives these measurements.
"""
import argparse
import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import signal
import sys
import tempfile
import uuid

from PIL import Image
import numpy as np
import websockets

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from validate_capture import inspect_frame,local_file,asset
from geometry import LiveGeometry

MAX_ASSET=3*1024*1024


def atomic_json(path,value):
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,allow_nan=False,indent=2)+'\n')
    os.replace(temporary,path)


class LiveSession:
    def __init__(self,root,metadata):
        identifier=str(uuid.UUID(metadata['session_id']))
        if metadata.get('format')!='immersive-arkit-capture-v1':raise ValueError('unsupported phone capture')
        conventions={'matrix_storage':'row_major','pose_convention':'ARKit_camera_to_world_meters',
                     'intrinsics_coordinates':'native_RGB_pixels','rgb_orientation':'native_sensor_unrotated',
                     'camera_axes':'x_right_y_up_z_back; forward_is_negative_z',
                     'depth_convention':'ARKit_sceneDepth_axial_meters','world_alignment':'gravity'}
        if any(metadata.get(k)!=v for k,v in conventions.items()):raise ValueError('unsupported camera conventions')
        setup=metadata['backend_scan']
        if str(uuid.UUID(setup['capture_id']))!=identifier:raise ValueError('capture identity mismatch')
        if setup.get('protocol')!=1:raise ValueError('unsupported capture protocol')
        layout=setup['layout']
        if not layout.get('nodes') or len(layout['nodes'])>128:raise ValueError('invalid projector layout')
        if layout.get('layout_id')!=setup['session'][:16]:raise ValueError('layout/session mismatch')
        self.directory=root/identifier
        self.directory.mkdir(parents=True,exist_ok=False)
        self.metadata=metadata;self.frames=[];self.last_index=-1;self.status="receiving"
        self.engine=LiveGeometry(layout)
        atomic_json(self.directory/'phone-session.json',metadata)
        self.persist()

    def persist(self):
        atomic_json(self.directory/'live-index.json',{'format':'immersive-live-capture-v1',
            'session_id':self.metadata['session_id'],'status':self.status,'frames':self.frames,
            'selected_frames_only':True,'calibration_validated':False})

    def ingest(self,packet):
        record=packet['record'];index=record['index']
        if type(index) is not int or index<=self.last_index or index>1000000:raise ValueError('stale or invalid frame index')
        name=f'{index:06d}.json'
        assets=packet['assets']
        if not isinstance(assets,dict):raise ValueError('missing frame assets')
        with tempfile.TemporaryDirectory(prefix='.incoming-',dir=self.directory) as staging:
            temporary=Path(staging)
            expected={}
            for key in ('rgb','depth','confidence'):
                info=record.get(key)
                if info is None:continue
                filename=info['file']
                if not isinstance(filename,str) or Path(filename).name!=filename or not filename.startswith(f'{index:06d}') or filename.endswith('.json'):
                    raise ValueError('invalid asset filename')
                if filename in expected:raise ValueError('duplicate asset filename')
                if type(info['bytes']) is not int or not 0<info['bytes']<=MAX_ASSET:raise ValueError('asset too large')
                encoded=assets.get(filename)
                if not isinstance(encoded,str) or len(encoded)>4*MAX_ASSET//3+4:raise ValueError('missing or oversized asset')
                data=base64.b64decode(encoded,validate=True)
                if len(data)!=info['bytes'] or hashlib.sha256(data).hexdigest()!=info['sha256']:raise ValueError('asset integrity mismatch')
                expected[filename]=data
            if set(assets)!=set(expected) or 'rgb' not in record:raise ValueError('unexpected or missing assets')
            for filename,data in expected.items():(temporary/filename).write_bytes(data)
            atomic_json(temporary/name,record)
            # Validates K/rigid pose, display receipts, timestamps and depth sizes;
            # dark references remain archived even when geometrically ineligible.
            inspected=inspect_frame(temporary,name,self.metadata['backend_scan'])
            if self.frames and record['timestamp_s']<=self.frames[-1]['timestamp_s']:raise ValueError('nonmonotonic frame timestamp')
            if record.get('depth') and record.get('confidence'):
                dw,dh=record['depth_resolution']
                depth=np.frombuffer(asset(temporary,record['depth']),dtype='<f4').reshape(dh,dw)
                confidence=np.frombuffer(asset(temporary,record['confidence']),dtype='u1').reshape(dh,dw)
                geometry_record=record if inspected['capture_eligible'] else {**record,'recognized_projector_markers':[]}
                report=self.engine.add(geometry_record,depth,confidence)
            else:
                report=self.engine.report();report['instruction']='Move slowly while LiDAR depth becomes available.'
            with Image.open(io.BytesIO(asset(temporary,record['rgb']))) as image:
                image.thumbnail((160,120));rgb=np.asarray(image.convert('RGB'),dtype=float)
            bright=rgb.mean(2)>30
            red=(rgb[:,:,0]>1.5*np.maximum(rgb[:,:,1],rgb[:,:,2])) & (rgb[:,:,0]>40) & bright
            report['camera_diagnostics']={'mean_rgb':rgb.mean((0,1)).tolist(),
                'red_fraction_of_bright_pixels':float(red.sum()/max(1,bright.sum())),
                'exposure_duration_s':record.get('exposure_duration_s'),
                'exposure_offset':record.get('exposure_offset'),
                'projection_colour_verified':False}
            for filename in expected:os.replace(temporary/filename,self.directory/filename)
            os.replace(temporary/name,self.directory/name)
        self.last_index=index
        self.frames.append({'file':name,'index':index,'timestamp_s':record['timestamp_s'],
                            'eligible':inspected['capture_eligible']})
        self.persist()
        report.update(frame_index=index,frames_stored=len(self.frames),session_id=self.metadata['session_id'])
        atomic_json(self.directory/'quality.json',report)
        return report


async def serve(args):
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=True)
    active={}
    async def protocol(ws):
        live=None
        async for raw in ws:
            request_id=None
            try:
                packet=json.loads(raw);request_id=packet.get('request_id')
                if packet.get('cmd')=='analysis_begin':
                    if live:raise ValueError('session already started')
                    live=await asyncio.to_thread(LiveSession,root,packet['metadata'])
                    active[ws]=live
                    response={'type':'analysis_started','session_id':live.metadata['session_id']}
                elif packet.get('cmd')=='analysis_frame':
                    if not live or packet.get('session_id')!=live.metadata['session_id']:raise ValueError('no matching live session')
                    response=await asyncio.to_thread(live.ingest,packet)
                else:raise ValueError('unknown analysis command')
                await ws.send(json.dumps({**response,'request_id':request_id},allow_nan=False))
            except (ValueError,KeyError,TypeError,OSError) as exc:
                await ws.send(json.dumps({'type':'analysis_error','request_id':request_id,'message':str(exc)}))
    async def handler(ws):
        try:
            await protocol(ws)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            live=active.pop(ws,None)
            if live:
                live.status='disconnected'
                await asyncio.to_thread(live.persist)
    advertiser=None
    if args.advertise_address:
        from zeroconf import Zeroconf,ServiceInfo
        advertiser=Zeroconf()
        info=ServiceInfo('_immersive-scan._tcp.local.','Immersive live analysis._immersive-scan._tcp.local.',
            addresses=[socket.inet_aton(args.advertise_address)],port=args.port,
            properties={'protocol':'1'},server=socket.gethostname().split('.')[0]+'.local.')
        await asyncio.to_thread(advertiser.register_service,info)
    try:
        async with websockets.serve(handler,args.host,args.port,compression=None,max_size=8*1024*1024):
            print(json.dumps({'listening':args.port,'pid':os.getpid(),'output':str(root),'bonjour':bool(advertiser)}),flush=True)
            stop=asyncio.Event()
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)
            await stop.wait()
    finally:
        if advertiser:await asyncio.to_thread(advertiser.close)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=8790)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--advertise-address')
    asyncio.run(serve(parser.parse_args()))
