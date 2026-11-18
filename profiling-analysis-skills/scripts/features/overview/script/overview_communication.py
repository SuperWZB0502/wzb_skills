#!/usr/bin/env python3
"""Independent group-lane communication attribution (TEXT profiler, raw us).

Only ijson is required beyond the Python standard library. The trace is streamed;
metadata, small derived caches and explicit group-role overrides are ordinary JSON.
Unknown roles remain visible in 其他通信. Duration sums and interval unions are
reported separately so simultaneous communication is never silently double counted.
"""
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path
import re

VERSION = 1
CATEGORIES = ('TP', 'CP', 'EP', 'DP', 'PP', 'Grad AR', '其他通信')
GROUP_LANE = re.compile(r'^Group (.+) Communication$')
STEP = re.compile(r'ProfilerStep(?:#|\s*)(\d+)')
RELEVANT_CONTEXT = re.compile(
    r'grad_norm|clip_grad|finish_grad_sync|start_grad_sync|param_gather|param_sync|'
    r'param_and_grad_buffer|distributed_optimizer|token_dispatcher|moe[/\\]mappings|'
    r'all_to_all|alltoall|track_das_indexer|reduce_loss_in_tracker|router_expert_bias|'
    r':barrier$|timers\.py|pipeline_parallel', re.I)
GRAD_CONTEXT = re.compile(r'get_grad_norm|clip_grad_norm|(?:start|finish)_grad_sync', re.I)
EP_CONTEXT = re.compile(r'token_dispatcher|moe[/\\]mappings|all_to_all|alltoall', re.I)
DP_CONTEXT = re.compile(r'(?:start|finish)_grad_sync|param_gather|param_sync|'
                        r'distributed_optimizer|router_expert_bias', re.I)
LOG_CONTEXT = re.compile(r'track_das_indexer|reduce_loss_in_tracker|:barrier$|timers\.py', re.I)
SEMANTIC_ROLES = {
    'tp': 'TP', 'tensor_model_parallel': 'TP', 'moe_tp': 'TP',
    'cp': 'CP', 'cp_ring': 'CP', 'cp_ulysses': 'CP', 'cp_ring_intra': 'CP',
    'pp': 'PP', 'pipeline_model_parallel': 'PP',
    'ep': 'EP', 'exp': 'EP', 'expert_model_parallel': 'EP',
    'dp': 'DP', 'dp_cp': 'DP', 'dp-cp': 'DP',
    'dp_modulo_exp': 'DP', 'dp_modulo_exp_cp': 'DP', 'dp_modulo_ep': 'DP',
}


def _decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f'Invalid profiler time: {value}')
    return result


def _time(value):
    return format(value, 'f')


def _fingerprint(paths):
    return [{'path': str(p.resolve()), 'size': p.stat().st_size,
             'mtime_ns': p.stat().st_mtime_ns} for p in paths]


def _iter_trace(path):
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError('Communication extraction requires ijson: pip install ijson') from exc
    with path.open('rb') as stream:
        first = b''
        while not first:
            chunk = stream.read(4096)
            if not chunk:
                raise ValueError(f'Empty profiler trace: {path}')
            first = chunk.lstrip()
        prefix = 'traceEvents.item' if first.startswith(b'{') else 'item'
        stream.seek(0)
        yield from ijson.items(stream, prefix)


