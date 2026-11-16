#!/usr/bin/env python3
"""Diagnose discontinuities without averaging across changed pose coordinates.

Segments are diagnostic partitions, not a claim that ARKit's absolute pose is
correct. This script never merges segments, edits recordings or applies warps.
"""
import argparse,json
from collections import defaultdict
from pathlib import Path
import sys
import numpy as np
from validate_capture import asset
sys.path.insert(0,str(Path(__file__).parent/'live'))
from geometry import corner_world,homography,project


def review(root,*,with_groups=False):
    index=json.loads((root/'live-index.json').read_text());metadata=json.loads((root/'phone-session.json').read_text())
    targets={m['payload']:(n['node'],m) for n in metadata['backend_scan']['layout']['nodes'] for m in n['markers']}
    segment=0;previous=None;jumps=[];groups=defaultdict(list);bounds=defaultdict(list)
    for item in index['frames']:
        r=json.loads((root/item['file']).read_text());pose=np.asarray(r['camera_to_world']).reshape(4,4);position=pose[:3,3]
        if previous is not None:
            last,last_position=previous;dt=r['timestamp_s']-last['timestamp_s'];distance=float(np.linalg.norm(position-last_position))
            if dt>0 and distance>.75 and distance/dt>2:
                segment+=1;jumps.append({'before_frame':last['index'],'after_frame':r['index'],'distance_m':distance,'elapsed_s':dt,'tracking_before':last['tracking'],'tracking_after':r['tracking']})
        previous=(r,position);bounds[segment].append(r['index'])
        if r.get('tracking')!='normal' or not r.get('capture_eligible') or not r.get('backend_pattern_settled'):continue
        if not r.get('depth') or not r.get('confidence'):continue
        dw,dh=r['depth_resolution'];depth=np.frombuffer(asset(root,r['depth']),dtype='<f4').reshape(dh,dw);confidence=np.frombuffer(asset(root,r['confidence']),dtype='u1').reshape(dh,dw)
        detected=defaultdict(list)
        for marker in r.get('recognized_projector_markers',[]):
            target=targets.get(marker.get('payload'))
            if not target:continue
            node,source=target
            try:world=corner_world(r,depth,confidence,marker['corners_px'])
            except ValueError:continue
            detected[node].append((source['cell'],world,np.asarray(source['projector_corners_px'])))
        for node,markers in detected.items():
            views=groups[(segment,node)]
            v=next((v for v in views if np.linalg.norm(v['position']-position)<.25),None)
            if v is None:v={'position':position.copy(),'cells':{}};views.append(v)
            for cell,world,pixels in markers:
                if cell not in v['cells']:v['cells'][cell]=(world,pixels)
    rows=[]
    for (segment,node),views in groups.items():
        cells=set().union(*(v['cells'] for v in views))
        row={'segment':segment,'node':node,'distinct_positions':len(views),'cells':sorted(cells)}
        if len(views)>=4:
            train,test=views[::2],views[1::2]
            xyz=np.concatenate([data[0] for v in train for data in v['cells'].values()]);uv=np.concatenate([data[1] for v in train for data in v['cells'].values()])
            check=np.concatenate([data[0] for v in test for data in v['cells'].values()]);truth=np.concatenate([data[1] for v in test for data in v['cells'].values()])
            origin=xyz.mean(0);_,_,basis=np.linalg.svd(xyz-origin,full_matrices=False)
            try:
                h=homography((xyz-origin)@basis[:2].T,uv);errors=np.linalg.norm(project(h,(check-origin)@basis[:2].T)-truth,axis=1)
                row.update(held_out_p50_px=float(np.median(errors)),held_out_p95_px=float(np.percentile(errors,95)),
                           held_out_plane_p95_m=float(np.percentile(abs((check-origin)@basis[2]),95)),held_out_points=len(check))
            except (ValueError,np.linalg.LinAlgError) as exc:row['fit_error']=str(exc)
        rows.append(row)
    result={'capture_id':metadata['session_id'],'discontinuities':jumps,
            'segments':[{'segment':i,'first_frame':min(frames),'last_frame':max(frames)} for i,frames in bounds.items()],
            'partial_projector_fits':rows,'calibration_validated':False,'room_model_applied':False,
            'limitation':'Diagnostic fits on observed regions. Segment coordinates are not aligned; missing cells and seam quality remain unvalidated.'}
    return (result,groups) if with_groups else result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args();result=review(a.directory);a.out.write_text(json.dumps(result,indent=2)+'\n')
    print('segments',len(result['segments']),'discontinuities',result['discontinuities'])
    for r in result['partial_projector_fits']:
        if 'held_out_p95_px' in r:print(r['segment'],r['node'],len(r['cells']),'cells',round(r['held_out_p95_px'],2),'p95 px',round(r['held_out_plane_p95_m']*100,2),'cm plane p95')
