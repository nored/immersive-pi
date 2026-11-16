"""Reject incomplete or inaccurate camera candidates before any live edits."""
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from shared.calibration import validate_solution


def validate_candidate(edits, report, expected_nodes, max_p95_px=4):
    if report.get('blend_gamma_convention') != 'projector-response':
        raise ValueError('Re-solve this capture with the current blend gamma convention')
    if report.get('missing_seams') or report.get('left_out') or (report.get('ring') and not report.get('closed')):
        raise ValueError('Calibration is incomplete; capture all selected nodes and seams')
    if set(report.get('nodes', {})) != set(expected_nodes):
        raise ValueError('Calibration report does not match the selected nodes')
    seams = report.get('seams', {})
    expected_seams = {a + '|' + b for a, b in zip(expected_nodes, expected_nodes[1:])}
    if report.get('ring') and len(expected_nodes) > 2:
        expected_seams.add(expected_nodes[-1] + '|' + expected_nodes[0])
    if set(seams) != expected_seams:
        raise ValueError('Calibration must measure every selected neighbour seam')
    bad = []
    for seam, metrics in seams.items():
        residual = metrics.get('p95_px')
        if (isinstance(residual, bool) or not isinstance(residual, (int, float))
                or not math.isfinite(residual) or not 0 <= residual <= max_p95_px):
            bad.append(seam)
    if bad:
        raise ValueError('Seam error exceeds ' + str(max_p95_px) + ' px: ' + ', '.join(sorted(bad)))
    entries = {node: {'node': node} for node in expected_nodes}
    for edit in edits:
        node, command = edit.get('node'), edit.get('cmd')
        field = {'set_source_region': 'source_region', 'set_mesh': 'mesh', 'set_blend': 'blend'}.get(command)
        if node not in entries or field is None or field in entries[node]:
            raise ValueError('Unexpected or duplicate calibration edit')
        entries[node][field] = edit[field]
    if any(not all(k in entry for k in ('source_region', 'mesh', 'blend')) for entry in entries.values()):
        raise ValueError('Calibration candidate has missing fields')
    validate_solution(list(entries.values()), expected_nodes)
