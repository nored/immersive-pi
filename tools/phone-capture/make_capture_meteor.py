#!/usr/bin/env python3
"""Experimental meteor clips baked through scan-derived wall mappings.

All projectors sample one shared cylindrical scene from reconstructed physical
wall points. Full native coverage uses extrapolated planar models. Provisional
normalized overlap weights are baked into clips, with no room-model edits.
"""
import argparse
from fractions import Fraction
import hashlib,json,math,subprocess,sys,time
from pathlib import Path
import cv2
import numpy as np
from PIL import Image
from make_capture_grid_test import compensate_baseline_mesh
from live.geometry import project
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'test-media'))
from make_native_meteor import Scene
from flight_scene import FlightScene,FallScene,PlungeScene
from video_scene import RealVideoScene


def file_sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def world_map(model,width,height):
    yy,xx=np.mgrid[:height,:width]
    uv=np.stack([xx.ravel(),yy.ravel()],axis=1)
    plane=project(np.linalg.inv(np.asarray(model['plane_to_projector'])),uv)
    return (plane@np.asarray(model['plane_axes'])+np.asarray(model['plane_origin'])).reshape(height,width,3)


def panorama_mapping(world,center,sign,offset,width,height,bottom,top):
    theta=np.arctan2(world[:,:,0]-center[0],world[:,:,2]-center[2])
    x=((sign*theta+offset)/(2*math.pi)%1*width).astype('f4')
    y=np.clip((top-world[:,:,1])/(top-bottom)*(height-1),0,height-1).astype('f4')
    return x,y


def equirectangular_mapping(world,center,sign,offset,width,original_height,crop_top,crop_height):
    theta=np.arctan2(world[:,:,0]-center[0],world[:,:,2]-center[2])
    elevation=np.arctan2(world[:,:,1]-center[1],np.hypot(world[:,:,0]-center[0],world[:,:,2]-center[2]))
    x=((sign*theta+offset)/(2*math.pi)%1*width).astype('f4')
    y=(.5-elevation/math.pi)*(original_height-1)-crop_top
    if np.min(y)<-1 or np.max(y)>crop_height:
        raise ValueError('Measured wall view exceeds the selected source latitude band')
    return x,np.clip(y,0,crop_height-1).astype('f4')


def coverage_weight(world,model,width,height):
    shape=world.shape[:2];points=world.reshape(-1,3)
    origin=np.asarray(model['plane_origin']);basis=np.asarray(model['plane_axes'],dtype=float)
    normal=np.cross(basis[0],basis[1]);normal/=np.linalg.norm(normal)
    distance=abs((points-origin)@normal)
    pixel=project(np.asarray(model['plane_to_projector']),(points-origin)@basis.T)
    u=pixel[:,0]/(width-1);v=pixel[:,1]/(height-1)
    edge=np.minimum.reduce([u,1-u,v,1-v])
    return np.where((edge>=0)&(distance<.25),np.maximum(edge,.002),0).reshape(shape).astype('f4')


