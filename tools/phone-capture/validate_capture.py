#!/usr/bin/env python3
"""Inspect an extracted native phone capture; never apply room calibration.

Requires NumPy and Pillow. Integrity and acquisition checks do not establish
tracking accuracy, room coverage, projector correspondences or seam quality.
"""
import argparse
import hashlib
import json
import math
import re
import uuid
from pathlib import Path

import numpy as np
from PIL import Image


def local_file(root, name):
    if not isinstance(name, str) or not name or Path(name).name != name:
        raise ValueError('capture filenames must be plain local names')
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise ValueError('missing or linked capture file: ' + name)
    return path


def asset(root, info):
    if not isinstance(info, dict):
        raise ValueError('missing asset metadata')
    data = local_file(root, info.get('file')).read_bytes()
    if len(data) != info.get('bytes') or hashlib.sha256(data).hexdigest() != info.get('sha256'):
        raise ValueError('asset integrity mismatch: ' + str(info.get('file')))
    return data


def dimensions(value):
    if not isinstance(value, list) or len(value) != 2 or not all(type(v) is int and v > 0 for v in value):
        raise ValueError('invalid image dimensions')
    return tuple(value)


def matrix(value, size):
    if not isinstance(value, list) or len(value) != size*size:
        raise ValueError('invalid calibration matrix dimensions')
    if not all(type(v) in (int, float) and math.isfinite(v) for v in value):
        raise ValueError('calibration matrix contains invalid numbers')
    return np.array(value, dtype=float).reshape(size, size)


def backend_reasons(record, setup):
    context=record.get('backend_scan_context')
    if setup is None:
        if context is not None:raise ValueError('backend context has no recorded session layout')
        return []
    if not isinstance(setup,dict) or setup.get('protocol')!=1 or not isinstance(context,dict):
        raise ValueError('guided frame has no supported backend context')
    layout=setup.get('layout',{})
    session=setup.get('session')
    if not isinstance(layout,dict) or layout.get('protocol')!=1 or not isinstance(session,str) or not re.fullmatch('[0-9a-f]{32}',session):
        raise ValueError('invalid backend layout/session identity')
    if layout.get('layout_id')!=session[:16] or context.get('session')!=session or context.get('layout_id')!=layout.get('layout_id'):
        raise ValueError('guided frame belongs to another backend session/layout')
    kind=context.get('kind');phase=context.get('phase');target=context.get('node')
    if kind not in ('overview','fiducials','black','white') or type(phase) is not int or phase<1:
        raise ValueError('invalid backend pattern phase')
    nodes=layout.get('nodes')
    if not isinstance(nodes,list) or not nodes or not all(isinstance(n,dict) and isinstance(n.get('node'),str) for n in nodes) or len({n.get('node') for n in nodes})!=len(nodes):
        raise ValueError('invalid backend projector layout')
    if kind!='overview' and target not in {n['node'] for n in nodes}:
        raise ValueError('backend pattern target is absent from the layout')
    reasons=[]
    if context.get('ready') is not True:reasons.append('backend_phase_not_ready')
    else:
        acks=context.get('display_acks')
        if not isinstance(acks,dict) or set(acks)!={n['node'] for n in nodes}:
            raise ValueError('backend phase lacks the complete display barrier')
        for node in nodes:
            ack=acks[node['node']]
            pattern='fiducials' if kind=='overview' else kind if node['node']==target else 'black'
            if not isinstance(ack,dict) or any(ack.get(k)!=value for k,value in {
                    'session':session,'phase':phase,'kind':pattern,'width':node['width'],'height':node['height'],
                    'sha256':node.get('sha256') if pattern=='fiducials' else None}.items()):
                raise ValueError('backend display receipt does not match the native target')
            if type(ack.get('flip_monotonic_ns')) is not int or ack['flip_monotonic_ns']<=0:
                raise ValueError('backend display receipt lacks a DRM timestamp')
    if record.get('backend_pattern_settled') is not True:reasons.append('backend_pattern_unsettled')
    return reasons


