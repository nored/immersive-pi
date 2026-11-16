"""Online projector measurement quality. No room writes or legacy scan code.

Models are fitted to spatially separated training views and checked against
held-out views. A successful gate establishes acquisition self-consistency,
not optical seam quality or absolute ARKit accuracy.
"""
import math
import numpy as np


def homography(source, target):
    def normalize(points):
        mean=points.mean(0);scale=np.sqrt(2)/max(np.mean(np.linalg.norm(points-mean,axis=1)),1e-9)
        transform=np.array([[scale,0,-mean[0]*scale],[0,scale,-mean[1]*scale],[0,0,1.]])
        return (np.c_[points,np.ones(len(points))]@transform.T)[:,:2],transform
    a,ta=normalize(source);b,tb=normalize(target)
    design=[]
    for (x,y),(u,v) in zip(a,b):
        design.extend([[-x,-y,-1,0,0,0,u*x,u*y,u],[0,0,0,-x,-y,-1,v*x,v*y,v]])
    _,singular,vt=np.linalg.svd(design,full_matrices=len(design)<9)
    if len(singular)<8 or singular[7]<singular[0]*1e-7:raise ValueError('degenerate projector observations')
    return np.linalg.inv(tb)@vt[-1].reshape(3,3)@ta


def project(h,points):
    p=np.c_[points,np.ones(len(points))]@h.T
    if np.any(abs(p[:,2])<1e-8):raise ValueError('invalid projective model')
    return p[:,:2]/p[:,2:]


def corner_world(record, depth, confidence, corners):
    """Robust high-confidence axial depth near native-image QR corners."""
    k=np.asarray(record['intrinsics'],float).reshape(3,3)
    pose=np.asarray(record['camera_to_world'],float).reshape(4,4)
    w,h=record['image_resolution'];dh,dw=depth.shape
    result=[]
    for u,v in corners:
        x,y=int(u*dw/w),int(v*dh/h)
        if not (0<=x<dw and 0<=y<dh):raise ValueError('marker outside depth image')
        patch=depth[max(0,y-2):y+3,max(0,x-2):x+3]
        levels=confidence[max(0,y-2):y+3,max(0,x-2):x+3]
        values=patch[(levels==2)&np.isfinite(patch)&(patch>.2)&(patch<8)]
        if len(values)<6 or np.percentile(values,90)-np.percentile(values,10)>.04:
            raise ValueError('unstable depth at marker corner')
        z=float(np.median(values));ray=np.linalg.solve(k,[u,v,1])
        camera=ray*z*[1,-1,-1]
        result.append(pose[:3,:3]@camera+pose[:3,3])
    return np.asarray(result)


