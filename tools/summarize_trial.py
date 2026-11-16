#!/usr/bin/env python3
"""Summarize every presentation receipt in a master's opt-in timing trace."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def summarize(path, expected_nodes):
    frames = defaultdict(dict)
    nodes = defaultdict(list)
    errors = []
    commits = holds = incomplete_lines = 0
    with path.open() as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                incomplete_lines += 1
                continue
            if record['event'] == 'commit':
                commits += 1
                holds += bool(record['held'])
            elif record['event'] == 'receipt' and record['packet']['type'] == 'frame_presented':
                packet, node = record['packet'], record['node']
                frames[packet['sequence']][node] = packet['presented_at_ns']
                nodes[node].append((packet['sequence'], packet['presented_at_ns']))
                errors.append(packet['presentation_error_ns']/1e6)
    if len(nodes) != expected_nodes:
        raise ValueError(f'Expected {expected_nodes} nodes; found {len(nodes)}')
    skews = [(max(f.values())-min(f.values()))/1e6
             for f in frames.values() if len(f) == expected_nodes]
    report = {'committed_frames': commits, 'complete_frames': len(skews),
              'holds': holds, 'max_skew_ms': max(skews, default=None),
              'median_skew_ms': statistics.median(skews) if skews else None,
              'max_deadline_error_ms': max(errors, default=None),
              'incomplete_lines': incomplete_lines, 'nodes': {}}
    for node, receipts in sorted(nodes.items()):
        sequences = [s for s, t in receipts]
        elapsed = receipts[-1][1]-receipts[0][1]
        report['nodes'][node] = {'presented': len(receipts),
            'contiguous': sequences == list(range(len(sequences))),
            'fps': (len(receipts)-1)*1e9/elapsed if elapsed else None}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--nodes', type=int, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    result = summarize(args.trace, args.nodes)
    payload = json.dumps(result, indent=2)
    if args.out:
        args.out.write_text(payload+'\n')
    print(payload)


if __name__ == '__main__':
    main()
