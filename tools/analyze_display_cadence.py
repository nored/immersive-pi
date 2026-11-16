#!/usr/bin/env python3
"""Group HDMI presentation receipts by session and count refresh intervals."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def analyze(path, refresh_hz):
    sessions = defaultdict(lambda: defaultdict(dict))
    incomplete = 0
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            incomplete += 1
            continue
        packet = record.get('packet', {})
        if record.get('event') == 'receipt' and packet.get('type') == 'frame_presented':
            sessions[packet['session']][record['node']][packet['sequence']] = packet['presented_at_ns']
    period = 1e9 / refresh_hz
    result = {'refresh_hz': refresh_hz, 'incomplete_lines': incomplete, 'sessions': {}}
    for session, nodes in sessions.items():
        report = {'nodes': {}}
        for node, frames in sorted(nodes.items()):
            samples = sorted(frames.items())
            intervals = [timestamp - previous_time
                         for (previous, previous_time), (sequence, timestamp)
                         in zip(samples, samples[1:]) if sequence == previous + 1]
            counts = defaultdict(int)
            for interval in intervals:
                refreshes = round(interval / period)
                key = str(refreshes) if abs(interval - refreshes * period) <= 1e6 else 'unclassified'
                counts[key] += 1
            report['nodes'][node] = {
                'receipts': len(samples), 'consecutive_intervals': len(intervals),
                'missing_sequences': samples[-1][0] - samples[0][0] + 1 - len(samples),
                'refresh_intervals': dict(sorted(counts.items())),
                'median_interval_ms': statistics.median(intervals) / 1e6 if intervals else None,
            }
        sequences = set.intersection(*(set(frames) for frames in nodes.values()))
        skews = [max(nodes[node][sequence] for node in nodes) -
                 min(nodes[node][sequence] for node in nodes) for sequence in sequences]
        report['complete_frames'] = len(sequences)
        report['max_skew_ms'] = max(skews) / 1e6 if skews else None
        result['sessions'][session] = report
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--refresh-hz', type=float, required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    payload = json.dumps(analyze(args.trace, args.refresh_hz), indent=2) + '\n'
    if args.out:
        args.out.write_text(payload)
    print(payload)
