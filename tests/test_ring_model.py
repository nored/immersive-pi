import pytest

from make_ring_model import build


@pytest.mark.parametrize('nodes', [1, 2, 5, 12, 17])
@pytest.mark.parametrize('ring', [False, True])
@pytest.mark.parametrize('overlap', [0, .08, .3])
def test_source_intersection_matches_output_blend_width(nodes, ring, overlap):
    entries = build(nodes, overlap, ring, black_lift=0)['nodes']
    for left, right in zip(entries, entries[1:]):
        a, b = left['source_region'], right['source_region']
        intersection = a['x'] + a['w'] - b['x']
        assert intersection / a['w'] == pytest.approx(left['blend']['right']['width'])
        assert intersection / b['w'] == pytest.approx(right['blend']['left']['width'])
        assert intersection / a['w'] == pytest.approx(overlap)
    first, last = entries[0], entries[-1]
    if ring and nodes > 1:
        a, b = last['source_region'], first['source_region']
        intersection = a['x'] + a['w'] - (1 + b['x'])
        assert intersection / a['w'] == pytest.approx(overlap)
        assert intersection / b['w'] == pytest.approx(overlap)
        assert all(e['source_region']['wrap'] for e in entries)
    else:
        assert first['source_region']['x'] == 0
        assert last['source_region']['x'] + last['source_region']['w'] == pytest.approx(1)
        assert first['blend']['left']['width'] == 0
        assert last['blend']['right']['width'] == 0
        assert all(not e['source_region']['wrap'] for e in entries)


@pytest.mark.parametrize('nodes,overlap,lift', [(0, .1, 0), (-1, .1, 0),
    (True, .1, 0), (12, -.1, 0), (12, .5, 0), (12, float('nan'), 0),
    (12, float('inf'), 0), (12, .1, -1), (12, .1, float('nan'))])
def test_invalid_ring_parameters_cannot_generate_a_model(nodes, overlap, lift):
    with pytest.raises(ValueError):
        build(nodes, overlap, True, lift)
