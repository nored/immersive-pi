import copy
import importlib.util
from pathlib import Path

import pytest

from roommodel import default_node_entry

spec = importlib.util.spec_from_file_location('seam_quality', Path(__file__).resolve().parents[1] / 'tools/seamcal/quality.py')
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


@pytest.fixture
def candidate():
    nodes = ['node-1', 'node-2']
    edits = [{'cmd': 'set_' + field, 'node': node, field: default_node_entry(node)[field]}
             for node in nodes for field in ('source_region', 'mesh', 'blend')]
    report = {'ring': False, 'closed': True, 'nodes': dict.fromkeys(nodes, {}),
              'seams': {'node-1|node-2': {'pairs': 30, 'p95_px': 2}},
              'missing_seams': [], 'left_out': [], 'blend_gamma_convention': 'projector-response'}
    return edits, report, nodes


def test_complete_accurate_candidate_is_accepted_without_mutating_it(candidate):
    before = copy.deepcopy(candidate)
    quality.validate_candidate(*candidate)
    assert candidate == before


@pytest.mark.parametrize('fault', ['large-residual', 'nonfinite-residual', 'missing-seam',
                                    'missing-node', 'old-gamma', 'missing-edit', 'folded-mesh', 'duplicate-edit'])
def test_unusable_candidate_is_rejected_before_live_edits(candidate, fault):
    edits, report, nodes = candidate
    if fault == 'large-residual': report['seams']['node-1|node-2']['p95_px'] = 288.61
    if fault == 'nonfinite-residual': report['seams']['node-1|node-2']['p95_px'] = float('nan')
    if fault == 'missing-seam': report['seams'].clear()
    if fault == 'missing-node': del report['nodes']['node-2']
    if fault == 'old-gamma': del report['blend_gamma_convention']
    if fault == 'missing-edit': edits.pop()
    if fault == 'folded-mesh':
        edits[1]['mesh']['points'][0]['x'] = 1.5
    if fault == 'duplicate-edit': edits.append(copy.deepcopy(edits[0]))
    with pytest.raises(ValueError):
        quality.validate_candidate(edits, report, nodes)
