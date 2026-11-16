#!/usr/bin/env python3
"""Draft rigid registration across pose jumps using common projector QR corners.

Uses training viewpoint groups only for registration; reports separate held-out
corner disagreement. Does not optimize poses, apply room calibration, fill
missing coverage or certify seams. Disconnected segments remain disconnected.
"""
import argparse,json
from collections import defaultdict
from pathlib import Path
import numpy as np
from review_pose_segments import review
from reconstruct_capture import unproject
from validate_capture import asset


def rigid(source,target):
    center=source.mean(0);destination=target.mean(0)
    if np.linalg.matrix_rank(source-center,tol=1e-5)<2:raise ValueError('collinear anchors')
    u,_,vt=np.linalg.svd((source-center).T@(target-destination))
    rotation=vt.T@u.T
    if np.linalg.det(rotation)<0:vt[-1]*=-1;rotation=vt.T@u.T
    return rotation,destination-rotation@center


def robust_rigid(source,target,*,threshold=.08):
    if len(source)<12:raise ValueError('insufficient common anchors')
    rng=np.random.default_rng(1701);best=None
    for _ in range(240):
        choice=rng.choice(len(source),3,replace=False)
        try:r,t=rigid(source[choice],target[choice])
        except ValueError:continue
        errors=np.linalg.norm(source@r.T+t-target,axis=1);inliers=errors<threshold
        score=int(inliers.sum())
        if score>=12 and (best is None or score>best[0]):best=(score,inliers)
    if best is None:raise ValueError('no supported rigid registration')
    inliers=best[1]
    for _ in range(3):
        r,t=rigid(source[inliers],target[inliers]);errors=np.linalg.norm(source@r.T+t-target,axis=1)
        inliers=errors<threshold
        if inliers.sum()<12:raise ValueError('unstable registration')
    return r,t,inliers,errors


def align(root):
    segmented,groups=review(root,with_groups=True)
    anchors=defaultdict(lambda:defaultdict(list));held=defaultdict(lambda:defaultdict(list))
    for (segment,node),views in groups.items():
        for i,v in enumerate(views):
            destination=anchors if i%2==0 else held
            for cell,(world,_) in v['cells'].items():
                for corner,point in enumerate(world):destination[segment][(node,cell,corner)].append(point)
    means={s:{key:np.median(points,axis=0) for key,points in rows.items()} for s,rows in anchors.items()}
    transforms={0:(np.eye(3),np.zeros(3))};edges=[]
    while True:
        candidates=[]
        for parent in transforms:
            for child in means.keys()-transforms.keys():
                keys=sorted(means[parent].keys()&means[child].keys())
                if len(keys)<12:continue
                source=np.asarray([means[child][k] for k in keys]);target=np.asarray([means[parent][k] for k in keys])
                try:r,t,inliers,error=robust_rigid(source,target)
                except ValueError:continue
                supported={keys[i][:2] for i in np.where(inliers)[0]}
                if len(supported)<3:continue
                candidates.append((int(inliers.sum()),parent,child,r,t,keys,inliers,error))
        if not candidates:break
        _,parent,child,r,t,keys,inliers,error=max(candidates,key=lambda c:c[0])
        pr,pt=transforms[parent];transforms[child]=(pr@r,pr@t+pt)
        validation=[]
        for key,points in held.get(child,{}).items():
            if key in means[parent]:validation.extend(np.linalg.norm(np.asarray(points)@r.T+t-means[parent][key],axis=1))
        edges.append({'parent':parent,'child':child,'common_corners':len(keys),'inlier_corners':int(inliers.sum()),
            'anchor_markers':len({keys[i][:2] for i in np.where(inliers)[0]}),
            'training_p95_m':float(np.percentile(error[inliers],95)),
            'held_out_points':len(validation),'held_out_p95_m':float(np.percentile(validation,95)) if validation else None})
    index=json.loads((root/'live-index.json').read_text());cloud=[]
    cutoffs=[s['first_frame'] for s in segmented['segments']]
    for ordinal,item in enumerate(index['frames']):
        if ordinal%10:continue
        record=json.loads((root/item['file']).read_text())
        segment=int(np.searchsorted(cutoffs,record['index'],side='right')-1)
        if segment not in transforms or record.get('tracking')!='normal' or not record.get('depth') or not record.get('confidence'):continue
        dw,dh=record['depth_resolution'];depth=np.frombuffer(asset(root,record['depth']),dtype='<f4').reshape(dh,dw);confidence=np.frombuffer(asset(root,record['confidence']),dtype='u1').reshape(dh,dw)
        points,_=unproject(depth,confidence,np.asarray(record['intrinsics']).reshape(3,3),np.asarray(record['camera_to_world']).reshape(4,4),record['image_resolution'],stride=8)
        r,t=transforms[segment];cloud.append(points@r.T+t)
    report={'capture_id':segmented['capture_id'],'segments':len(cutoffs),'registered_segments':sorted(transforms),
        'unregistered_segments':sorted(set(range(len(cutoffs)))-transforms.keys()),'edges':edges,
        'transforms':{str(s):{'rotation':r.tolist(),'translation_m':t.tolist()} for s,(r,t) in transforms.items()},
        'calibration_validated':False,'room_model_applied':False,
        'limitation':'Draft common-corner registration; held-out residuals do not certify optical alignment or absolute room accuracy.'}
    return report,np.concatenate(cloud) if cloud else np.empty((0,3))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--output',type=Path,required=True);args=p.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    report,cloud=align(args.directory);(args.output/'segment-alignment.json').write_text(json.dumps(report,indent=2)+'\n')
    np.save(args.output/'aligned-depth-points.npy',cloud)
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(9,7));ax.scatter(cloud[:,0],cloud[:,2],s=.15,alpha=.15)
    ax.set(xlabel='Draft aligned world X (m)',ylabel='Draft aligned world Z (m)',title='QR-anchor alignment of pose segments — diagnostic reconstruction, not applied calibration')
    ax.set_aspect('equal');fig.tight_layout();fig.savefig(args.output/'aligned-depth-top-view.png',dpi=160);plt.close(fig)
    print(json.dumps({'registered':report['registered_segments'],'missing':report['unregistered_segments'],'edges':report['edges']}))