class TraceCollector:
    """Feature1's original extraction rules, usable during the shared input pass."""
    def __init__(self,trace,metadata,rank,seeded=None):
        self.trace,self.metadata,self.rank,self.seeded=trace,metadata,rank,seeded
        self.processes,self.threads,self.candidates,self.contexts,self.steps={},{},[],[],[]

    def feed(self,event,ordinal):
        trace,seeded=self.trace,self.seeded
        processes,threads,candidates,contexts,steps=self.processes,self.threads,self.candidates,self.contexts,self.steps
        if not isinstance(event, dict):
            raise ValueError(f'Non-object trace event in {trace}')
        phase, name = event.get('ph'), str(event.get('name', ''))
        pid, tid = str(event.get('pid', '')), str(event.get('tid', ''))
        if phase == 'M':
            args = event.get('args') or {}
            if name == 'process_name':
                processes[pid] = str(args.get('name', ''))
            elif name == 'thread_name':
                threads[(pid, tid)] = str(args.get('name', ''))
        elif phase == 'X':
            marker = STEP.search(name)
            if marker and seeded is None:
                steps.append({'step': marker.group(1), 'absolute_start_us': str(event['ts']),
                              'duration_us': str(event.get('dur', 0))})
            if '.py(' in name and RELEVANT_CONTEXT.search(name):
                contexts.append({'name': name, 'absolute_start_us': str(event['ts']),
                                 'duration_us': str(event.get('dur', 0))})
            if seeded is None and (name.lower().startswith(('hcom_', 'hccl_'))
                                    or GROUP_LANE.fullmatch(threads.get((pid, tid), ''))):
                candidates.append({'ordinal': ordinal, 'name': name, 'pid': pid, 'tid': tid,
                                   'absolute_start_us': str(event['ts']),
                                   'duration_us': str(event.get('dur', 0)),
                                   'args': event.get('args') or {}})

    def finish(self):
        metadata,rank,seeded=self.metadata,self.rank,self.seeded
        processes,threads,candidates,contexts,steps=self.processes,self.threads,self.candidates,self.contexts,self.steps
        if seeded is not None:
            events, steps = seeded['events'], seeded['steps']
        else:
            events = []
            for event in candidates:
                if processes.get(event['pid'], '').casefold() not in ('communication', 'hccl'):
                    continue
                match = GROUP_LANE.fullmatch(threads.get((event['pid'], event['tid']), ''))
                if not match:
                    continue
                event.update(group_id=match.group(1), rank=rank)
                begin = _decimal(event['absolute_start_us'])
                matching = {s['step'] for s in steps if _decimal(s['absolute_start_us']) <= begin
                            < _decimal(s['absolute_start_us']) + _decimal(s['duration_us'])}
                event['step'] = next(iter(matching)) if len(matching) == 1 else 'unassigned'
                events.append(event)
        return {'rank': rank, 'metadata': metadata, 'steps': steps,
                'events': events, 'contexts': contexts}


def _stream_rank(trace,metadata,rank,seeded=None):
    collector=TraceCollector(trace,metadata,rank,seeded)
    for ordinal,event in enumerate(_iter_trace(trace)):collector.feed(event,ordinal)
    return collector.finish()


def _read_rank(rank, folder, cache_dir, seed_cache_dir):
    trace = folder / 'ASCEND_PROFILER_OUTPUT' / 'trace_view.json'
    metadata_path = folder / 'profiler_metadata.json'
    infos = sorted(folder.glob('profiler_info*.json'))
    if not infos:
        raise ValueError(f'Rank {rank} has no profiler_info JSON: {folder}')
    paths = [trace, metadata_path, infos[0]]
    signature = _fingerprint(paths)
    own_path = cache_dir / f'communication_rank_{rank}.json'
    warning = []
    if own_path.is_file():
        try:
            data = json.loads(own_path.read_text(encoding='utf-8'))
            if (data.get('version') == VERSION and data.get('fingerprint') == signature
                    and data.get('rank') == rank and isinstance(data.get('contexts'), list)):
                return data, 'cache', paths, warning
        except (OSError, ValueError, TypeError):
            warning.append(f'Rank {rank}: invalid communication cache was rebuilt.')
    seed = None
    if seed_cache_dir is not None:
        seed_path = seed_cache_dir / f'rank_{rank}.json'
        if seed_path.is_file():
            try:
                candidate = json.loads(seed_path.read_text(encoding='utf-8'))
                if (candidate.get('version') == 3 and candidate.get('rank') == rank
                        and candidate.get('fingerprint') == signature
                        and isinstance(candidate.get('events'), list)
                        and isinstance(candidate.get('steps'), list)):
                    seed = candidate
            except (OSError, ValueError, TypeError):
                warning.append(f'Rank {rank}: invalid seed cache was ignored.')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    data = _stream_rank(trace, metadata, rank, seed)
    if _fingerprint(paths) != signature:
        raise ValueError(f'Rank {rank} profiling files changed during extraction; rerun.')
    data.update(version=VERSION, fingerprint=signature)
    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = own_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding='utf-8')
    temporary.replace(own_path)
    return data, 'validated seed + streamed CPU context' if seed else 'streamed trace', paths, warning


