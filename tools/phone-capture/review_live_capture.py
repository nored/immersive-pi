#!/usr/bin/env python3
"""Review saved live measurements without changing or applying calibration.

Fits on separate camera-position groups and reports errors only within observed
projector regions. Missing cells remain missing; diagnostic fits are not an
acceptance decision. Original files are never modified.
"""
import argparse
from collections import Counter,defaultdict
import json
import re
from pathlib import Path
import sys
import time

import numpy as np

from validate_capture import inspect_frame,asset,local_file
from reconstruct_capture import unproject
sys.path.insert(0,str(Path(__file__).parent/'live'))
from geometry import corner_world,homography,project


def audit(root):
    root=Path(root);index=json.loads(local_file(root,'live-index.json').read_text())
    metadata=json.loads(local_file(root,'phone-session.json').read_text())
    setup=metadata['backend_scan'];layout=setup['layout']
    targets={m['payload']:(n['node'],m) for n in layout['nodes'] for m in n['markers']}
    nodes={n['node']:{'detections':0,'depth_accepted_markers':0,'cells':set(),'views':[],
                      'repeated_corners':defaultdict(list)} for n in layout['nodes']}
    errors=[];kinds=Counter();tracking=Counter();phases={};samples=[];best={}
    previous_index=-1;previous_time=-float('inf');eligible=0
    for ordinal,item in enumerate(index['frames']):
        name=item['file']
        try:
            checked=inspect_frame(root,name,setup)
            r=json.loads(local_file(root,name).read_text())
            if r['index']!=item['index'] or r['index']<=previous_index or r['timestamp_s']<=previous_time:
                raise ValueError('unordered live index')
            previous_index=r['index'];previous_time=r['timestamp_s']
            context=r['backend_scan_context'];kind=context['kind'];kinds[kind]+=1;tracking[r['tracking']]+=1
            eligible+=checked['capture_eligible']
            row=phases.setdefault(context['phase'],{'phase':context['phase'],'kind':kind,'node':context.get('node'),'saved':0,'settled':0})
            row['saved']+=1;row['settled']+=r.get('backend_pattern_settled') is True
            if not r.get('depth') or not r.get('confidence'):continue
            dw,dh=r['depth_resolution']
            depth=np.frombuffer(asset(root,r['depth']),dtype='<f4').reshape(dh,dw)
            confidence=np.frombuffer(asset(root,r['confidence']),dtype='u1').reshape(dh,dw)
            pose=np.asarray(r['camera_to_world']).reshape(4,4)
            if r['tracking']=='normal' and ordinal%10==0:
                cloud,_=unproject(depth,confidence,np.asarray(r['intrinsics']).reshape(3,3),pose,r['image_resolution'],stride=8)
                samples.append(cloud)
            if not checked['capture_eligible']:continue
            grouped=defaultdict(list);visible=Counter()
            for detected in r.get('recognized_projector_markers',[]):
                known=targets.get(detected.get('payload'))
                if not known:continue
                node,marker=known
                if kind=='fiducials' and context.get('node')!=node:continue
                if kind not in ('overview','fiducials'):continue
                nodes[node]['detections']+=1;visible[node]+=1
                try:world=corner_world(r,depth,confidence,detected['corners_px'])
                except ValueError:continue
                state=nodes[node];state['depth_accepted_markers']+=1;state['cells'].add(marker['cell'])
                grouped[node].append((marker['cell'],world,np.asarray(marker['projector_corners_px'])))
            for node,count in visible.items():
                if count>best.get(node,{}).get('markers',0):best[node]={'file':r['rgb']['file'],'markers':count,'frame':r['index']}
            for node,markers in grouped.items():
                state=nodes[node];views=state['views'];position=pose[:3,3]
                view=next((v for v in views if np.linalg.norm(v['position']-position)<.25),None)
                if view is None:
                    view={'position':position.copy(),'cells':set(),'world':[],'pixels':[]};views.append(view)
                for cell,world,pixels in markers:
                    if cell in view['cells']:continue
                    view['cells'].add(cell);view['world'].append(world);view['pixels'].append(pixels)
                    state['repeated_corners'][cell].append(world)
        except (ValueError,TypeError,KeyError,OSError) as exc:errors.append({'file':name,'error':str(exc)})
    report=[]
    for node,state in nodes.items():
        views=state['views'];row={k:state[k] for k in ('detections','depth_accepted_markers')}
        row.update(node=node,cells_seen=sorted(state['cells']),missing_cells=sorted(set(range(12))-state['cells']),
                   distinct_positions=len(views),best_image=best.get(node))
        repeated=[]
        for observations in state['repeated_corners'].values():
            if len(observations)<3:continue
            points=np.asarray(observations);median=np.median(points,axis=0)
            repeated.extend(np.linalg.norm(points-median,axis=2).ravel())
        if repeated:row['same_corner_world_spread_p95_m']=float(np.percentile(repeated,95))
        if len(views)>=6:
            training=views[::2];held=views[1::2]
            xyz=np.concatenate([np.concatenate(v['world']) for v in training]);pixels=np.concatenate([np.concatenate(v['pixels']) for v in training])
            check=np.concatenate([np.concatenate(v['world']) for v in held]);truth=np.concatenate([np.concatenate(v['pixels']) for v in held])
            origin=xyz.mean(0);_,_,basis=np.linalg.svd(xyz-origin,full_matrices=False)
            try:
                h=homography((xyz-origin)@basis[:2].T,pixels)
                error=np.linalg.norm(project(h,(check-origin)@basis[:2].T)-truth,axis=1)
                row['diagnostic_fit']={'held_out_p50_px':float(np.median(error)),
                    'held_out_p95_px':float(np.percentile(error,95)),
                    'held_out_plane_p95_m':float(np.percentile(abs((check-origin)@basis[2]),95)),
                    'training_cells':sorted(set().union(*(v['cells'] for v in training))),
                    'held_out_cells':sorted(set().union(*(v['cells'] for v in held))),
                    'training_points':len(xyz),'held_out_points':len(check),
                    'plane_origin_world_m':origin.tolist(),'plane_axes_world':basis[:2].tolist(),
                    'plane_to_projector':h.tolist(),'scope':'Observed regions only; missing boundaries are not validated'}
            except (ValueError,np.linalg.LinAlgError) as exc:row['fit_error']=str(exc)
        report.append(row)
    references=Counter()
    sequence=sorted(phases.values(),key=lambda r:r['phase'])
    for i in range(len(sequence)-3):
        part=sequence[i:i+4]
        if ([p['kind'] for p in part]==['fiducials','black','white','fiducials'] and
            len({p['node'] for p in part})==1 and all(p['settled']>=2 for p in part)):
            references[part[0]['node']]+=1
    report.sort(key=lambda r:tuple((1,int(t)) if t.isdigit() else (0,t) for t in re.split(r'(\d+)',r['node'])))
    return {'capture_id':metadata['session_id'],'stored_frames':len(index['frames']),'eligible_frames':eligible,
            'integrity_errors':errors,'frame_kinds':dict(kinds),'tracking':dict(tracking),
            'reference_sequences_with_two_settled_frames_per_phase':dict(references),
            'nodes':report,'calibration_validated':False,'room_model_applied':False},np.concatenate(samples) if samples else np.empty((0,3))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('directory',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    started=time.monotonic();report,cloud=audit(args.directory);report['analysis_seconds']=time.monotonic()-started
    (args.output/'review.json').write_text(json.dumps(report,indent=2,allow_nan=False)+'\n')
    np.save(args.output/'sampled-world-points.npy',cloud)
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(9,7));ax.scatter(cloud[:,0],cloud[:,2],s=.15,alpha=.15)
    ax.set(xlabel='ARKit world X (m)',ylabel='ARKit world Z (m)',title='Recorded depth, top view — raw ARKit poses; no alignment or calibration applied')
    ax.set_aspect('equal');fig.tight_layout();fig.savefig(args.output/'depth-top-view.png',dpi=160);plt.close(fig)
    print(json.dumps({'frames':report['stored_frames'],'integrity_errors':len(report['integrity_errors']),'nodes':len(report['nodes']),'seconds':report['analysis_seconds']}))
    for r in report['nodes']:
        print(r['node'],len(r['cells_seen']),'cells',r['distinct_positions'],'positions',r.get('diagnostic_fit',{}).get('held_out_p95_px'),'p95 px',r.get('same_corner_world_spread_p95_m'),'corner spread m')


if __name__=='__main__':main()
