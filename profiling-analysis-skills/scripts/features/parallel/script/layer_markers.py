"""Layer scopes and backward device attribution from recorded CPU and flow events."""
from bisect import bisect_right
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import re

from timeline_streams import _iter_trace


# Insight DbSqlDefs.h distinguishes fwdbwd and async_npu by connection identity.
# The TEXT adapter uses those categories and wraps the sample's explicit layer scopes.
LAYER = re.compile(r'^hy106\.model\.(student|teacher)\.layer\.(\d+)$')
PHASES = {'hy106.phase.fwd': 'f', 'hy106.phase.bwd': 'b'}


def tick(value):
    return round(Decimal(str(value)) * 1000)


def us(value):
    return format(Decimal(value) / 1000, 'f')


class Intervals:
    def __init__(self, rows):
        self.rows = sorted(rows)
        self.starts = [row[0] for row in self.rows]
        self.max_ends = []
        for _, end, *_ in self.rows:
            self.max_ends.append(max(end, self.max_ends[-1]) if self.max_ends else end)

    def at(self, time):
        right = bisect_right(self.starts, time)
        left = bisect_right(self.max_ends, time, 0, right)
        return [row for row in self.rows[left:right] if row[1] > time]

    def innermost(self, time):
        hits = self.at(time)
        if not hits:
            return None
        width = min(end - start for start, end, *_ in hits)
        matches = [row for row in hits if row[1] - row[0] == width]
        return matches[0] if len(matches) == 1 else None


EMPTY = Intervals([])


def paired(points):
    if len(points) == 2 and {point[0] for point in points} == {'s', 'f'}:
        return sorted(points, reverse=True)
    return None


def scan_trace(trace):
    scopes = defaultdict(set)
    autograd = defaultdict(list)
    sequences = defaultdict(list)
    flows = defaultdict(lambda: defaultdict(list))
    for ordinal, event in enumerate(_iter_trace(trace)):
        phase = event.get('ph')
        category = event.get('cat')
        if phase in ('s', 'f') and category in ('fwdbwd', 'async_npu') and 'id' in event:
            flows[category][str(event['id'])].append(
                (phase, (str(event['pid']), str(event['tid'])), tick(event['ts']))
            )
        if phase != 'X' or category != 'cpu_op':
            continue
        name = event.get('name', '')
        layer = LAYER.fullmatch(name)
        start = tick(event['ts'])
        end = start + tick(event['dur'])
        if end <= start:
            continue
        track = str(event['pid']), str(event['tid'])
        if layer:
            scopes[track].add((start, end, layer[1], int(layer[2])))
        if name.startswith('autograd::engine::evaluate_function:'):
            autograd[track].append((start, end, ordinal))
        sequence = (event.get('args') or {}).get('Sequence number')
        if isinstance(sequence, int) and sequence >= 0:
            sequences[track].append((start, end, sequence))
    return scopes, {k: Intervals(v) for k, v in autograd.items()}, {
        k: Intervals(v) for k, v in sequences.items()
    }, flows