def _event_contexts(events, contexts):
    """Temporal CPU evidence, without assuming CPU/device execution is synchronous."""
    ranges = sorted((_decimal(c['absolute_start_us']),
                     _decimal(c['absolute_start_us']) + _decimal(c['duration_us']), c['name'])
                    for c in contexts)
    active, index, result = [], 0, {}
    for ordinal, event in sorted(enumerate(events), key=lambda x: _decimal(x[1]['absolute_start_us'])):
        begin = _decimal(event['absolute_start_us'])
        while index < len(ranges) and ranges[index][0] <= begin:
            active.append(ranges[index])
            index += 1
        active = [c for c in active if c[1] >= begin]
        result[ordinal] = sorted({c[2] for c in active})
    return result


def _sizes(config, metadata):
    summary = config.get('summary', config)
    args = metadata.get('distributed_args', {})
    fields = {'TP': 'tensor_model_parallel_size', 'CP': 'context_parallel_size',
              'EP': 'expert_model_parallel_size', 'DP': 'data_parallel_size',
              'PP': 'pipeline_model_parallel_size'}
    output = {}
    for name, field in fields.items():
        value = next((summary[key] for key in (name, name.lower(), name.lower() + '_size', field)
                      if summary.get(key) is not None), args.get(field))
        output[name] = int(value) if value is not None else None
    return output


