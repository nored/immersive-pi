#!/usr/bin/env python3
"""Compare a renderer's chosen refresh with its DRM presentation receipt."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def analyze(path):
    submissions = {}
    clock_samples = {}
    sessions = defaultdict(list)
    incomplete = 0
    for line in path.open():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            incomplete += 1
            continue
        if row['event'] == 'flip_submitted':
            submissions[row['sequence']] = row
        elif row['event'] == 'flip_clock':
            clock_samples[(row['session'], row['sequence'])] = row
        elif row['event'] == 'presented':
            submitted = submissions.pop(row['sequence'], None)
            if submitted is None or submitted['last_flip_ns'] is None:
                continue
            period = submitted['period_ns']
            if not period:
                continue
            target = submitted['queue_at_ns'] + period - 2_000_000
            sample = clock_samples.pop((submitted['session'], row['sequence']), None)
            sessions[submitted['session']].append({
                'sequence': row['sequence'], 'target_ns': target,
                'flip_ns': row['clock_ns'], 'period_ns': period,
                'target_error_ms': (row['clock_ns'] - target) / 1e6,
                'submission_delay_ms': (submitted['submitted_master_ns'] - submitted['queue_at_ns']) / 1e6,
                'return_margin_ms': (target - submitted['returned_master_ns']) / 1e6,
                'deadline_error_ms': row['error_ns'] / 1e6,
                'clock_sample': sample,
            })
    report = {'incomplete_lines': incomplete, 'sessions': {}}
    for session, rows in sessions.items():
        transitions = []
        for previous, row in zip(rows, rows[1:]):
            if row['sequence'] != previous['sequence'] + 1:
                continue
            actual_steps = round((row['flip_ns'] - previous['flip_ns']) / row['period_ns'])
            target_steps = round((row['target_ns'] - previous['target_ns']) / row['period_ns'])
            if actual_steps != 2:
                transitions.append({**row, 'actual_refreshes': actual_steps,
                                    'chosen_refreshes': target_steps})
        report['sessions'][session] = {
            'matched_receipts': len(rows),
            'median_submission_delay_ms': statistics.median(r['submission_delay_ms'] for r in rows),
            'max_submission_delay_ms': max(r['submission_delay_ms'] for r in rows),
            'min_return_margin_ms': min(r['return_margin_ms'] for r in rows),
            'max_abs_target_error_ms': max(abs(r['target_error_ms']) for r in rows),
            'transitions': transitions,
        }
        samples = [row['clock_sample'] for row in rows if row['clock_sample']]
        if samples:
            raw_intervals = defaultdict(int)
            for previous, row in zip(rows, rows[1:]):
                if row['sequence'] != previous['sequence'] + 1 or not previous['clock_sample'] or not row['clock_sample']:
                    continue
                interval = row['clock_sample']['flip_local_ns'] - previous['clock_sample']['flip_local_ns']
                steps = round(interval / row['period_ns'])
                key = str(steps) if abs(interval - steps * row['period_ns']) <= 1_000_000 else 'unclassified'
                raw_intervals[key] += 1
            spans = sorted(sample['sample_span_ns'] for sample in samples)
            report['sessions'][session]['clock_mapping'] = {
                'samples': len(samples),
                'max_sample_span_ns': max(spans),
                'p99_sample_span_ns': spans[min(len(spans) - 1, int(len(spans) * .99))],
                'max_sampling_uncertainty_ns': max(sample['uncertainty_ns'] for sample in samples),
                'cached_samples': sum(sample['reused'] for sample in samples),
                'raw_drm_refresh_intervals': dict(raw_intervals),
            }
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('trace', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    payload = json.dumps(analyze(args.trace), indent=2) + '\n'
    if args.out:
        args.out.write_text(payload)
    print(payload)