def rank_layers(trace, origin, parents, events):
    scopes, autograd, sequences, flows = scan_trace(trace)
    parent_index = {
        phase: Intervals(
            (origin + tick(region['framework_start_us']), origin + tick(region['framework_end_us']), region['region_id'])
            for region in parents.values() if region['phase'] == phase
        )
        for phase in ('f', 'b')
    }
    forward, replay = defaultdict(list), defaultdict(list)
    for track, spans in scopes.items():
        for start, end, role, number in spans:
            fwd = parent_index['f'].innermost(start)
            if fwd and end <= fwd[1]:
                forward[track].append((start, end, role, number, fwd[2]))
            bwd = parent_index['b'].innermost(start)
            if bwd and end <= bwd[1]:
                replay[track].append((start, end))
    forward = {k: Intervals(v) for k, v in forward.items()}
    replay = {k: Intervals(v) for k, v in replay.items()}
    owners = defaultdict(set)
    for points in flows['fwdbwd'].values():
        pair = paired(points)
        if not pair:
            continue
        (_, source, start), (_, target, end) = pair
        layer = forward.get(source, EMPTY).innermost(start)
        parent = parent_index['b'].innermost(end)
        node = autograd.get(target, EMPTY).innermost(end)
        if not layer or not node or not parent or parents[layer[4]]['step'] != parents[parent[2]]['step']:
            continue
        common = {v[2] for v in sequences.get(source, EMPTY).at(start)} & {
            v[2] for v in sequences.get(target, EMPTY).at(end)
        }
        if common:
            owners[node[2]].add((parent[2], layer[2], layer[3]))

    devices = defaultdict(list)
    for event in events:
        start = origin + tick(event['start_us'])
        track = str(event['pid']), str(event['tid'])
        devices[track, start].append(event)
    assignments = defaultdict(set)
    kernels = {}
    for points in flows['async_npu'].values():
        pair = paired(points)
        if not pair:
            continue
        (_, source, start), (_, target, end) = pair
        candidates = devices.get((target, end), [])
        if len(candidates) != 1:
            continue
        event = candidates[0]
        forward_scope = forward.get(source, EMPTY).innermost(start)
        if forward_scope:
            parent_id = forward_scope[4]
            key = (parent_id, forward_scope[2], forward_scope[3], forward_scope[:2])
        else:
            node = autograd.get(source, EMPTY).innermost(start)
            layer_owners = owners.get(node[2], set()) if node else set()
            if len(layer_owners) != 1 or replay.get(source, EMPTY).at(start):
                continue
            parent_id, role, number = next(iter(layer_owners))
            bounds = parent_index['b'].innermost(start)
            if not bounds or bounds[2] != parent_id or not bounds[0] <= end < bounds[1]:
                continue
            key = (parent_id, role, number, ())
        if str(event['step']) == str(parents[parent_id]['step']):
            event_id = str(event['event_id'])
            assignments[event_id].add(key)
            kernels[event_id] = event
    by_layer = defaultdict(list)
    for event_id, matches in assignments.items():
        if len(matches) == 1:
            by_layer[next(iter(matches))].append(kernels[event_id])
    for index in forward.values():
        for start, end, role, number, parent_id in index.rows:
            by_layer.setdefault((parent_id, role, number, (start, end)), [])
    rows = []
    for index, ((parent_id, role, number, scope), kernels) in enumerate(sorted(by_layer.items())):
        parent = parents[parent_id]
        phase = parent['phase']
        if scope:
            start, end = (value - origin for value in scope)
            basis = '显式框架区间；设备算子按 torch_to_npu 关联' if kernels else '仅显式框架区间'
        else:
            start = min(tick(event['start_us']) for event in kernels)
            end = max(tick(event['end_us']) for event in kernels)
            basis = '已归层设备算子覆盖范围 · fwdbwd / Sequence number / torch_to_npu'
        rows.append({
            'region_id': f'{parent_id}:layer:{index}', 'parent_region_id': parent_id,
            'start_us': us(start), 'end_us': us(end), 'duration_us': us(end - start),
            'rank': parent['rank'], 'stage': parent.get('stage'), 'step': parent['step'],
            'phase': phase, 'model_role': role, 'layer_number': number, 'label': f'L{number}{phase}',
            'basis': basis, 'kernel_count': len(kernels),
            'event_ids': [str(event['event_id']) for event in kernels],
        })
    return rows


def collect_layer_regions(report):
    by_rank = defaultdict(dict)
    for region in report.get('compute_regions', []):
        if PHASES.get(region.get('source_name')) == region['phase'] and all(
            region.get(key) is not None for key in ('framework_start_us', 'framework_end_us')
        ):
            by_rank[str(region['rank'])][region['region_id']] = region
    if not by_rank:
        return []
    traces = {str(item['rank']): Path(item['trace']) for item in report.get('sources', []) if item.get('trace')}
    events = defaultdict(list)
    for event in report.get('events', []):
        rank = str(event['rank'])
        if event['kind'] == 'compute' and rank in by_rank:
            events[rank].append(event)
    origin = tick(report['time_origin_us'])
    return [
        region for rank, parents in by_rank.items() if rank in traces and traces[rank].is_file()
        for region in rank_layers(traces[rank], origin, parents, events[rank])
    ]
