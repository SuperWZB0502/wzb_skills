#!/usr/bin/env python3
"""Feature 2 standalone streaming extraction and conservative group-domain evidence.

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

VERSION = 2
CATEGORIES = ('TP', 'CP', 'EP', 'DP', 'PP', 'Grad AR', '其他通信')
GROUP_LANE = re.compile(r'^(?:\d+:)?Group (.+) Communication$')
STEP = re.compile(r'ProfilerStep(?:#|\s*)(\d+)')
RELEVANT_CONTEXT = re.compile(
    r'grad_norm|clip_grad|finish_grad_sync|start_grad_sync|param_gather|param_sync|'
    r'param_and_grad_buffer|distributed_optimizer|token_dispatcher|moe[/\\]mappings|'
    r'all_to_all|alltoall|track_das_indexer|reduce_loss_in_tracker|router_expert_bias|'
    r':barrier$|timers\.py|pipeline_parallel|tensor_parallel|context_parallel|'
    r'ring_attention|ulysses|all_to_all_sp2hp|all_to_all_hp2sp', re.I)
GRAD_CONTEXT = re.compile(r'get_grad_norm|clip_grad_norm|(?:start|finish)_grad_sync', re.I)
EP_CONTEXT = re.compile(r'token_dispatcher|moe[/\\]mappings|all_to_all|alltoall', re.I)
DP_CONTEXT = re.compile(r'(?:start|finish)_grad_sync|param_gather|param_sync|'
                        r'distributed_optimizer|router_expert_bias', re.I)
LOG_CONTEXT = re.compile(r'track_das_indexer|reduce_loss_in_tracker|:barrier$|timers\.py', re.I)
SEMANTIC_ROLES = {
    'tp': 'TP', 'tensor_model_parallel': 'TP', 'moe_tp': 'TP', 'moetp': 'TP',
    'nd1_dim1': 'TP', 'nd1_dim2': 'TP', 'nd2_dim1': 'TP', 'nd2_dim2': 'TP',
    'cp': 'CP', 'cp_ring': 'CP', 'cp_ulysses': 'CP', 'cp_ring_intra': 'CP',
    'pp': 'PP', 'pipeline_model_parallel': 'PP',
    'ep': 'EP', 'exp': 'EP', 'expert_model_parallel': 'EP',
    'dp': 'DP', 'dp_cp': 'DP', 'dp-cp': 'DP',
    'dp_modulo_exp': 'DP', 'dp_modulo_exp_cp': 'DP', 'dp_modulo_ep': 'DP',
}
from parallel_domain_registry import DOMAINS, SEMANTIC_EXTENSIONS, EXTENDED_DOMAINS
SEMANTIC_ROLES.update(SEMANTIC_EXTENSIONS)
CATEGORIES=(*CATEGORIES,*EXTENDED_DOMAINS)


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


def _stream_rank(trace, metadata, rank, seeded=None):
    processes, threads, candidates, contexts, steps = {}, {}, [], [], []
    for ordinal, event in enumerate(_iter_trace(trace)):
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
        if explicit in (*CATEGORIES, *DOMAINS):
            roles[group_id], reasons[group_id] = explicit, 'explicit communication_group_roles override'
            continue
        semantic = next((SEMANTIC_ROLES.get(str(detail.get(field, '')).casefold())
                         for field in ('pg_name', 'type', 'group_name')
                         if SEMANTIC_ROLES.get(str(detail.get(field, '')).casefold())), None)
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