class LiveGeometry:
    def __init__(self,layout):
        self.layout=layout
        self.targets={m['payload']:(n['node'],m) for n in layout['nodes'] for m in n['markers']}
        self.views={n['node']:[] for n in layout['nodes']}
        self.shared_views=set()
        self.last_tracking='not_started'
        self.last_rejections=[]
        self.frames=0
        self.visible=set()

    def add(self,record,depth,confidence):
        self.frames+=1;self.last_tracking=record.get('tracking');self.last_rejections=[]
        if record.get('tracking')!='normal' or not record.get('backend_pattern_settled'):
            return self.report()
        context=record['backend_scan_context']
        if context['kind'] not in ('overview','fiducials'):return self.report()
        pose=np.asarray(record['camera_to_world'],float).reshape(4,4)
        grouped={};visible=set()
        for detected in record.get('recognized_projector_markers',[]):
            target=self.targets.get(detected.get('payload'))
            if not target:continue
            node,marker=target
            if context['kind']=='fiducials' and context.get('node')!=node:continue
            try:world=corner_world(record,depth,confidence,detected['corners_px'])
            except (ValueError,IndexError,TypeError) as exc:
                self.last_rejections.append(str(exc));continue
            grouped.setdefault(node,[]).append((marker['cell'],world,marker['projector_corners_px']))
            visible.add(node)
        self.visible=visible
        for a in visible:
            for b in visible:
                if a<b:self.shared_views.add((a,b))
        for node,markers in grouped.items():
            views=self.views[node]
            position=pose[:3,3]
            # Hundreds of stationary frames are one view, never independent evidence.
            nearby=next((v for v in views if np.linalg.norm(position-v['position'])<.25),None)
            if nearby is not None:
                fresh=[m for m in markers if m[0] not in nearby['cells']]
                if fresh:
                    nearby['cells'].update(m[0] for m in fresh)
                    nearby['world']=np.concatenate([nearby['world']]+[m[1] for m in fresh])
                    nearby['pixels']=np.concatenate([nearby['pixels']]+[m[2] for m in fresh])
                continue
            if len(views)>=60:continue
            views.append({'position':position.copy(),'cells':set(m[0] for m in markers),
                'world':np.concatenate([m[1] for m in markers]),
                'pixels':np.concatenate([m[2] for m in markers])})
        return self.report()

    def quality(self,node):
        views=self.views[node];cells=set().union(*(v['cells'] for v in views)) if views else set()
        row={'node':node,'distinct_views':len(views),'cells_seen':len(cells),'total_cells':12,
             'geometry_sufficient':False,'reason':'Walk slowly to see this projector from several positions.'}
        if len(cells)<12:
            row['reason']='Step back slightly so the projection edges and targets fit in the camera.'
            return row
        if len(views)<6:
            row['reason']='Take a small sideways step, keeping this projection in view.'
            return row
        training=views[::2];held=views[1::2]
        # Hold-out must also cover all marker cells, not just a well-seen centre.
        train_cells=set().union(*(v['cells'] for v in training))
        test_cells=set().union(*(v['cells'] for v in held))
        if len(train_cells)<12 or len(test_cells)<12:
            row['reason']='Show the full projection from another position, including its edges.';return row
        xyz=np.concatenate([v['world'] for v in training]);uv=np.concatenate([v['pixels'] for v in training])
        origin=xyz.mean(0);_,_,basis=np.linalg.svd(xyz-origin,full_matrices=False)
        plane=(xyz-origin)@basis[:2].T
        try:h=homography(plane,uv)
        except ValueError as exc:row['reason']=str(exc);return row
        check=np.concatenate([v['world'] for v in held]);pixels=np.concatenate([v['pixels'] for v in held])
        residual=np.abs((check-origin)@basis[2])
        try:error=np.linalg.norm(project(h,(check-origin)@basis[:2].T)-pixels,axis=1)
        except ValueError as exc:row['reason']=str(exc);return row
        p95=float(np.percentile(error,95));plane95=float(np.percentile(residual,95))
        row.update(held_out_p95_px=p95,held_out_plane_p95_m=plane95,held_out_points=len(check))
        # No extrapolation: require enough spread and a nontrivial baseline.
        baseline=max(np.linalg.norm(a['position']-b['position']) for a in views for b in views)
        row['baseline_m']=float(baseline)
        row['draft_model']={'type':'planar_projector_homography',
            'plane_origin_world_m':origin.tolist(),'plane_axes_world':basis[:2].tolist(),
            'plane_normal_world':basis[2].tolist(),'plane_to_projector':h.tolist(),
            'training_views':len(training),'held_out_views':len(held),
            'absolute_geometry_validated':False,'room_model_applied':False}

        row['geometry_sufficient']=bool(p95<=2 and plane95<=.02 and baseline>=.5)
        row['reason']=('Geometry checks passed; overlap and light blending still need validation.' if row['geometry_sufficient'] else
                       'Measurements disagree. Rescan the edges slowly from a different position; avoid tracking loss.')
        return row

    def report(self):
        rows=[self.quality(n['node']) for n in self.layout['nodes']]
        missing=next((r for r in rows if r['node'] in self.visible and not r['geometry_sufficient']),None) or next((r for r in rows if not r['geometry_sufficient']),None)
        if self.last_tracking not in ('normal','not_started'):
            instruction='Hold still while camera tracking recovers.'
        elif self.last_rejections:
            instruction='Move slightly back: depth at the target is unstable.'
        elif missing:
            instruction=missing['reason'] if missing['node'] in self.visible else 'Walk slowly along the room with projected targets in view.'
        else:
            instruction='Geometry is consistent. Overlap and brightness checks are still required.'
        return {'type':'analysis_quality','frames_received':self.frames,'nodes':rows,
                'instruction':instruction,'geometry_sufficient':bool(rows) and missing is None,
                'simultaneously_seen_projector_pairs':[list(p) for p in sorted(self.shared_views)],
                # Completion requires more than this planar self-consistency model.
                'acquisition_complete':False,'calibration_validated':False,
                'remaining':['independent overlap alignment','radiometric blend validation'],
                'thresholds':{'held_out_p95_px':2,'plane_p95_m':.02,'minimum_distinct_views':6,'view_spacing_m':.25},
                'limitation':'Planar measurement self-consistency only; nonplanar surfaces, absolute drift and optical seam quality are not certified.'}