def _resolve_roles(data, contexts, config):
    groups = data['metadata'].get('parallel_group_info', {})
    by_group = defaultdict(list)
    for i, event in enumerate(data['events']):
        by_group[event['group_id']].append((event, contexts[i]))
    roles, reasons = {}, {}
    overrides = config.get('communication_group_roles', {})
    sizes = _sizes(config, data['metadata'])
    megatron = any('megatron/' in c['name'].replace('\\', '/') for c in data['contexts'])
    expected_dp, expected_ep, expected_dp_mod_ep = set(), set(), set()
    if megatron and all(sizes.get(key) for key in ('TP', 'CP', 'DP', 'PP')):
        step = sizes['TP'] * sizes['CP']
        stage_span = step * sizes['DP']
        stage_base = data['rank'] // stage_span * stage_span
        coordinate = data['rank'] % step
        dp_index = data['rank'] // step % sizes['DP']
        expected_dp = {stage_base + coordinate + d * step for d in range(sizes['DP'])}
        if sizes.get('EP') and sizes['DP'] % sizes['EP'] == 0:
            start = dp_index // sizes['EP'] * sizes['EP']
            expected_ep = {stage_base + coordinate + d * step
                           for d in range(start, start + sizes['EP'])}
            expected_dp_mod_ep = {stage_base + coordinate + d * step
                                 for d in range(dp_index % sizes['EP'], sizes['DP'], sizes['EP'])}
    for group_id, records in by_group.items():
        detail = groups.get(group_id, {})
        group_name = str(detail.get('group_name', ''))
        explicit = overrides.get(group_id, overrides.get(group_name))
        if explicit in CATEGORIES:
            roles[group_id], reasons[group_id] = explicit, 'explicit communication_group_roles override'
            continue
        semantic = SEMANTIC_ROLES.get(group_name.casefold())
        if semantic:
            roles[group_id], reasons[group_id] = semantic, f'metadata group_name={group_name}'
            continue
        members = {int(rank) for rank in detail.get('global_ranks', [])}
        # A pending P2P op can overlap unrelated host dispatch/sync work. Establish
        # pipeline topology before using temporal CPU context for ambiguous groups.
        if megatron and all(sizes.get(key) for key in ('TP', 'CP', 'DP', 'PP')):
            # Megatron tp-cp-dp-pp layout: full coordinates, not a group-size test.
            stride = sizes['TP'] * sizes['CP'] * sizes['DP']
            base = data['rank'] % stride
            expected = {base + stage * stride for stage in range(sizes['PP'])}
            if sizes['PP'] > 1 and members == expected and any(
                    'sendreceive' in event['name'].casefold()
                    or 'sendrecv' in event['name'].casefold()
                    or 'batchsendrecv' in event['name'].casefold()
                    or event['name'].casefold().startswith(('hcom_send', 'hcom_receive'))
                    for event, _ in records):
                roles[group_id], reasons[group_id] = 'PP', 'Megatron pipeline rank coordinates corroborated by P2P operators'
                continue
        if megatron and all(sizes.get(k) for k in ('TP','CP','DP','PP')):
            tp, cp, dp = (sizes[k] for k in ('TP','CP','DP'))
            base = data['rank'] // (tp*cp*dp) * (tp*cp*dp)
            d = data['rank'] // (tp*cp) % dp; c = data['rank'] // tp % cp; t = data['rank'] % tp
            tp_members = {base+d*tp*cp+c*tp+i for i in range(tp)}
            cp_members = {base+d*tp*cp+i*tp+t for i in range(cp)}
            candidates = [domain for domain, expected_members, size, pattern in (
                ('TP',tp_members,tp,r'tensor_parallel[/\\](?:mappings|layers)\.py'),
                ('CP',cp_members,cp,r'context_parallel|ring_attention|ulysses'))
                if size>1 and members==expected_members and any(
                    re.search(r'allreduce|allgather|reducescatter|alltoall|sendrecv|sendreceive',e['name'],re.I)
                    and any(re.search(pattern,n,re.I) for n in names) for e,names in records)]
            if len(candidates)==1:
                roles[group_id],reasons[group_id]=candidates[0],'Megatron coordinates + specific runtime path + collective family'
                continue
        # CPU and device timelines can overlap without a causal relationship.
        # Require the actual collective family plus configured rank coordinates;
        # reject contradictory evidence rather than choosing an arbitrary priority.
        ep_evidence = (sizes.get('EP', 0) and sizes['EP'] > 1 and members == expected_ep
                       and any('alltoall' in event['name'].casefold()
                               and any(EP_CONTEXT.search(name) for name in names)
                               for event, names in records))
        dp_coordinates = bool(members) and members in (expected_dp, expected_dp_mod_ep)
        dp_evidence = dp_coordinates and any(
            ('reducescatter' in event['name'].casefold()
             and any(re.search(r'(?:start|finish)_grad_sync', name, re.I) for name in names))
            or (any(re.search(r'param_gather|param_sync|distributed_optimizer', name, re.I) for name in names)
                and 'allgather' in event['name'].casefold())
            or ('allreduce' in event['name'].casefold()
                and any('router_expert_bias' in name for name in names))
            for event, names in records)
        if ep_evidence and dp_evidence:
            reasons[group_id] = 'conflicting EP and DP runtime evidence; explicit group role required'
        elif ep_evidence:
            roles[group_id], reasons[group_id] = 'EP', 'AllToAll + MoE CPU context + configured EP rank coordinates'
        elif dp_evidence:
            roles[group_id], reasons[group_id] = 'DP', 'gradient/parameter sync or router bias + configured DP rank coordinates'
    return roles, reasons


def _union_duration(events):
    total, last = Decimal(0), None
    ranges = sorted((_decimal(e['absolute_start_us']),
                     _decimal(e['absolute_start_us']) + _decimal(e['duration_us'])) for e in events)
    for begin, end in ranges:
        if last is None:
            total += end - begin
            last = end
        elif end > last:
            total += end - max(begin, last)
            last = end
    return total


def _warm_communication(task):
    rank, folder, cache_dir, seed_dir = task
    data, status, _, _ = _read_rank(rank, Path(folder), Path(cache_dir), Path(seed_dir) if seed_dir else None)
    return {'rank': rank, 'cache_status': status, 'event_count': len(data['events'])}


