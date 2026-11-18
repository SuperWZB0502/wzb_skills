#!/usr/bin/env python3
"""Stream device computation/copy/Free and prove Python stack associations.

Only stdlib and ijson are required. CPU events are indexed for one rank at a
time and never embedded in the demonstration. No nearest-time or cross-thread
overlap shortcut is used to manufacture a stack.
"""
from __future__ import annotations
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import csv
import gc
from contextlib import contextmanager
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
import time

VERSION = 4
RAW_INDEX_VERSION = 1
FLOW_CATEGORIES = {'HostToDevice', 'async_npu', 'async_mindspore', 'async_task_queue'}
COPY = re.compile(r'memcpy|memmove|memset|memory[_ ]?copy|(?:^|[_ :])(?:h2d|d2h|d2d)(?:$|[_ :])', re.I)
STACK_KEYS = ('callstack', 'call_stack', 'Call stack', 'Call Stack', 'stack', 'Python stack')
SCALE = Decimal(1000)  # ns, relative to the common PP origin (fits int64).


def _d(value):
    value = Decimal(str(value))
    if not value.is_finite(): raise ValueError('Non-finite trace time')
    return value


def _tick(value, origin):
    scaled = (_d(value) - origin) * SCALE
    return int(scaled) if scaled == scaled.to_integral_value() else scaled


def _floor(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR)) if isinstance(value, Decimal) else value


def _ceil(value):
    return int(value.to_integral_value(rounding=ROUND_CEILING)) if isinstance(value, Decimal) else value


def _us(tick):
    return format(Decimal(tick) / SCALE, 'f')


def _signature(paths):
    return [{'path': str(p.resolve()), 'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in paths]


@contextmanager
def _acyclic_gc_scope():
    """JSON/Node indexes are acyclic; avoid repeated GC scans while building them."""
    enabled=gc.isenabled()
    try:
        if enabled:gc.disable()
        yield
    finally:
        if enabled:gc.enable()


def _read_gzip_json(path):
    with _acyclic_gc_scope():
        with gzip.open(path,'rt',encoding='utf-8') as stream:return json.load(stream)


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str), encoding='utf-8')
    temporary.replace(path)