def build(models,baseline,output,frames,*,flight=False,fall=False,plunge=False,video=None,start_seconds=0,hardware_video=False,gpu_map=False,source_projection='equirect'):
    output.mkdir(parents=True,exist_ok=False)
    entries={n['node']:n for n in baseline['nodes']};rows=models['models'];count=len(rows)
    width,height=1280,800;source_width=width*count
    center=np.mean([m['plane_origin'] for m in rows],axis=0)
    theta=np.array([math.atan2(m['plane_origin'][0]-center[0],m['plane_origin'][2]-center[2]) for m in rows])
    expected=np.array([2*math.pi*(entries[m['node']]['source_region']['x']+entries[m['node']]['source_region']['w']/2) for m in rows])
    choices=[]
    for sign in (-1,1):
        difference=np.exp(1j*(expected-sign*theta));mean=difference.mean()
        choices.append((abs(mean),sign,float(np.angle(mean))))
    coherence,sign,offset=max(choices)
    if coherence<.6:raise ValueError('Recovered projector arrangement does not support a coherent room wrap')
    worlds={m['node']:world_map(m,width,height) for m in rows}
    bottom=min(float(np.min(w[:,:,1])) for w in worlds.values());top=max(float(np.max(w[:,:,1])) for w in worlds.values())
    if not .5<top-bottom<8:raise ValueError('Unstable extrapolated vertical bounds')
    fps=Fraction(521875,17451)
    footage=RealVideoScene(video,frames,fps,start_seconds=start_seconds,hardware=hardware_video,projection=source_projection) if video else None
    maps={};weight_summary=[]
    for model in rows:
        node=model['node'];world=worlds[node]
        if footage:
            x,y=equirectangular_mapping(world,center,sign,offset,footage.width,footage.original_height,footage.crop_top,footage.height)
        else:
            x,y=panorama_mapping(world,center,sign,offset,source_width,height,bottom,top)
        own=coverage_weight(world,model,width,height);total=np.zeros_like(own)
        normal=np.cross(*np.asarray(model['plane_axes']))
        for other in rows:
            other_normal=np.cross(*np.asarray(other['plane_axes']))
            if abs(np.dot(normal,other_normal))>.9:total+=coverage_weight(world,other,width,height)
        weights=np.divide(own,total,out=np.ones_like(own),where=total>.00001)
        # A diagnostic gamma assumption, not measured radiometric calibration.
        gain=np.clip(weights,0,1)**(1/2.2)
        maps[node]=(compensate_baseline_mesh(x,entries[node]),compensate_baseline_mesh(y,entries[node]),
                    compensate_baseline_mesh(gain,entries[node]))
        weight_summary.append({'node':node,'overlap_fraction':float((weights<.99).mean()),'minimum_weight':float(weights.min())})
    del worlds
    mapping_device='CPU (OpenCV native)'
    if gpu_map:
        cv2.ocl.setUseOpenCL(True)
        if not cv2.ocl.useOpenCL():raise RuntimeError('OpenCL GPU mapping was requested but is unavailable')
        mapping_device=cv2.ocl.Device_getDefault().name()
        maps={node:(cv2.UMat(x),cv2.UMat(y),cv2.UMat(np.repeat(gain[:,:,None],3,axis=2))) for node,(x,y,gain) in maps.items()}
    else:
        maps={node:(x,y,np.repeat(gain[:,:,None],3,axis=2)) for node,(x,y,gain) in maps.items()}
    fps=Fraction(521875,17451)
    scene=footage or (PlungeScene if plunge else FallScene if fall else FlightScene if flight else Scene)(width,height,count,frames,panorama_width=source_width)
    prefix=('capture-video-'+video.stem[:24]+'-' if video else 'capture-plunge-' if plunge else 'capture-fall-' if fall else 'capture-flight-' if flight else 'capture-meteor-')+models['capture_id'][:8]
    encoders={};paths={}
    try:
        for model in rows:
            node=model['node'];path=output/(prefix+'-'+node+'.mp4');paths[node]=path
            codec=(['-c:v','h264_videotoolbox','-allow_sw','0','-b:v','4M','-profile:v','high','-g','60'] if hardware_video else ['-c:v','libx264','-preset','veryfast','-threads','1','-crf','20'])
            encoders[node]=subprocess.Popen(['ffmpeg','-nostdin','-v','error','-f','rawvideo','-pix_fmt','rgb24',
                '-s',f'{width}x{height}','-r',str(fps),'-i','pipe:0','-an']+codec+['-pix_fmt','yuv420p','-movflags','+faststart',str(path)],stdin=subprocess.PIPE,stderr=subprocess.PIPE)
        started=time.monotonic()
        for frame in range(frames):
            panorama=np.asarray(scene.render(frame))
            if gpu_map:panorama=cv2.UMat(panorama)
            # Enqueue all GPU views before any readback. Each node then reads
            # its finished result, avoiding a GPU fence after every kernel pair.
            rendered={}
            for model in rows:
                node=model['node'];x,y,gain=maps[node]
                sampled=cv2.remap(panorama,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP)
                rendered[node]=cv2.multiply(sampled,gain,dtype=cv2.CV_8U)
            for model in rows:
                node=model['node'];encoded=rendered[node]
                if gpu_map:encoded=encoded.get()
                encoders[node].stdin.write(encoded.tobytes())
                if frame==frames//3:Image.fromarray(encoded).save(output/('preview-'+node+'.jpg'))
            if frame%30==0:print(json.dumps({'rendered':frame+1,'frames':frames,'elapsed_s':round(time.monotonic()-started,1)}),flush=True)
        for process in encoders.values():process.stdin.close()
        for node,process in encoders.items():
            error=process.stderr.read().decode();code=process.wait()
            if code:raise RuntimeError(node+': '+error)
    except BaseException:
        for process in encoders.values():
            if process.poll() is None:process.kill()
        for process in encoders.values():process.wait(timeout=10)
        if footage:footage.close()
        raise
    if footage:footage.close()
    if hardware_video:
        from repair_h264_timing import ensure_timing
        timing=[ensure_timing(path,fps) for path in paths.values()]
        (output/'timing-repair-report.json').write_text(json.dumps(timing,indent=2)+'\n')
    package={'format':'immersive-tiled-v1','fps_num':fps.numerator,'fps_den':fps.denominator,'frames':frames,'nodes':{}}
    for model in rows:
        node=model['node'];path=paths[node]
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
            '-show_entries','stream=width,height,nb_frames,r_frame_rate','-of','json',str(path)]))['streams'][0]
        assert (probe['width'],probe['height'],int(probe['nb_frames']),Fraction(probe['r_frame_rate']))==(width,height,frames,fps)
        package['nodes'][node]={'file':path.name,'sha256':file_sha256(path),
            'width':width,'height':height,'region':entries[node]['source_region']}
    (output/(prefix+'.immersive.json')).write_text(json.dumps(package,indent=2)+'\n')
    report={'capture_id':models['capture_id'],'mapping':'Shared reconstructed wall positions to one cylindrical panorama',
        'camera_motion':'Real recorded footage; original camera motion' if video else 'Accelerating descent through a dense enclosing rock chasm, close debris and upward particle streaks; fixed horizon' if plunge else 'Fast downward observer translation; upward asteroid flow and slower depth-layered moving/twinkling stars; fixed horizon' if fall else 'Forward observer translation through a stationary asteroid field; fixed horizon, periodic course' if flight else 'Stationary observer with moving asteroids',
        'nodes':count,'frames':frames,'native_output':[width,height],'observer_center_world':center.tolist(),
        'direction_sign':sign,'angular_offset_radians':offset,'layout_coherence':coherence,
        'estimated_vertical_bounds_m':[bottom,top],'unmeasured_edges':'Planar extrapolation included',
        'blend':'Provisional geometry-normalized overlap weights; assumed gamma 2.2','weights':weight_summary,
        'calibration_validated':False,'room_model_changed':False,'manifest':prefix+'.immersive.json',
        'video_acceleration':'Apple VideoToolbox encode' if hardware_video else 'Software encode',
        'mapping_device':mapping_device,'source_hardware_decode':bool(footage and footage.hardware_decode)}
    if footage:report['source']={'file':str(video),'projection':source_projection,'source_dimensions':[footage.width,footage.original_height],'start_seconds':start_seconds,'sha256':file_sha256(video)}
    (output/'mapping-report.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--models',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--frames',type=int,default=360);p.add_argument('--flight',action='store_true');p.add_argument('--fall',action='store_true');p.add_argument('--plunge',action='store_true');p.add_argument('--video',type=Path);p.add_argument('--start-seconds',type=float,default=0);p.add_argument('--hardware-video',action='store_true');p.add_argument('--gpu-map',action='store_true');p.add_argument('--source-projection',choices=('equirect','eac'),default='equirect');a=p.parse_args()
    build(json.loads(a.models.read_text()),json.loads(a.baseline.read_text()),a.output,a.frames,flight=a.flight,fall=a.fall,plunge=a.plunge,video=a.video,start_seconds=a.start_seconds,hardware_video=a.hardware_video,gpu_map=a.gpu_map,source_projection=a.source_projection)
