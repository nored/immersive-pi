#!/usr/bin/env python3
"""Measure CPU/OpenCL remap and blend, including CPU readback for encoding."""
import argparse,json,time
from fractions import Fraction
from pathlib import Path
import cv2,numpy as np
from make_capture_meteor import world_map,equirectangular_mapping
from video_scene import RealVideoScene


def benchmark(video,model_file):
    rows=json.loads(model_file.read_text())['models'];center=np.mean([r['plane_origin'] for r in rows],axis=0)
    source=RealVideoScene(video,1,Fraction(30),start_seconds=60,hardware=True)
    try:panorama=source.render(0)
    finally:source.close()
    maps=[]
    for row in rows:
        world=world_map(row,1280,800)
        x,y=equirectangular_mapping(world,center,-1,1.163066236467986,source.width,source.original_height,source.crop_top,source.height)
        yy,xx=np.mgrid[:800,:1280];gain=(.3+.7*xx/1279).astype('f4')
        maps.append((x,y,gain,np.repeat(gain[:,:,None],3,axis=2)))
    cv2.ocl.setUseOpenCL(True)
    device=cv2.ocl.Device_getDefault().name()
    gpu_maps=[(cv2.UMat(x),cv2.UMat(y),cv2.UMat(g3)) for x,y,g,g3 in maps]
    def cpu_numpy():
        return [np.clip(cv2.remap(panorama,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP).astype('f4')*g[:,:,None],0,255).astype('u1') for x,y,g,g3 in maps]
    def cpu_native():
        return [cv2.multiply(cv2.remap(panorama,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP),g3,dtype=cv2.CV_8U) for x,y,g,g3 in maps]
    def gpu():
        image=cv2.UMat(panorama)
        return [cv2.multiply(cv2.remap(image,x,y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_WRAP),g,dtype=cv2.CV_8U).get() for x,y,g in gpu_maps]
    original=cpu_numpy();optimized=cpu_native();accelerated=gpu()
    delta=[np.abs(a.astype('i2')-b.astype('i2')) for a,b in zip(original,accelerated)]
    report={'device':device,'opencl_enabled':cv2.ocl.useOpenCL(),'tiles':len(maps),
            'gpu_max_rgb_difference':int(max(d.max() for d in delta)),
            'gpu_mean_rgb_difference':float(np.mean([d.mean() for d in delta]))}
    for name,fn in [('cpu_numpy',cpu_numpy),('cpu_native',cpu_native),('opencl_gpu',gpu)]:
        times=[]
        for _ in range(12):
            start=time.monotonic();fn();times.append(time.monotonic()-start)
        report[name]={'median_ms':float(np.median(times)*1000),'wall_frames_per_second':float(1/np.median(times))}
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--video',type=Path,required=True);p.add_argument('--models',type=Path,required=True);p.add_argument('--out',type=Path,required=True);a=p.parse_args();r=benchmark(a.video,a.models);a.out.write_text(json.dumps(r,indent=2)+'\n');print(json.dumps(r))