def _write_gzip(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    from fast_json import iter_json_bytes
    with gzip.open(temporary, 'wb', compresslevel=3) as stream:
        for chunk in iter_json_bytes(data):stream.write(chunk)
    temporary.replace(path)


def _iter_trace(path):
    import ijson
    with path.open('rb') as stream:
        first = b''
        while not first:
            chunk = stream.read(4096)
            if not chunk: raise ValueError(f'Empty trace {path}')
            first = chunk.lstrip()
        stream.seek(0)
        yield from ijson.items(stream, 'traceEvents.item' if first.startswith(b'{') else 'item')


@dataclass(slots=True)
class Node:
    ordinal: int
    pid: str
    tid: str
    begin: int
    end: int
    name: str
    cat: str
    args: dict | None = None


class TrackIndex:
    """Exact integer interval index, returning all fully containing intervals."""
    def __init__(self, nodes):
        self.nodes = sorted(nodes, key=lambda n: (n.begin, -n.end, n.ordinal))
        self.starts = array('q', (_floor(n.begin) for n in self.nodes))
        size = 1
        while size < len(self.nodes): size *= 2
        self.size = size
        self.maximum = array('q', [-9223372036854775808]) * (size * 2)
        for i, node in enumerate(self.nodes): self.maximum[size + i] = _ceil(node.end)
        for i in range(size - 1, 0, -1): self.maximum[i] = max(self.maximum[i * 2], self.maximum[i * 2 + 1])

    def contains(self, begin, end):
        limit = bisect_right(self.starts, _floor(begin))
        found = []
        def visit(index, left, right):
            if left >= limit or self.maximum[index] < end: return
            if right - left == 1:
                if left < len(self.nodes):
                    node = self.nodes[left]
                    if node.begin <= begin and node.end >= end: found.append(node)
                return
            middle = (left + right) // 2
            visit(index * 2, left, middle); visit(index * 2 + 1, middle, right)
        visit(1, 0, self.size)
        return found

    def point(self, tick, connection_id=None):
        candidates = self.contains(tick, tick)
        exact = [n for n in candidates if n.begin == tick]
        candidates = exact or [n for n in candidates if n.begin < tick < n.end]
        if not candidates: return None, 'no containing event on identical pid/tid'
        score = max(n.begin for n in candidates)
        candidates = [n for n in candidates if n.begin == score]
        finish = min(n.end for n in candidates)
        candidates = [n for n in candidates if n.end == finish]
        if len(candidates) != 1 and connection_id is not None:
            linked = [n for n in candidates if str((n.args or {}).get('connection_id', '')) == str(connection_id)]
            if len(linked) == 1:
                return linked[0], f'unique same-track containing event with exact connection_id={connection_id}'
        if len(candidates) != 1: return None, 'ambiguous same-track interval at flow endpoint'
        return candidates[0], 'exact same-track start' if exact else 'unique deepest same-track containing event'


def _cpu(process, label):
    return 'CPU' in label.upper() or process.casefold() in ('mindspore', 'pytorch', 'python', 'cann')


def _copy(node):
    args = node.args or {}
    task = str(args.get('Task Type', args.get('task_type', args.get('task type', '')))).upper()
    # AI_CORE/AI_VECTOR initialization kernels often end in _MemSet. They
    # remain computation kernels; a substring in their fused name is not DMA.
    if re.search(r'AI[_ ]?CORE|AI[_ ]?VECTOR|AI[_ ]?CPU|MIX[_ ]?AI|AIC|AIV', task):
        return False
    named = re.match(r'^(?:MEMCPY|MEMMOVE|MEMSET|MEMORY[_ ]?COPY)(?:_|[ :]|$)', node.name, re.I)
    return bool(COPY.search(task) or (named and (not task or re.search(r'SDMA|DMA|MEMORY', task))))


def _kernel(node):
    if _copy(node): return False
    args = node.args or {}
    task = str(args.get('Task Type', args.get('task_type', args.get('task type', '')))).upper()
    if task:
        return bool(re.search(r'AI[_ ]?CORE|AI[_ ]?VECTOR|AI[_ ]?CPU|MIX[_ ]?AI|AIC|AIV', task))
    return not re.search(r'notify|wait|event[_ ]?(?:record|reset)|synchronize|hcom|hccl|labelset|labelgoto', node.name, re.I)


class ScanCollector:
    """The original CPU/device/flow index, fed by a shared trace iterator."""
    def __init__(self,origin,targets=None):
        self.origin,self.targets=origin,targets
        self.nodes,self.processes,self.labels,self.threads=defaultdict(list),{},{},{}
        self.flows,self.counts,self.adapter_names=defaultdict(list),Counter(),Counter()
        self.pending=defaultdict(list)

    def feed(self,event,ordinal):
        origin,targets=self.origin,self.targets
        nodes,processes,labels,threads=self.nodes,self.processes,self.labels,self.threads
        flows,counts,adapter_names=self.flows,self.counts,self.adapter_names
        phase = event.get('ph'); name = str(event.get('name', ''))
        pid, tid = str(event.get('pid', '')), str(event.get('tid', ''))
        counts['all_events'] += 1
        if phase == 'M':
            args = event.get('args') or {}
            if name == 'process_name': processes[pid] = str(args.get('name', ''))
            elif name == 'process_labels': labels[pid] = str(args.get('labels', ''))
            elif name == 'thread_name': threads[(pid, tid)] = str(args.get('name', ''))
        elif phase == 'X' and 'ts' in event:
            begin = _tick(event['ts'], origin); duration = _tick(event.get('dur', 0), Decimal(0))
            if duration < 0: raise ValueError(f'Negative duration at ordinal {ordinal}')
            cat = str(event.get('cat') or '')
            args = event.get('args') or {}
            process = processes.get(pid, '')
            if _cpu(process, labels.get(pid, '')):
                args = {key: value for key, value in args.items() if key in STACK_KEYS or re.search(r'external.?id|correlation|connection_id|^Mode$|^level$', key, re.I)}
            else:
                args = {key: value for key, value in args.items() if re.search(r'task.?type|stream|task.?id|op.?type|operation|size\(B\)|block.?dim|call.?stack|correlation|external.?id|connection_id', key, re.I)}
            node = Node(ordinal, pid, tid, begin, begin + duration, sys.intern(name), cat, args or None)
            if _cpu(process, labels.get(pid, '')) or process in ('Ascend Hardware', 'Overlap Analysis') or not process:
                nodes[(pid, tid)].append(node)
            elif targets is None or ordinal in targets:
                self.pending[(pid,tid)].append(node)
            if cat == 'python_function':
                counts['python_events'] += 1
                match = re.search(r'(?:^|[/\\])((?:msadapter|mindtorch)[/\\][^()]*)', name, re.I)
                if match: adapter_names[match.group(1).split('/')[0]] += 1
        elif phase in ('s', 't', 'f') and event.get('cat') in FLOW_CATEGORIES and 'ts' in event:
            flows[(str(event['cat']), str(event.get('id', event.get('id2', ''))))].append(
                (phase, pid, tid, _tick(event['ts'], origin), ordinal, name))
            counts['flow_points'] += 1

    def finish(self,targets=None):
        targets=self.targets if targets is None else targets
        for track,values in self.pending.items():
            selected=[n for n in values if n.ordinal in targets]
            if not selected:continue
            self.nodes[track].extend(selected)
            self.nodes[track].sort(key=lambda n:n.ordinal)
        self.pending.clear()
        return self.nodes,self.processes,self.labels,self.threads,self.flows,self.counts,self.adapter_names


def _scan(trace,origin,targets):
    collector=ScanCollector(origin,targets)
    for ordinal,event in enumerate(_iter_trace(trace)):collector.feed(event,ordinal)
    return collector.finish()


@_acyclic_gc_scope()
def _cached_scan(trace, origin, targets, cache_path):
    """Cache compact CPU/flow indices separately from association rules.

    Rule refinements reuse this fingerprint-checked derived index rather than
    scanning the multi-gigabyte trace again. CPU rows stay outside HTML/report.
    """
    signature = _signature([trace])
    if cache_path and cache_path.is_file():
        try:
            cached = _read_gzip_json(cache_path)
            if cached.get('version') == RAW_INDEX_VERSION and cached.get('fingerprint') == signature and set(targets) <= set(cached.get('target_ordinals', [])):
                names = cached['names']; nodes = defaultdict(list); shift=(_d(cached['origin'])-origin)*SCALE
                if shift==shift.to_integral_value():shift=int(shift)
                def number(value): return _d(value) if isinstance(value, str) else value
                for track in cached['tracks']:
                    pid, tid = track['pid'], track['tid']
                    nodes[(pid, tid)] = [Node(record[0], pid, tid, number(record[1])+shift, number(record[2])+shift, names[record[3]], record[4], record[5]) for record in track['nodes']]
                flows = defaultdict(list)
                for row in cached['flows']:
                    flows[(row['category'], row['id'])] = [tuple([*p[:3], number(p[3])+shift, *p[4:]]) for p in row['points']]
                threads = {(p, t): name for p, t, name in cached['threads']}
                return (nodes, cached['processes'], cached['labels'], threads, flows,
                        Counter(cached['counts']), Counter(cached['adapter_names'])), True
        except (OSError, ValueError, KeyError, TypeError, EOFError):
            pass
    result = _scan(trace, origin, targets)
    if _signature([trace]) != signature: raise ValueError(f'Trace changed during indexing: {trace}')
    if cache_path:
        write_scan_cache(cache_path,result,signature,origin,targets)
    return result, False


def write_scan_cache(cache_path,result,signature,origin,targets):
    nodes, processes, labels, threads, flows, counts, adapter_names = result
    names, name_ids, tracks = [], {}, []
    for (pid, tid), values in nodes.items():
        records = []
        for node in values:
            if node.name not in name_ids:
                name_ids[node.name] = len(names); names.append(node.name)
            records.append([node.ordinal, node.begin, node.end, name_ids[node.name], node.cat, node.args])
        tracks.append({'pid': pid, 'tid': tid, 'nodes': records})
    _write_gzip(cache_path, {'version': RAW_INDEX_VERSION, 'fingerprint': signature, 'origin': str(origin),
        'target_ordinals': sorted(targets),
        'names': names, 'tracks': tracks, 'processes': processes, 'labels': labels,
        'threads': [[p, t, name] for (p, t), name in threads.items()],
        'flows': [{'category': category, 'id': identity, 'points': points} for (category, identity), points in flows.items()],
        'counts': dict(counts), 'adapter_names': dict(adapter_names)})
    # Small secondary indexes are derived while nodes are already in memory.
    # Later diagnosis/phase extraction no longer decompresses the whole CPU index.
    match=re.fullmatch(r'raw_rank_(\d+)\.json\.gz',cache_path.name)
    if match:
        rank=match.group(1);state=_signature([cache_path])
        launches=Counter(n.name.split('LaunchTask::',1)[1] for track,values in nodes.items()
                         if processes.get(track[0],'').casefold()=='mindspore'
                         for n in values if 'LaunchTask::DistComm' in n.name)
        _write_json(cache_path.parent/f'distcomm_counts_rank_{rank}.json',{'fingerprint':state,'counts':dict(launches)})
        python_tracks=[{'pid':track['pid'],'tid':track['tid'],'nodes':[row for row in track['nodes'] if row[4]=='python_function']}
                       for track in tracks]
        _write_gzip(cache_path.parent/f'python_rank_{rank}.json.gz',{
            'version':RAW_INDEX_VERSION,'fingerprint':signature,'origin':str(origin),'source_index_state':state,
            'names':names,'tracks':[track for track in python_tracks if track['nodes']]})


def _flow_index(flows):
    incoming, stats = defaultdict(list), Counter()
    for (category, identity), points in flows.items():
        points.sort(key=lambda p: (p[3], {'s': 0, 't': 1, 'f': 2}[p[0]], p[4]))
        start = None; closed = False; ambiguous = False
        for point in points:
            if point[0] == 's':
                if start is not None and not closed:
                    ambiguous = True; stats['overlapping_start_ids'] += 1
                else: ambiguous = False
                start, closed = point, False
            elif start is not None:
                if ambiguous:
                    stats['ambiguous_edges_skipped'] += 1; continue
                if point[3] < start[3]: continue
                edge = {'category': category, 'flow_id': identity, 'source_point': start, 'target_point': point}
                incoming[(point[1], point[2])].append(edge)
                stats['edges'] += 1
                if point[0] == 't': start, closed = point, False
                else: closed = True
            else: stats['orphan_endpoints'] += 1
    for track in incoming:
        incoming[track].sort(key=lambda edge: edge['target_point'][3])
    return incoming, stats


def _trim(frames, aliases):
    for index, frame in enumerate(frames):
        if any(re.search(r'(?:^|[/\\])' + re.escape(alias) + r'(?:[/\\]|\.)', frame, re.I) for alias in aliases):
            return frames[:index], {'trimmed': True, 'first_adapter_frame': frame, 'aliases': aliases}
    return frames, {'trimmed': False, 'aliases': aliases}


class Associations:
    def __init__(self, indices, incoming, nodes_by_ordinal, aliases, processes=None):
        self.indices, self.incoming, self.nodes_by_ordinal = indices, incoming, nodes_by_ordinal
        self.point_times = {track: array('q', (_floor(e['target_point'][3]) for e in edges)) for track, edges in incoming.items()}
        self.aliases = aliases; self.stacks = {}; self.evidence = {}; self.witnesses = {}
        self.memo = {}; self.endpoint_cache = {}; self.failures = Counter()
        self.partial = {}; self.unavailable_evidence = {}
        processes = processes or {}
        framework = [pid for pid, name in processes.items() if name.casefold() == 'mindspore']
        cann = {pid for pid, name in processes.items() if name.casefold() == 'cann'}
        self.framework_pid = framework[0] if len(framework) == 1 else None
        self.cann_pids = cann

    def witness(self, node):
        identity = str(node.ordinal)
        if identity not in self.witnesses:
            self.witnesses[identity] = {'ordinal': node.ordinal, 'pid': node.pid, 'tid': node.tid,
                'start_us': _us(node.begin), 'end_us': _us(node.end), 'name': node.name, 'category': node.cat}
            if node.cat == 'flow_source_point':
                self.witnesses[identity].update(source_type='flow_source_point',
                    flow_category=(node.args or {}).get('flow_category'), flow_id=(node.args or {}).get('flow_id'),
                    phase=(node.args or {}).get('phase'))
            if node.args:
                self.witnesses[identity]['correlation'] = {key: value for key, value in node.args.items() if re.search(r'connection_id|external.?id|correlation|^Mode$|^level$', key, re.I)}
        return identity

    def point(self, point, connection_id=None):
        key = (point[1], point[2], point[3], str(connection_id))
        if key not in self.endpoint_cache:
            index = self.indices.get((point[1], point[2]))
            self.endpoint_cache[key] = index.point(point[3], connection_id) if index else (None, 'flow endpoint track has no indexed events')
        return self.endpoint_cache[key]

    def python(self, node):
        index = self.indices[(node.pid, node.tid)]
        parents = [n for n in index.contains(node.begin, node.end) if n.cat == 'python_function'
                   and (node.cat != 'flow_source_point' or n.begin <= node.begin < n.end)]
        parents.sort(key=lambda n: (n.begin, -n.end, n.ordinal))
        if not parents: return None
        for left, right in zip(parents, parents[1:]):
            if left.begin > right.begin or left.end < right.end or (left.begin == right.begin and left.end == right.end):
                self.failures['ambiguous_python_nesting'] += 1
                return None
        frames, trimming = _trim([n.name for n in parents], self.aliases)
        if not frames:
            self.failures['adapter_boundary_has_no_parent'] += 1; return None
        digest = hashlib.sha256(json.dumps(frames, ensure_ascii=False).encode()).hexdigest()[:20]
        stack_id = 'stack:' + digest
        self.stacks.setdefault(stack_id, {'frames': frames, 'association': 'recorded Python frames; exact event flow/nesting evidence is stored separately',
            'source': 'trace_view.json python_function events', 'adapter_boundary': trimming})
        return {'stack_id': stack_id, 'python_ordinals': [self.witness(n) for n in parents],
                'origin_node': self.witness(node), 'adapter_boundary': trimming, 'flow_path': []}

    def explicit(self, node):
        for key in STACK_KEYS:
            raw = (node.args or {}).get(key)
            if isinstance(raw, (list, tuple)) and all(isinstance(frame, str) for frame in raw):
                frames = [frame.strip() for frame in raw if frame.strip()]
            elif isinstance(raw, str):
                frames = [frame.strip() for frame in re.split(r'\n|;', raw) if frame.strip()]
            else: continue
            if not frames: continue
            frames, trimming = _trim(frames, self.aliases)
            if not frames: continue
            digest = hashlib.sha256(json.dumps(frames, ensure_ascii=False).encode()).hexdigest()[:20]
            stack_id = 'stack:' + digest
            self.stacks.setdefault(stack_id, {'frames': frames, 'association': 'explicit callstack field on the identical raw operator event',
                'source': 'trace_view.json event.args.' + key, 'frame_order': 'as recorded', 'adapter_boundary': trimming})
            return {'stack_id': stack_id, 'python_ordinals': [], 'origin_node': self.witness(node),
                    'adapter_boundary': trimming, 'flow_path': [], 'explicit_callstack': {'field': key, 'value': raw}}
        return None

    def trace(self, node, active=None, depth=0):
        if node.ordinal in self.memo: return self.memo[node.ordinal]
        if depth > 12: return None
        active = set() if active is None else active
        if node.ordinal in active: return None
        active = active | {node.ordinal}
        direct = self.explicit(node) or self.python(node)
        if direct:
            self.memo[node.ordinal] = direct; return direct
        # MindSpore's own assembler uses these exact launch keywords and OS
        # thread ids to connect CANN pseudo-process APIs to framework launch
        # events. Require full API containment (stronger than its start-only
        # check), a unique launch, and one framework process in this rank trace.
        if node.pid in self.cann_pids and self.framework_pid is not None:
            framework_index = self.indices.get((self.framework_pid, node.tid))
            if framework_index:
                launches = [n for n in framework_index.contains(node.begin, node.end)
                            if 'KernelLaunch' in n.name or 'LaunchTask' in n.name]
                if launches:
                    minimum = min(n.end - n.begin for n in launches)
                    launches = [n for n in launches if n.end - n.begin == minimum]
                    if len(launches) == 1:
                        launch = launches[0]; upstream = self.trace(launch, active, depth + 1)
                        bridge = {'category': 'MindSporeLaunchContainsCANNAPI',
                            'source_node': self.witness(launch), 'target_node': self.witness(node),
                            'same_os_thread': node.tid,
                            'rule': 'unique recorded MindSpore KernelLaunch/LaunchTask fully contains CANN API on identical OS thread; per-rank single framework pseudo-process',
                            'upstream_rule': 'MindSpore AscendTimelineAssembler._create_fwk_to_hardware_flow; EventConstant.KERNEL_LAUNCH_KEYWORDS'}
                        if upstream:
                            result = dict(upstream, flow_path=[bridge] + upstream['flow_path'])
                            self.memo[node.ordinal] = result; return result
                        self.partial[node.ordinal] = [bridge] + self.partial.get(launch.ordinal, [])
        track = (node.pid, node.tid); index = self.indices[track]
        parents = index.contains(node.begin, node.end)
        parents.sort(key=lambda n: (n.end - n.begin, -n.begin, n.ordinal))
        # A parent relation is admitted only on the identical pid/tid and with
        # full containment; no closest predecessor or cross-thread overlap.
        candidates = []
        for parent in parents:
            edges = self.incoming.get(track, []); times = self.point_times.get(track, [])
            for edge in edges[bisect_left(times, _floor(parent.begin)):bisect_right(times, _ceil(parent.end))]:
                if not parent.begin <= edge['target_point'][3] <= parent.end: continue
                target, target_rule = self.point(edge['target_point'])
                if target is None or target.ordinal != parent.ordinal: continue
                connection_id = (target.args or {}).get('connection_id')
                point = edge['source_point']
                source = Node(point[4], point[1], point[2], point[3], point[3], point[5] if len(point) > 5 else edge['category'],
                              'flow_source_point', {'flow_category': edge['category'], 'flow_id': edge['flow_id'], 'phase': point[0]})
                upstream = self.python(source) if (source.pid, source.tid) in self.indices else None
                if upstream:
                    upstream['association_type'] = 'flow_source_point'
                    source_rule = 'original source flow point with strict same-track Python parents'
                else:
                    source, source_rule = self.point(point, connection_id)
                    if source is None: continue
                    upstream = self.trace(source, active, depth + 1)
                proof = {'category': edge['category'], 'flow_id': edge['flow_id'],
                    'source_point': list(edge['source_point']), 'target_point': list(edge['target_point']),
                    'source_node': self.witness(source), 'target_node': self.witness(target),
                    'contained_target_node': self.witness(node),
                    'source_mapping': source_rule, 'target_mapping': target_rule}
                if 'exact connection_id=' in source_rule: proof['connection_id'] = connection_id
                if upstream:
                    candidates.append(dict(upstream, flow_path=[proof] + upstream['flow_path']))
                else:
                    partial = [proof] + self.partial.get(source.ordinal, [])
                    if len(partial) > len(self.partial.get(node.ordinal, [])):
                        self.partial[node.ordinal] = partial
            if candidates: break
        if not candidates:
            self.memo[node.ordinal] = None; return None
        if len({candidate['stack_id'] for candidate in candidates}) != 1:
            self.failures['conflicting_causal_stacks'] += 1
            self.memo[node.ordinal] = None; return None
        result = min(candidates, key=lambda candidate: len(candidate['flow_path']))
        self.memo[node.ordinal] = result
        return result

    def associate(self, node, event_id, save_unavailable=False):
        proof = self.trace(node)
        if proof is None:
            partial = self.partial.get(node.ordinal, [])
            reason = '原数据缺少可唯一关联到Python的完整因果flow链，未按相邻时间补栈。'
            if partial:
                terminal = self.witnesses[partial[-1]['source_node']]
                if 'DistComm' in terminal['name'] and 'LaunchTask' in terminal['name']:
                    reason = '已定位设备/CANN/DistComm LaunchTask，缺少继续到Python的有效上游flow。'
                else:
                    reason = '已保存能证明的设备/CPU部分关联链，但无法唯一继续到Python调用层级。'
            if save_unavailable:
                self.unavailable_evidence[event_id] = {'target_node': self.witness(node), 'flow_path': partial,
                    'status': 'unavailable', 'reason': reason}
            return {'status': 'unavailable', 'reason': reason, 'association_id': event_id,
                    'association_type': 'partial_flow' if partial else 'unavailable', 'known_flow_hops': len(partial)}
        self.evidence[event_id] = dict(proof, target_node=self.witness(node))
        return {'status': 'available', 'stack_id': proof['stack_id'], 'association_id': event_id,
                'stack_truncated': proof['adapter_boundary']['trimmed'],
                'stack_association': proof.get('association_type') or ('explicit_callstack' if proof.get('explicit_callstack') else ('causal_flow' if proof['flow_path'] else 'exact_python_hierarchy')),
                'association_type': proof.get('association_type') or ('causal_flow' if proof['flow_path'] else 'exact_python_hierarchy')}


def _steps(raw_steps, origin):
    return [(str(s['step']), _tick(s['absolute_start_us'], origin), _tick(s['absolute_start_us'], origin) + _tick(s['duration_us'], Decimal(0))) for s in raw_steps]


def _step(begin, steps):
    matches = [step for step, left, right in steps if left <= begin < right]
    return matches[0] if len(matches) == 1 else 'unassigned'


def _rank_report(rank_data, trace, origin, stage, pp_report, scan_cache=None):
    rank = int(rank_data['rank']); comm_targets = {int(e['ordinal']): e for e in rank_data['events']}
    scanned, raw_cache_hit = _cached_scan(trace, origin, comm_targets, scan_cache)
    nodes, processes, labels, threads, flows, counts, adapter_names = scanned
    hardware = {track: values for track, values in nodes.items() if processes.get(track[0]) == 'Ascend Hardware'}
    hardware_counts = {track: sum(_kernel(n) for n in values) for track, values in hardware.items()}
    main = max(hardware_counts, key=lambda track: (hardware_counts[track], sum(n.end - n.begin for n in hardware[track] if _kernel(n)), track)) if hardware_counts else None
    # Only an actual, explicitly named Ascend Hardware process is admitted.
    targets = []
    if main:
        targets.extend(('compute', n) for n in hardware[main] if _kernel(n))
    targets.extend(('copy', n) for values in hardware.values() for n in values if _copy(n))
    free_nodes = [n for track, values in nodes.items() if processes.get(track[0]) == 'Overlap Analysis'
                  for n in values if n.name.casefold() == 'free' or threads.get(track, '').casefold() == 'free']
    targets.extend(('free', n) for n in free_nodes)
    target_ordinals = {n.ordinal for _, n in targets} | set(comm_targets)
    selected_nodes = {n.ordinal: n for values in nodes.values() for n in values if n.ordinal in target_ordinals}
    cpu_tracks = {track: values for track, values in nodes.items() if _cpu(processes.get(track[0], ''), labels.get(track[0], ''))}
    indices = {track: TrackIndex(values) for track, values in cpu_tracks.items()}
    for track, values in nodes.items():
        if track not in indices:
            selected = [n for n in values if n.ordinal in target_ordinals]
            if selected: indices[track] = TrackIndex(selected)
    incoming, flow_stats = _flow_index(flows)
    # mindtorch is enabled only when its alias has been confirmed for this
    # dataset by the source audit; see ADAPTER_ALIASES below.
    aliases = list(ADAPTER_ALIASES)
    associator = Associations(indices, incoming, selected_nodes, aliases, processes)
    steps = _steps(rank_data['steps'], origin)
    events = []
    for kind, node in targets:
        identity = f'{kind}:{rank}:{node.ordinal}'
        association = associator.associate(node, identity) if kind != 'free' else {'status': 'not_applicable', 'reason': 'Overlap Analysis Free is an aggregate interval, not an executed operator'}
        event = {'event_id': identity, 'kind': kind, 'rank': rank, 'stage': stage, 'pp_stage': stage,
                 'step': _step(node.begin, steps), 'pid': node.pid, 'tid': node.tid, 'name': node.name,
                 'absolute_start_us': format(origin + Decimal(node.begin) / SCALE, 'f'),
                 'start_us': _us(node.begin), 'duration_us': _us(node.end - node.begin), 'end_us': _us(node.end),
                 'source_ordinal': node.ordinal, 'stack_status': association['status']}
        if association.get('stack_id'): event['stack_id'] = association['stack_id']
        for key in ('stack_truncated', 'stack_association', 'association_type'):
            if key in association: event[key] = association[key]
        if association.get('reason'): event['stack_reason'] = association['reason']
        if kind != 'free': event['association_id'] = identity
        else: event['stack_reason'] = association['reason']
        if node.args: event['args'] = node.args
        if kind == 'copy':
            event['memory_operation'] = 'Memset' if re.search(r'memset', node.name + str(node.args or {}), re.I) else 'Memcpy'
        events.append(event)
    communication_stacks = {}
    for ordinal in comm_targets:
        identity = f'{rank}:{ordinal}'; node = selected_nodes.get(ordinal)
        communication_stacks[identity] = associator.associate(node, identity, save_unavailable=True) if node else {'status': 'unavailable', 'reason': 'Original communication ordinal was not found in trace'}
    micro_stacks = {}
    py_by_ordinal = {n.ordinal: n for values in cpu_tracks.values() for n in values if n.cat == 'python_function'}
    for region in pp_report.get('compute_regions', []):
        if int(region['rank']) != rank: continue
        identity = region['region_id']; node = py_by_ordinal.get(region.get('source_ordinal'))
        if node and node.name == region.get('source_name'):
            micro_stacks[identity] = associator.associate(node, identity, save_unavailable=True)
        else:
            micro_stacks[identity] = {'status': 'unavailable', 'reason': 'PP source_ordinal/source_name did not resolve to the identical Python event'}
    stream_counts = [{'pid': track[0], 'tid': track[1], 'label': threads.get(track, ''),
                      'kernel_count': hardware_counts[track], 'copy_count': sum(_copy(n) for n in values),
                      'all_event_count': len(values), 'kernel_duration_us': _us(sum(n.end - n.begin for n in values if _kernel(n)))}
                     for track, values in hardware.items()]
    overlap = defaultdict(lambda: defaultdict(Decimal))
    overlap_counts = defaultdict(Counter)
    for track, values in nodes.items():
        if processes.get(track[0]) != 'Overlap Analysis': continue
        for node in values:
            step = _step(node.begin, steps)
            overlap[step][node.name] += Decimal(node.end - node.begin) / SCALE
            overlap_counts[step][node.name] += 1
    return {'events': events, 'stacks': associator.stacks, 'communication_stacks': communication_stacks,
            'micro_stacks': micro_stacks, 'associations': associator.evidence, 'witnesses': associator.witnesses,
            'unavailable_associations': associator.unavailable_evidence,
            'rank_info': {'rank': rank, 'stage': stage, 'compute_pid': main[0] if main else None,
                'compute_tid': main[1] if main else None, 'compute_label': threads.get(main, '') if main else None,
                'source_count': counts['all_events'], 'python_count': counts['python_events'],
                'raw_index_cache_hit': raw_cache_hit,
                'hardware_streams': stream_counts, 'adapter_names_observed': dict(adapter_names),
                'copy_operation_counts': dict(Counter(e['memory_operation'] for e in events if e['kind'] == 'copy')),
                'overlap_summary_us': {step: {name: str(value) for name, value in values.items()} for step, values in overlap.items()},
                'overlap_event_counts': {step: dict(values) for step, values in overlap_counts.items()},
                'selection_rule': 'maximum non-copy computation-kernel count on explicitly named Ascend Hardware process; ties by kernel duration',
                'flow_stats': dict(flow_stats), 'stack_failures': dict(associator.failures)},
            'process_metadata': {'processes': processes, 'labels': labels,
                                 'threads': {f'{pid}:{tid}': name for (pid, tid), name in threads.items()}}}


# Source-audited aliases; keep this explicit rather than guessing from names.
ADAPTER_ALIASES = ('msadapter',)


def _diagnose_distcomm(data, trace, cache_dir, rank):
    from timeline_streams_tlv import diagnose
    matched, records, unique_launches = {}, {}, {}
    for identity, proof in data.get('unavailable_associations', {}).items():
        if identity not in data['communication_stacks']: continue
        chain = [data['witnesses'][proof['target_node']]['name']]
        for edge in proof.get('flow_path', []):
            name = data['witnesses'][edge['source_node']]['name']
            if chain[-1] != name: chain.append(name)
        data['communication_stacks'][identity]['partial_chain'] = chain
        if not proof.get('flow_path'): continue
        terminal = data['witnesses'][proof['flow_path'][-1]['source_node']]
        if 'LaunchTask::DistComm' not in terminal['name']: continue
        name = terminal['name'].split('LaunchTask::', 1)[1]
        matched[identity] = name
        unique_launches[terminal['ordinal']] = name
        records[identity] = proof
    expected = Counter(unique_launches.values())
    # The partial chains may converge on a shared enclosing launch. Compare
    # TLV classes to every actual recorded launch in the compact trace index,
    # rather than equating number of displayed communication ops with launches.
    raw_index = cache_dir / f'raw_rank_{rank}.json.gz'
    count_path = cache_dir / f'distcomm_counts_rank_{rank}.json'
    if raw_index.is_file():
        fingerprint = _signature([raw_index]); counted = None
        if count_path.is_file():
            try:
                candidate = json.loads(count_path.read_text(encoding='utf-8'))
                if candidate.get('fingerprint') == fingerprint: counted = candidate['counts']
            except (OSError, ValueError, KeyError): pass
        if counted is None:
            raw = _read_gzip_json(raw_index)
            name_ids = {index: name.split('LaunchTask::', 1)[1] for index, name in enumerate(raw['names']) if 'LaunchTask::DistComm' in name}
            counted = Counter()
            for track in raw['tracks']:
                if raw['processes'].get(track['pid'], '').casefold() != 'mindspore': continue
                counted.update(name_ids[row[3]] for row in track['nodes'] if row[3] in name_ids)
            _write_json(count_path, {'fingerprint': fingerprint, 'counts': dict(counted)})
        expected = Counter(counted)
    diagnosis = diagnose(trace.parent.parent, expected, cache_dir / f'tlv_rank_{rank}.json')
    diagnosis['trace_launch_counts'] = dict(expected)
    for identity, name in matched.items():
        item = data['communication_stacks'][identity]
        reason = '已定位设备/CANN/DistComm LaunchTask，缺少继续到Python的有效上游flow。'
        if name in diagnosis.get('verified_names', {}):
            reason = '原始DistComm LaunchTask FLOW_ID无效，已定位设备/CANN/LaunchTask，缺少继续到Python的关联。'
            item['tlv_confirmed_invalid_flow'] = True
            records[identity]['tlv_diagnostic'] = {'file': diagnosis['file'], 'name': name,
                'ms_version': diagnosis['ms_version'], **diagnosis['verified_names'][name]}
        item['reason'] = reason; item['stack_reason'] = reason; records[identity]['reason'] = reason
    data['rank_info']['distcomm_tlv_diagnostics'] = diagnosis
    return diagnosis


def collect_timeline_streams(profiling: Path, ranks: list[dict], pp_report: dict,
                             output_dir: Path) -> dict:
    """Return lightweight device rows and deduplicated stacks with raw evidence."""
    output_dir = Path(output_dir); cache_dir = output_dir / 'timeline_cache'; origin = _d(pp_report['time_origin_us'])
    stage_map = {int(e['rank']): e.get('pp_stage', e.get('stage')) for e in pp_report.get('events', [])}
    for g in pp_report.get('groups', []):
        if g.get('kind') == 'pp':
            for stage, rank in enumerate(g['members']): stage_map.setdefault(int(rank), stage)
    stage_map.update({int(k):v for k,v in pp_report.get('rank_stages',{}).items()})
    for row in ranks:
        if row.get('metadata',{}).get('distributed_args',{}).get('pipeline_model_parallel_size')==1:stage_map.setdefault(int(row['rank']),0)
    sources_by_rank = {int(s['rank']): s for s in pp_report.get('sources', []) if 'rank' in s}
    result = {'schema_version': VERSION, 'time_origin_us': str(origin), 'time_unit': 'us',
        'events': [], 'stacks': {}, 'communication_stacks': {}, 'micro_stacks': {}, 'rank_info': {},
        'sources': [], 'notes': ['计算泳道只选真实Ascend Hardware主计算stream；Copy保留所有真实hardware stream上的内存操作。',
            'Free直接提取Overlap Analysis原区间，不从空白位置猜测。',
            'Python栈仅经原flow端点、相同track包含层级或原始显式callstack建立；缺乏证据时显示unavailable。',
            'association_type=flow_source_point表示原始flow下发时刻的Python栈；来源是零长度因果点，不伪装为完整调用区间。',
            '详细关联及原节点证据另存associations.jsonl.gz；HTML不携带全部CPU事件。'], 'warnings': []}
    evidence_path = output_dir / 'streams/associations.jsonl.gz'; evidence_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic(); cache_hits = 0
    with gzip.open(evidence_path, 'wt', encoding='utf-8', compresslevel=3) as evidence_stream:
        for row in sorted(ranks, key=lambda r: int(r['rank'])):
            rank = int(row['rank']); source = sources_by_rank.get(rank, {})
            print(f'设备流与调用链 rank {rank}：读取缓存或建立原始索引', file=sys.stderr, flush=True)
            trace = Path(source['trace']) if source.get('trace') else next(Path(x['path']) for x in row['fingerprint'] if Path(x['path']).name == 'trace_view.json')
            paths = [trace] + [Path(source[k]) for k in ('metadata',) if source.get(k)]
            signature = _signature(paths)
            input_key = hashlib.sha256(json.dumps({'stage': stage_map.get(rank),
                'communication_ordinals': sorted(int(e['ordinal']) for e in row['events']),
                'micro_sources': [(r['region_id'], r.get('source_ordinal'), r.get('source_name'))
                                  for r in pp_report.get('compute_regions', []) if int(r['rank']) == rank]},
                ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cache_path = cache_dir / f'rank_{rank}.json.gz'
            data = None
            if cache_path.is_file():
                try:
                    candidate = _read_gzip_json(cache_path)
                    if candidate.get('version') == VERSION and candidate.get('fingerprint') == signature and candidate.get('time_origin_us') == str(origin) and candidate.get('adapter_aliases') == list(ADAPTER_ALIASES) and candidate.get('input_key') == input_key:
                        data = candidate['data']; cache_hits += 1
                except (OSError, ValueError, EOFError): pass
            if data is None:
                data = _rank_report(row, trace, origin, stage_map.get(rank), pp_report,
                                    scan_cache=cache_dir / f'raw_rank_{rank}.json.gz')
                if _signature(paths) != signature: raise ValueError(f'Rank {rank} changed during extraction')
                _write_gzip(cache_path, {'version': VERSION, 'fingerprint': signature, 'time_origin_us': str(origin), 'adapter_aliases': list(ADAPTER_ALIASES), 'input_key': input_key, 'data': data})
            tlv_diagnosis = _diagnose_distcomm(data, trace, cache_dir, rank)
            result['events'].extend(data['events']); result['stacks'].update(data['stacks'])
            result['communication_stacks'].update(data['communication_stacks']); result['micro_stacks'].update(data['micro_stacks'])
            result['rank_info'][str(rank)] = data['rank_info']
            print(f'  设备事件 {len(data["events"])}，调用栈 {len(data["stacks"])}', file=sys.stderr, flush=True)
            result['sources'].append({'rank': rank, 'trace': str(trace), 'fingerprint': signature,
                                      'framework_tlv': tlv_diagnosis.get('file'), 'tlv_fingerprint': tlv_diagnosis.get('fingerprint')})
            evidence_stream.write(json.dumps({'rank': rank, 'associations': data['associations'], 'witnesses': data['witnesses'],
                'unavailable_associations': data.get('unavailable_associations', {}),
                'process_metadata': data['process_metadata']}, ensure_ascii=False, separators=(',', ':')) + '\n')
    result['events'].sort(key=lambda e: (e['rank'], _d(e['start_us']), e['event_id']))
    result['stats'] = {'event_count': len(result['events']), 'kinds': dict(Counter(e['kind'] for e in result['events'])),
        'stack_count': len(result['stacks']), 'event_stack_status': dict(Counter(e['stack_status'] for e in result['events'])),
        'communication_stack_status': dict(Counter(e['status'] for e in result['communication_stacks'].values())),
        'communication_association_types': dict(Counter(e.get('association_type', 'unavailable') for e in result['communication_stacks'].values())),
        'micro_stack_status': dict(Counter(e['status'] for e in result['micro_stacks'].values())),
        'cache_hits': cache_hits, 'elapsed_seconds': round(time.monotonic() - started, 3)}
    result['association_file'] = 'associations.jsonl.gz'
    _write_json(output_dir / 'streams/report.json', result)
    _write_json(output_dir / 'streams/stack_table.json', result['stacks'])
    with (output_dir / 'streams/events.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        fields = ['event_id', 'kind', 'rank', 'stage', 'step', 'pid', 'tid', 'name', 'start_us', 'duration_us', 'end_us', 'stack_status', 'stack_id']
        writer = csv.DictWriter(stream, fields, extrasaction='ignore'); writer.writeheader(); writer.writerows(result['events'])
    return result
