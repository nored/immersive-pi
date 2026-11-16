"""Native QR scan targets and conservative capture-coverage accounting.

Coverage is acquisition evidence, never permission to apply a calibration.
Target generation imports OpenCV only on demand on the control node.
"""
import hashlib
import math
from pathlib import Path
import re


PROTOCOL = 1
COLS, ROWS = 4, 3


def make_targets(directory, session, nodes):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw
    if not re.fullmatch(r'[0-9a-f]{32}',session):
        raise ValueError('invalid scan session')
    if not nodes or len({n['node'] for n in nodes})!=len(nodes):
        raise ValueError('unique projector nodes required')
    params=cv2.QRCodeEncoder_Params()
    params.version=3
    params.correction_level=cv2.QRCodeEncoder_CORRECT_LEVEL_M
    encoder=cv2.QRCodeEncoder_create(params)
    result=[]
    for index,node in enumerate(nodes):
        width,height=node['width'],node['height']
        if type(width) is not int or type(height) is not int or not 640<=width<=4096 or not 480<=height<=4096:
            raise ValueError('unsupported projector target dimensions')
        image=Image.new('RGB',(width,height),(255,255,255))
        draw=ImageDraw.Draw(image)
        markers=[]
        for cell in range(COLS*ROWS):
            row,col=divmod(cell,COLS)
            payload=f'IC1|{session[:16]}|{index}|{cell}'
            encoded=encoder.encode(payload)
            ys,xs=np.nonzero(encoded<128)
            # Remove encoder padding, then provide an explicit four-module
            # quiet zone. Coordinates below describe the symbol, not padding.
            body=encoded[ys.min():ys.max()+1,xs.min():xs.max()+1]
            modules=body.shape[0]
            if body.shape!=(modules,modules):raise ValueError('nonsquare QR target')
            scale=int(min(width/COLS,height/ROWS)*.86/(modules+8))
            if scale<3:raise ValueError('target modules too small')
            tile=np.pad(body,4,constant_values=255)
            tile=np.repeat(np.repeat(tile,scale,axis=0),scale,axis=1)
            x=int(round((col+.5)*width/COLS-tile.shape[1]/2))
            y=int(round((row+.5)*height/ROWS-tile.shape[0]/2))
            image.paste(Image.fromarray(tile).convert('RGB'),(x,y))
            a,b=x+4*scale,y+4*scale
            side=modules*scale
            markers.append({'cell':cell,'row':row,'col':col,'payload':payload,
                            'projector_corners_px':[[a,b],[a+side-1,b],[a+side-1,b+side-1],[a,b+side-1]]})
            draw.text((int(col*width/COLS)+5,int(row*height/ROWS)+4),f'{node["node"]}  {row+1}/{col+1}',fill=(32,32,32))
        name=f'scan-target-{session}-{index}.png'
        path=Path(directory)/name
        image.save(path)
        result.append({'node':node['node'],'width':width,'height':height,'file':name,
                       'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'markers':markers})
    return {'protocol':PROTOCOL,'layout_id':session[:16],'cols':COLS,'rows':ROWS,
            'coordinate_convention':'native_projector_pixels_top_left_origin',
            'projection_path':'raw_unwarped_output','nodes':result}


def camera_position(value):
    if not isinstance(value,list) or len(value)!=16 or any(type(x) not in (int,float) or not math.isfinite(x) for x in value):
        raise ValueError('finite row-major camera pose required')
    if any(abs(value[12+i]-target)>1e-4 for i,target in enumerate([0,0,0,1])):
        raise ValueError('invalid camera pose bottom row')
    rows=[value[0:3],value[4:7],value[8:11]]
    for i in range(3):
        for j in range(3):
            if abs(sum(a*b for a,b in zip(rows[i],rows[j]))-(1 if i==j else 0))>1e-3:
                raise ValueError('camera pose rotation must be rigid')
    a,b,c=rows
    determinant=a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(determinant-1)>1e-3:raise ValueError('camera pose rotation must preserve handedness')
    return (value[3],value[7],value[11])


class Coverage:
    def __init__(self,layout):
        self.layout=layout
        self.lookup={m['payload']:(node['node'],m['cell']) for node in layout['nodes'] for m in node['markers']}
        self.cells={n['node']:{m['cell']:[] for m in n['markers']} for n in layout['nodes']}
        self.detections={n['node']:0 for n in layout['nodes']}
        self.last_frame=-1
        self.last_timestamp=-math.inf

    def observe(self,message,*,phase_kind,target=None):
        if phase_kind not in ('overview','fiducials'):
            raise ValueError('this phase contains no identifiable targets')
        if message.get('tracking')!='normal' or message.get('eligible') is not True:
            raise ValueError('normal tracking and eligible capture required')
        index=message.get('frame_index');timestamp=message.get('timestamp_s')
        if type(index) is not int or index<=self.last_frame or type(timestamp) not in (int,float) or not math.isfinite(timestamp) or timestamp<=self.last_timestamp:
            raise ValueError('stale or unordered capture observation')
        position=camera_position(message.get('camera_to_world'))
        size=message.get('image_resolution')
        if not isinstance(size,list) or len(size)!=2 or any(type(v) is not int or not 1<=v<=8192 for v in size):
            raise ValueError('valid camera image dimensions required')
        observations=message.get('markers')
        if not isinstance(observations,list) or len(observations)>256:
            raise ValueError('bounded marker observation list required')
        accepted=[];seen=set()
        # Validate the entire packet before changing any accumulated evidence.
        for observation in observations:
            if not isinstance(observation,dict):raise ValueError('marker observation must be an object')
            payload=observation.get('payload')
            if payload not in self.lookup:raise ValueError('unknown or stale target payload')
            if payload in seen:raise ValueError('duplicate marker in one frame')
            seen.add(payload)
            node,cell=self.lookup[payload]
            if phase_kind=='fiducials' and node!=target:
                raise ValueError('marker belongs to a projector outside the active phase')
            corners=observation.get('corners_px')
            if not isinstance(corners,list) or len(corners)!=4 or any(not isinstance(p,list) or len(p)!=2 for p in corners):
                raise ValueError('four native camera-pixel corners required')
            for p in corners:
                if any(type(v) not in (int,float) or not math.isfinite(v) for v in p) or not 0<=p[0]<size[0] or not 0<=p[1]<size[1]:
                    raise ValueError('marker corner outside the native camera image')
            turns=[]
            for i in range(4):
                a,b,c=corners[i],corners[(i+1)%4],corners[(i+2)%4]
                turns.append((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
            area=abs(sum(corners[i][0]*corners[(i+1)%4][1]-corners[(i+1)%4][0]*corners[i][1] for i in range(4)))/2
            if not (all(v>0 for v in turns) or all(v<0 for v in turns)) or area<100:
                raise ValueError('degenerate or undersized marker')
            accepted.append((node,cell,area))
        self.last_frame=index;self.last_timestamp=timestamp
        for node,cell,_ in accepted:
            views=self.cells[node][cell]
            if len(views)<8 and all(math.dist(position,prior)>=.2 for prior in views):views.append(position)
            self.detections[node]+=1
        return max(accepted,key=lambda item:item[2])[0] if accepted else None

    def snapshot(self):
        result=[]
        for node,cells in self.cells.items():
            counts={str(cell):len(views) for cell,views in cells.items()}
            seen=sum(v>0 for v in counts.values());multiple=sum(v>=2 for v in counts.values())
            state='unseen' if seen==0 else 'coverage_recorded' if multiple==len(cells) else 'needs_more_views'
            result.append({'node':node,'state':state,'markers_seen':seen,'markers_with_multiple_views':multiple,
                           'marker_count':len(cells),'cell_views':counts,'detections':self.detections[node]})
        return {'nodes':result,'min_view_baseline_m':.2,'calibration_validated':False,
                'limitation':'QR visibility and recorded-pose diversity only; no geometric fit, depth accuracy, full projection boundary or radiometric blend validated.'}