def collect_communication(profiling, rank_folders, config, cache_dir,
                          seed_cache_dir=None, grad_ar_mode='gradient', workers=1):
    """Return category sums with provenance; input and output durations are us.

    `gradient`: Grad AR includes only allReduce with specific gradient CPU evidence.
    PP otherwise includes communication on an evidenced pipeline group, including
    its logging collectives. `pp_allreduce` moves PP-group allReduce to Grad AR as
    an explicit alternative convention, and does not additionally count it in PP.
    """
    if grad_ar_mode not in ('gradient', 'pp_allreduce'):
        raise ValueError('grad_ar_mode must be gradient or pp_allreduce')
    result = {'per_step_rank': {}, 'notes': [], 'sources': [], 'warnings': [],
              'classifications': [], 'checks': {}, 'cache': {}, 'grad_ar_mode': grad_ar_mode}
    cache_dir = Path(cache_dir)
    seed_cache_dir = Path(seed_cache_dir) if seed_cache_dir is not None else None
    result['notes'].extend([
        '通信分类采用 Group Communication 泳道大算子完整持续时间，包含计算掩盖部分；各事件只归入一类。',
        'Grad AR 仅按具体梯度 CPU 上下文识别 allReduce；PP 域日志 allReduce 在 gradient 模式计入 PP。',
        '未知组或无法确认用途的通信保留为其他通信；分类累计值与区间并集分别核对。',
        'bubble = PP + Grad AR 为自定义估算口径，包含传输和部分日志通信，不等同原工具 receive bubble。',
    ])
    if workers > 1 and len(rank_folders) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_warm_communication, [(int(r),str(f),str(cache_dir),str(seed_cache_dir) if seed_cache_dir else None) for r,f in sorted(rank_folders.items())]))
    for rank, folder in sorted(rank_folders.items()):
        rank = int(rank)
        data, status, paths, warnings = _read_rank(rank, Path(folder), cache_dir, seed_cache_dir)
        result['cache'][rank] = status
        result['warnings'].extend(warnings)
        result['sources'].extend(str(path) for path in paths)
        contexts = _event_contexts(data['events'], data['contexts'])
        roles, reasons = _resolve_roles(data, contexts, config)
        grouped = defaultdict(list)
        unknown = defaultdict(int)
        for step in data['steps']:
            grouped[str(step['step'])]  # Retain measured zero-communication steps.
        for i, event in enumerate(data['events']):
            grouped[str(event.get('step', 'unassigned'))].append((i, event))
        for step, records in grouped.items():
            values = {key: Decimal(0) for key in CATEGORIES}
            for i, event in records:
                group_id, names = event['group_id'], contexts[i]
                category = roles.get(group_id, '其他通信')
                reason = reasons.get(group_id, 'group role is not established by metadata or runtime context')
                allreduce = event['name'].casefold().startswith(('hcom_allreduce', 'hccl_allreduce'))
                is_log = any(LOG_CONTEXT.search(name) for name in names)
                if allreduce and not is_log and any(GRAD_CONTEXT.search(name) for name in names):
                    category, reason = 'Grad AR', 'allReduce inside a specific gradient norm/sync CPU function'
                if allreduce and grad_ar_mode == 'pp_allreduce' and roles.get(group_id) == 'PP':
                    category, reason = 'Grad AR', 'explicit PP-allReduce convention; logging semantics may apply'
                if category == '其他通信':
                    unknown[group_id] += 1
                values[category] += _decimal(event['duration_us'])
                result['classifications'].append({
                    'rank': rank, 'step': step, 'name': event['name'], 'group_id': group_id,
                    'group_name': data['metadata'].get('parallel_group_info', {}).get(group_id, {}).get('group_name'),
                    'category': category, 'reason': reason, 'duration_us': event['duration_us'],
                    'absolute_start_us': event['absolute_start_us'], 'cpu_context': names})
            events = [event for _, event in records]
            duration_sum = sum(values.values())
            interval_union = _union_duration(events)
            result['per_step_rank'].setdefault(step, {})[rank] = {key: _time(value) for key, value in values.items()}
            result['checks'].setdefault(step, {})[rank] = {
                'duration_sum_us': _time(duration_sum), 'interval_union_us': _time(interval_union),
                'overcount_us': _time(duration_sum - interval_union), 'event_count': len(events)}
            if duration_sum != interval_union:
                result['warnings'].append(f'Rank {rank} Step {step}: communication events overlap; '
                    'category duration sums cannot be equated with merged wall time.')
            if step == 'unassigned' and records:
                result['warnings'].append(f'Rank {rank}: {len(records)} communication events are outside an unambiguous ProfilerStep.')
        if unknown:
            result['warnings'].append(f'卡号 {rank}：{len(unknown)} 个组中的 {sum(unknown.values())} 个算子单独保留为“其他通信”，未强行计入六类。')
    result['sources'] = sorted(set(result['sources']))
    return result
