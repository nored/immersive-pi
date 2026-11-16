#!/usr/bin/env python3
"""Bake a reversible native-video grid test from measured projector mappings.

Room calibration is not changed. Pixels outside observed QR coverage are black.
Lines represent quarter-metre levels in the draft reconstructed coordinate
system; this is an experimental visual check, not validated calibration.
"""
import argparse,hashlib,json,subprocess
from collections import defaultdict
from pathlib import Path
import cv2
import numpy as np
from PIL import Image,ImageDraw
from review_pose_segments import review
from live.geometry import homography,project


def grid_image(h,origin,basis,support,width,height):
    yy,xx=np.mgrid[:height,:width]
    pixels=np.stack([xx.ravel(),yy.ravel()],axis=1)
    plane=project(np.linalg.inv(h),pixels)
    world=(plane@basis+origin).reshape(height,width,3)
    horizontal=int(np.argmax(np.linalg.norm(basis[:,[0,2]],axis=0)))
    coordinate=0 if horizontal==0 else 2
    image=np.zeros((height,width,3),dtype='u1')
    for axis,intensity in [(coordinate,100),(1,230)]:
        value=world[:,:,axis]
        distance=abs((value+.125)%.25-.125)
        dy,dx=np.gradient(value);pixel_scale=np.hypot(dx,dy)
        line=distance<1.2*np.maximum(pixel_scale,1e-7)
        image[line]=np.maximum(image[line],intensity)
    mask=np.zeros((height,width),dtype='u1')
    hull=cv2.convexHull(np.asarray(support,dtype='f4')).round().astype('i4')
    cv2.fillConvexPoly(mask,hull,255)
    image[mask==0]=0
    return image,float((mask>0).mean())


def compensate_baseline_mesh(image,entry):
    """Compose the desired output image with the existing two-triangle mesh.

    This bakes compensation into a diagnostic clip, leaving room settings alone.
    """
    height,width=image.shape[:2]
    points=entry['mesh']['points']
    assert entry['mesh']['cols']==2 and entry['mesh']['rows']==2
    assert [(p['u'],p['v']) for p in points]==[(0,0),(1,0),(0,1),(1,1)]
    v,u=np.mgrid[:height,:width].astype('f4');u/=width-1;v/=height-1
    xy=np.asarray([[p['x'],p['y']] for p in points],dtype='f4')
    # Same diagonal as gl_pipeline: TL/TR/BR and TL/BR/BL.
    first=(1-u)[...,None]*xy[0]+(u-v)[...,None]*xy[1]+v[...,None]*xy[3]
    second=(1-v)[...,None]*xy[0]+u[...,None]*xy[3]+(v-u)[...,None]*xy[2]
    output=np.where((u>=v)[...,None],first,second)
    map_x=(output[:,:,0]+1)*(width-1)/2;map_y=(1-output[:,:,1])*(height-1)/2
    return cv2.remap(image,map_x,map_y,cv2.INTER_LINEAR,borderMode=cv2.BORDER_CONSTANT)


def build(root,alignment,baseline,output):
    output.mkdir(parents=True,exist_ok=False)
    _,groups=review(root,with_groups=True)
    transforms=alignment['transforms'];model_entries={n['node']:n for n in baseline['nodes']}
    metadata=json.loads((root/'phone-session.json').read_text())
    targets={n['node']:n for n in metadata['backend_scan']['layout']['nodes']}
    gathered=defaultdict(lambda:{'training':defaultdict(list),'held':[]})
    for (segment,node),views in groups.items():
        if str(segment) not in transforms:continue
        transform=transforms[str(segment)];r=np.asarray(transform['rotation']);t=np.asarray(transform['translation_m'])
        for i,v in enumerate(views):
            for cell,(world,pixels) in v['cells'].items():
                xyz=world@r.T+t
                if i%2==0:
                    for corner,point in enumerate(xyz):gathered[node]['training'][(cell,corner)].append((point,pixels[corner]))
                else:gathered[node]['held'].append((xyz,pixels))
    package={'format':'immersive-tiled-v1','fps_num':30,'fps_den':1,'frames':90,'nodes':{}}
    models=[];previews=[]
    for node,entry in model_entries.items():
        # These images are prewarped for the baseline native output. Refuse
        # a different active mesh instead of silently applying a second warp.
        target=targets[node];width,height=target['width'],target['height']
        rows=gathered[node]['training'];keys=sorted(rows)
        if len(keys)<12:raise ValueError(node+': too few measured points for a draft test')
        xyz=np.asarray([np.median([p[0] for p in rows[key]],axis=0) for key in keys])
        uv=np.asarray([rows[key][0][1] for key in keys])
        origin=xyz.mean(0);_,_,basis=np.linalg.svd(xyz-origin,full_matrices=False);basis=basis[:2]
        h=homography((xyz-origin)@basis.T,uv)
        held=gathered[node]['held']
        errors=np.concatenate([np.linalg.norm(project(h,(world-origin)@basis.T)-pixels,axis=1) for world,pixels in held]) if held else np.array([])
        image,coverage=grid_image(h,origin,basis,uv,width,height)
        image=compensate_baseline_mesh(image,entry)
        picture=Image.fromarray(image);draw=ImageDraw.Draw(picture)
        draw.rectangle((8,8,420,46),fill=(0,0,0));draw.text((15,18),'DRAFT MEASURED GRID / '+node,fill=(180,180,180))
        stem='capture-grid-'+node;png=output/(stem+'.png');mp4=output/(stem+'.mp4');picture.save(png)
        subprocess.run(['ffmpeg','-nostdin','-v','error','-loop','1','-framerate','30','-i',str(png),
                        '-frames:v','90','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p',
                        '-movflags','+faststart',str(mp4)],check=True)
        package['nodes'][node]={'file':mp4.name,'sha256':hashlib.sha256(mp4.read_bytes()).hexdigest(),
                               'width':width,'height':height,'region':entry['source_region']}
        models.append({'node':node,'training_cells':sorted({key[0] for key in keys}),
                       'observed_hull_output_fraction':coverage,'held_out_p95_px':float(np.percentile(errors,95)) if len(errors) else None,
                       'plane_origin':origin.tolist(),'plane_axes':basis.tolist(),'plane_to_projector':h.tolist()})
        picture.thumbnail((384,240));previews.append((node,picture.copy()))
    (output/'capture-grid-draft.immersive.json').write_text(json.dumps(package,indent=2)+'\n')
    (output/'draft-models.json').write_text(json.dumps({'capture_id':metadata['session_id'],'room_version':baseline['version'],
        'models':models,'calibration_validated':False,'room_model_changed':False,
        'limitation':'Diagnostic prewarped grid; sparse edges masked, no radiometric blending, coordinates from draft QR-anchor registration.'},indent=2)+'\n')
    sheet=Image.new('RGB',(384*3,270*4),(15,15,15));draw=ImageDraw.Draw(sheet)
    for i,(node,picture) in enumerate(previews):
        x=(i%3)*384;y=(i//3)*270;sheet.paste(picture,(x,y+24));draw.text((x+8,y+6),node,fill='white')
    sheet.save(output/'grid-contact-sheet.jpg')
    print(json.dumps({'nodes':len(models),'output':str(output),'models':[{'node':m['node'],'p95_px':m['held_out_p95_px'],'coverage':m['observed_hull_output_fraction']} for m in models]}))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--alignment',type=Path,required=True);p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    build(a.directory,json.loads(a.alignment.read_text()),json.loads(a.baseline.read_text()),a.output)