def inspect_frame(root, name, backend_setup=None):
    import io
    record = json.loads(local_file(root, name).read_text())
    if not isinstance(record,dict):raise ValueError('frame metadata must be an object')
    width, height = dimensions(record.get('image_resolution'))
    k = matrix(record.get('intrinsics'), 3)
    pose = matrix(record.get('camera_to_world'), 4)
    if (k[0,0] <= 0 or k[1,1] <= 0 or not np.allclose(k[2], [0,0,1]) or
            not 0 <= k[0,2] <= width or not 0 <= k[1,2] <= height):
        raise ValueError('invalid native-image camera intrinsics')
    rotation = pose[:3,:3]
    if (not np.allclose(pose[3], [0,0,0,1]) or
            not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-3) or
            not np.isclose(np.linalg.det(rotation), 1, atol=1e-3)):
        raise ValueError('camera pose is not a rigid camera-to-world transform')
    rgb_data = asset(root, record.get('rgb'))
    with Image.open(io.BytesIO(rgb_data)) as image:
        if image.size != (width, height):
            raise ValueError('RGB dimensions do not match calibration')
        image.thumbnail((256,256))
        gray = np.asarray(image.convert('L'))
        brightness = float(gray.mean())
        bright_fraction = float((gray > 5).mean())
    reasons = backend_reasons(record,backend_setup)
    if record.get('tracking') != 'normal':
        reasons.append('tracking_not_normal')
    if brightness <= 5 or bright_fraction <= .02:
        reasons.append('near_black_image')
    valid_depth_fraction = 0.0
    if record.get('depth') is None or record.get('confidence') is None:
        reasons.append('depth_or_confidence_missing')
    else:
        if record.get('depth_encoding') != 'float32_little_endian_packed_rows':
            raise ValueError('unsupported depth encoding')
        if record.get('confidence_encoding') != 'ARConfidenceLevel_uint8_packed_rows':
            raise ValueError('unsupported confidence encoding')
        dw, dh = dimensions(record.get('depth_resolution'))
        if dimensions(record.get('confidence_resolution')) != (dw, dh):
            raise ValueError('confidence and depth are not aligned')
        depth = np.frombuffer(asset(root, record['depth']), dtype='<f4')
        confidence = np.frombuffer(asset(root, record['confidence']), dtype=np.uint8)
        if depth.size != dw*dh or confidence.size != dw*dh:
            raise ValueError('depth/confidence byte count disagrees with dimensions')
        if np.any(confidence > 2):
            raise ValueError('invalid ARKit confidence values')
        valid_depth_fraction = float((np.isfinite(depth) & (depth > 0) & (confidence >= 1)).mean())
        if valid_depth_fraction < .1:
            reasons.append('insufficient_confident_depth')
    timestamp = record.get('timestamp_s')
    if type(timestamp) not in (int,float) or not math.isfinite(timestamp) or record.get('timestamp_clock') != 'ARFrame':
        raise ValueError('invalid ARFrame timestamp')
    return {'file': name, 'index': record.get('index'), 'timestamp_s': timestamp,
            'capture_eligible': not reasons, 'review_reasons': reasons,
            'mean_brightness':brightness, 'valid_confident_depth_fraction':valid_depth_fraction}


def inspect_bundle(root):
    root = Path(root)
    session = json.loads(local_file(root, 'session.json').read_text())
    if not isinstance(session,dict):raise ValueError('session metadata must be an object')
    if session.get('format') != 'immersive-arkit-capture-v1':
        raise ValueError('not a native ARKit capture bundle')
    expected = {'matrix_storage':'row_major','pose_convention':'ARKit_camera_to_world_meters',
                'intrinsics_coordinates':'native_RGB_pixels','rgb_orientation':'native_sensor_unrotated',
                'camera_axes':'x_right_y_up_z_back; forward_is_negative_z',
                'depth_convention':'ARKit_sceneDepth_axial_meters','world_alignment':'gravity'}
    for key, value in expected.items():
        if session.get(key) != value:
            raise ValueError('unsupported or missing capture convention: ' + key)
    names = session.get('frames')
    backend_setup=session.get('backend_scan')
    if backend_setup is not None:
        if not isinstance(backend_setup,dict) or not isinstance(backend_setup.get('capture_id'),str) or not isinstance(session.get('session_id'),str) or uuid.UUID(backend_setup['capture_id'])!=uuid.UUID(session['session_id']):
            raise ValueError('backend capture ID does not match the phone recording')
    if not isinstance(names,list) or not names or not all(isinstance(n,str) for n in names) or len(set(names)) != len(names):
        raise ValueError('capture must list unique frame metadata files')
    rows, errors = [], []
    previous = -math.inf
    for index, name in enumerate(names):
        try:
            row = inspect_frame(root, name, backend_setup)
            if row['index'] != index or row['timestamp_s'] <= previous:
                raise ValueError('nonsequential frame index or timestamp')
            previous = row['timestamp_s']
            rows.append(row)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            errors.append({'file':name,'error':str(exc)})
    complete = session.get('status') == 'complete' and not session.get('errors')
    eligible = sum(row['capture_eligible'] for row in rows)
    return {'format':session['format'],'session_id':session.get('session_id'),
            'complete':complete,'frames_inspected':len(rows),'eligible_observations':eligible,
            'errors':errors,'frames':rows,'capture_checks_passed':complete and not errors and eligible > 0,
            'calibration_validated':False,
            'limitation':'Acquisition/integrity checks only. No geometric accuracy, full-room coverage, projector alignment or blending claim.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    try:
        report = inspect_bundle(args.directory)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        report = {'capture_checks_passed':False,'calibration_validated':False,'error':str(exc)}
    encoded = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded)
    else:
        print(encoded,end='')
    return 0 if report['capture_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
