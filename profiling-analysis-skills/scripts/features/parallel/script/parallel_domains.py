#!/usr/bin/env python3
"""Extract independent DP/EP/CP/TP timelines, retaining a complete group audit.

This module belongs entirely to feature 2. It does not import feature 1, infer a
domain from group size alone, merge separate communicators with equal members,
or fabricate rows for ranks whose traces were not collected.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from decimal import Decimal
import csv
import json
from pathlib import Path
import re

from domain_trace import (_event_contexts, _read_rank, _resolve_roles, _sizes,
                          GRAD_CONTEXT, LOG_CONTEXT, SEMANTIC_ROLES)
from global_communication import (identify_global_group, is_global_name,
                                  validate_global_members, purpose_of_event)

from parallel_domain_registry import STAGE_DOMAINS as DOMAINS
from mindspeed_domains import model_evidence, initialization_roles
from hyperparallel_domains import (candidate_axes, causal_group_usages, corroborated_axis,
                                   exported_group_ids, mesh_evidence, single_pipeline_stage)
CONFIG_FIELDS = {'TP': 'tensor_model_parallel_size', 'CP': 'context_parallel_size',
                 'DP': 'data_parallel_size', 'EP': 'expert_model_parallel_size',
                 'PP': 'pipeline_model_parallel_size'}


def _dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')
    tmp.replace(path)


def _csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict))
                             else value for key, value in row.items()})


def _normal_overrides(overrides):
    result = dict((overrides or {}).get('communication_group_roles', overrides or {}))
    for identity, role in result.items():
        if role not in (*DOMAINS, 'PP', 'GLOBAL', 'unresolved'):
            raise ValueError(f'通信组 {identity} 的域应为 PP/DP/EP/CP/TP/GLOBAL/unresolved，实际为 {role!r}')
    return result


def _semantic_group_file(profiling, ranks):
    """Read the same real pg_name provenance consumed by msinsight.

    Never treat collective/p2p rank_set or numeric pg_name as a domain name.
    Full communicator ids are primary; a unique alias + identical rank_set is
    accepted only when the exported file omitted the full id.
    """
    lookup, source_entries, warnings = {}, [], []
    candidates = [profiling / 'cluster_analysis_output/communication_group.json',
                  profiling / 'communication_group.json',
                  profiling.parent / 'cluster_analysis_output/communication_group.json']
    metadata_groups = {}
    for row in ranks:
        for identity, detail in row.get('metadata', {}).get('parallel_group_info', {}).items():
            metadata_groups.setdefault(identity, detail)
    for path in dict.fromkeys(candidates):
        if not path.is_file(): continue
        try:
            content = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            warnings.append(f'通信组语义文件无法读取：{path}：{exc}'); continue
        for index, detail in enumerate(content.get('comm_group_parallel_info', [])):
            if not isinstance(detail, dict): continue
            pg_name = str(detail.get('pg_name', ''))
            if not SEMANTIC_ROLES.get(pg_name.casefold()) and not is_global_name(pg_name): continue
            identity = str(detail.get('group_id', ''))
            if identity not in metadata_groups:
                members = set(map(int, detail.get('rank_set', [])))
                matches = [key for key, meta in metadata_groups.items()
                           if str(meta.get('group_name', '')) == str(detail.get('group_name', ''))
                           and set(map(int, meta.get('global_ranks', []))) == members]
                if len(matches) != 1: continue
                identity = matches[0]
            evidence = {'file': str(path), 'json_path': f'comm_group_parallel_info[{index}].pg_name',
                        'pg_name': pg_name, 'group_id': identity}
            previous = lookup.get(identity)
            previous_role = ('GLOBAL' if is_global_name(previous['pg_name']) else SEMANTIC_ROLES.get(previous['pg_name'].casefold())) if previous else None
            current_role = 'GLOBAL' if is_global_name(pg_name) else SEMANTIC_ROLES.get(pg_name.casefold())
            if previous and (previous.get('conflict') or previous_role != current_role):
                lookup[identity] = dict(evidence, conflict=True)
                warnings.append(f'通信组 {identity} 的语义pg_name冲突，保留未确认。')
            else:
                lookup[identity] = evidence
            source_entries.append(evidence)
    return lookup, source_entries, warnings


def _tp_cp_roles(data, contexts, sizes, roles, reasons):
    """Conservative Megatron coordinate + named CPU-path fallback.

    TP/CP numeric groups require corroborating runtime paths. The zero-size
    single-rank case is deliberately not inferred from singleton groups.
    """
    if not any('megatron/' in c['name'].replace('\\', '/') for c in data['contexts']):
        return
    if not all(sizes.get(key) for key in ('TP', 'CP', 'DP', 'PP')):
        return
    tp, cp, dp = (sizes[key] for key in ('TP', 'CP', 'DP'))
    rank = data['rank']; span = tp * cp * dp
    base = rank // span * span
    d = rank // (tp * cp) % dp; c = rank // tp % cp; t = rank % tp
    expected = {
        'TP': {base + d * tp * cp + c * tp + i for i in range(tp)},
        'CP': {base + d * tp * cp + i * tp + t for i in range(cp)},
    }
    for group_id, detail in data['metadata'].get('parallel_group_info', {}).items():
        if group_id in roles:
            continue
        members = set(map(int, detail.get('global_ranks', [])))
        records = [(e, contexts[i]) for i, e in enumerate(data['events']) if e['group_id'] == group_id]
        candidates = []
        for domain, path in [('TP', r'tensor_parallel[/\\](?:mappings|layers)\.py'),
                             ('CP', r'context_parallel|ring_attention|ulysses')]:
            if sizes[domain] > 1 and members == expected[domain] and any(
                    re.search(r'allreduce|allgather|reducescatter|alltoall|sendreceive|sendrecv', e['name'], re.I)
                    and any(re.search(path, name, re.I) for name in names) for e, names in records):
                candidates.append(domain)
        if len(candidates) == 1:
            roles[group_id] = candidates[0]
            reasons[group_id] = f'Megatron {candidates[0]} rank coordinates + specific runtime CPU path + collective family'


def _proven_default_layout(ranks, pp_report):
    layouts = set()
    for row in ranks:
        args = row.get('metadata', {}).get('distributed_args', {})
        try:
            world, pp = int(args['world_size']), int(args['pipeline_model_parallel_size'])
            span = int(args['tensor_model_parallel_size']) * int(args['context_parallel_size']) * int(args['data_parallel_size'])
            if min(world, pp, span) <= 0 or world != pp * span:
                return None
            layouts.add((world, pp, span))
        except (KeyError, ValueError, TypeError):
            return None
    if len(layouts) != 1:
        return None
    world, pp, span = next(iter(layouts))
    # The formula is enabled only after an actual PP communicator, including
    # its stage order, corroborates the default stride. Configuration alone
    # does not establish how the framework placed ranks.
    if pp == 1:
        return world, span
    full_groups = [g for g in pp_report.get('groups', []) if g.get('kind') == 'pp'
                   and len(g.get('members', [])) == pp]
    if not full_groups or not all(g['members'] == list(range(min(g['members']), world, span))
                                 and min(g['members']) < span for g in full_groups):
        return None
    return world, span


def _rank_stage_map(ranks, pp_report):
    mapping = {}
    for group in pp_report.get('groups', []):
        if group.get('kind') == 'pp':
            for stage, rank in enumerate(group['members']):
                mapping[int(rank)] = stage
    layout = _proven_default_layout(ranks, pp_report)
    if layout:
        world, span = layout
        # Include unsampled metadata members for topology only. This never
        # creates an event, a measured duration or an available-rank entry.
        known_ranks = {int(row['rank']) for row in ranks}
        for row in ranks:
            for group in row.get('metadata', {}).get('parallel_group_info', {}).values():
                known_ranks.update(map(int, group.get('global_ranks', [])))
        for rank in known_ranks:
            if 0 <= rank < world:
                mapping.setdefault(rank, rank // span)
    return mapping


def _load_context_rank(profiling, row, cache_dir, source_map):
    rank = int(row['rank'])
    source = source_map.get(rank, {})
    metadata = Path(source['metadata']) if source.get('metadata') else None
    if metadata is None:
        signature = row.get('fingerprint', [])
        metadata = next((Path(p['path']) for p in signature if Path(p['path']).name == 'profiler_metadata.json'), None)
    if metadata is None:
        # Small in-memory fixtures can still use semantic metadata and explicit
        # overrides; missing CPU evidence becomes unresolved without failing PP.
        return dict(row, contexts=row.get('contexts', [])), 'in-memory/no CPU context', []
    if isinstance(row.get('contexts'),list):
        return dict(row), 'same-pass contexts', []
    data, status, paths, warnings = _read_rank(rank, metadata.parent, cache_dir, None)
    return data, status, [*warnings]


def _source_compute_regions(domain, groups, pp_report, output_dir):
    """Reuse verified same-rank PP micro blocks as scheduling illustrations.

    No communication-domain-specific uncovered time is recalculated here.
    The original PP report retains all fine-grained computation segments.
    """
    source_regions = defaultdict(list)
    for region in pp_report.get('compute_regions', []):
        if not region.get('region_id') or region.get('phase') not in ('f', 'b'):
            continue
        if region.get('micro') is None or str(region.get('step')) not in pp_report.get('steps', {}):
            continue
        source_regions[int(region['rank'])].append(region)
    result = []
    for group in groups:
        for rank in group['available_ranks']:
            for source in source_regions.get(int(rank), []):
                if domain != 'GLOBAL' and source.get('stage') != group.get('pp_stage'):
                    continue
                region = {key: value for key, value in source.items() if key != 'segments'}
                region.update(region_id=f"{domain.lower()}:{group['group_id']}:{source['region_id']}",
                    group_id=group['group_id'], pp_stage=source.get('stage') if domain == 'GLOBAL' else group['pp_stage'],
                    source_region_id=source['region_id'], source_group_id=source['group_id'],
                    source_segments_count=len(source.get('segments', [])),
                    source_report=str(Path(output_dir).resolve() / 'report.json'),
                    source_report_relative='../../report.json',
                    source_basis='沿用原PP报告相同rank、step和micro的已验证计算调度示意；未重新计算本通信域的未掩盖量。')
                result.append(region)
    return result


def build_domain_reports(profiling: Path, ranks: list[dict], pp_report: dict,
                         output_dir: Path, cache_dir: Path = None,
                         overrides: dict = None, log_dir=None) -> dict:
    """Write output_dir/domains/{DP,EP,CP,TP,GLOBAL}, returning five reports.

    Explicit overrides map communicator id (preferred) or metadata alias to a
    domain, e.g. {"<group_id>": "EP"}. Unknown communication remains in
    domains/unresolved rather than being silently attributed to DP or EP.
    """
    profiling, output_dir = Path(profiling), Path(output_dir)
    cache_dir = Path(cache_dir) if cache_dir else output_dir / 'domain_cache'
    overrides = _normal_overrides(overrides)
    semantic_groups, semantic_sources, semantic_warnings = _semantic_group_file(profiling, ranks)
    source_map = {int(s['rank']): s for s in pp_report.get('sources', []) if 'rank' in s}
    stage_map = _rank_stage_map(ranks, pp_report)
    sampled = {int(row['rank']) for row in ranks}
    configs = [_sizes({}, row.get('metadata', {})) for row in ranks]
    sizes, config_warnings = {}, []
    for domain in (*DOMAINS, 'PP'):
        distinct = {c[domain] for c in configs if c.get(domain) is not None}
        sizes[domain] = next(iter(distinct)) if len(distinct) == 1 else None
        if len(distinct) > 1:
            config_warnings.append(f'{domain}并行度在不同采集卡的配置中冲突：{sorted(distinct)}；全局并行度保留未知。')
    world_values = set()
    for row in ranks:
        value = row.get('metadata', {}).get('distributed_args', {}).get('world_size')
        try:
            if value is not None and int(value) > 0: world_values.add(int(value))
        except (TypeError, ValueError):
            config_warnings.append(f"卡号{row['rank']}的world_size无效；GLOBAL身份保留未知。")
            world_values.add(None)
    sizes['GLOBAL'] = next(iter(world_values)) if len(world_values) == 1 else None
    if len(world_values) > 1:
        config_warnings.append('不同采集卡的world_size配置冲突，GLOBAL身份保留未知。')
    warnings, cache_status, rows = [*semantic_warnings, *config_warnings], {}, []
    for row in ranks:
        data, status, local_warnings = _load_context_rank(profiling, row, cache_dir, source_map)
        # Enrich an in-memory copy only; raw metadata and fingerprint caches are
        # unchanged, and the export path/JSON location remain auditable below.
        data = dict(data, metadata=dict(data.get('metadata', {})))
        data['metadata']['parallel_group_info'] = {
            key: dict(detail) for key, detail in data['metadata'].get('parallel_group_info', {}).items()}
        for identity, evidence in semantic_groups.items():
            if identity in data['metadata']['parallel_group_info'] and not evidence.get('conflict'):
                data['metadata']['parallel_group_info'][identity]['pg_name'] = evidence['pg_name']
        cache_status[int(row['rank'])] = status
        warnings.extend(local_warnings)
        rows.append(data)
    evidence=model_evidence(profiling,log_dir)
    meshes=mesh_evidence(profiling,log_dir)
    mesh_single_stage=bool(meshes) and single_pipeline_stage(profiling,log_dir) and not any(stage_map.values())
    initialization,initialization_audit=initialization_roles(rows,profiling,evidence)
    _dump(output_dir/'domains/initialization_evidence.json',initialization_audit)
    catalog, role_evidence = {}, defaultdict(list)
    all_events = []
    runtime_sizes=defaultdict(set)
    export_ids=defaultdict(set)
    causal_usages=defaultdict(Counter)
    pp_group_ids = {g['group_id'] for g in pp_report.get('groups', []) if g.get('kind') == 'pp'}
    pp_event_ids = {e.get('event_id', f"{e['rank']}:{e['ordinal']}") for e in pp_report.get('events', [])}
    origin = Decimal(str(pp_report.get('time_origin_us', 0)))
    for data in rows:
        rank = int(data['rank'])
        trace=source_map.get(rank,{}).get('trace')
        if trace:
            for identity, ids in exported_group_ids(data['events'],Path(trace).with_name('communication.json')).items():
                export_ids[identity].update(ids)
            if meshes:
                index=output_dir/f'timeline_cache/raw_rank_{rank}.json.gz'
                for identity, counts in causal_group_usages(data['events'],index,Path(trace)).items():
                    causal_usages[identity].update(counts)
        contexts = _event_contexts(data['events'], data.get('contexts', []))
        roles, reasons = _resolve_roles(data, contexts, {'communication_group_roles': overrides})
        _tp_cp_roles(data, contexts, _sizes({}, data['metadata']), roles, reasons)
        for group_id, detail in data['metadata'].get('parallel_group_info', {}).items():
            members = [int(v) for v in detail.get('global_ranks', [])]
            group = catalog.setdefault(group_id, {'group_id': group_id, 'members': members,
                'aliases': [], 'parallel_subtypes': [], 'observed_on_ranks': [], 'classification_evidence': [], 'event_count': 0})
            if group['members'] != members:
                warnings.append(f'通信组 {group_id} 不同卡的成员元数据冲突，保留未确认。')
                role_evidence[group_id].append(('unresolved', 'conflicting member metadata'))
            alias = str(detail.get('group_name', group_id))
            if alias not in group['aliases']: group['aliases'].append(alias)
            group['observed_on_ranks'].append(rank)
            explicit = overrides.get(group_id, overrides.get(alias))
            role, reason = None, ''
            if explicit is not None:
                role, reason = explicit, 'explicit group-domain override'
                if role == 'GLOBAL':
                    confirmed, reason = validate_global_members(members, sizes['GLOBAL'], reason)
                    if not confirmed: role = 'unresolved'
            elif semantic_groups.get(group_id, {}).get('conflict'):
                role, reason = 'unresolved', 'conflicting communication_group.json semantic pg_name'
            else:
                # Authority order: exported/direct semantic pg_name; semantic
                # group_name/type; only then corroborated runtime inference.
                for field in ('pg_name', 'group_name', 'type'):
                    candidate = 'GLOBAL' if is_global_name(detail.get(field)) else SEMANTIC_ROLES.get(str(detail.get(field, '')).casefold())
                    if candidate:
                        if str(detail[field]) not in group['parallel_subtypes']:
                            group['parallel_subtypes'].append(str(detail[field]))
                        if role is None:
                            role, reason = candidate, f'metadata {field}={detail[field]}'
                            if role == 'GLOBAL':
                                confirmed, reason = identify_global_group(detail, sizes['GLOBAL'])
                                if not confirmed: role = 'unresolved'
                if role is None:
                    constructed=initialization.get(group_id)
                    if constructed:
                        role,reason=constructed['domain'],constructed['reason']
                        group['domain_aliases']=constructed.get('domain_aliases',[])
                    else:
                        role, reason = roles.get(group_id), reasons.get(group_id, '')
            if group_id in pp_group_ids and role is None:
                role, reason = 'PP', 'existing PP report verified communicator and PP member order'
            if role:
                role_evidence[group_id].append((role, reason or 'explicit group-domain override'))
            group['classification_evidence'].append({'rank': rank, 'metadata': source_map.get(rank, {}).get('metadata'),
                'group_name': alias, 'pg_name': detail.get('pg_name'),
                'semantic_export': semantic_groups.get(group_id),
                'candidate_domain': role, 'reason': reason or 'no verified role'})
        for i, event in enumerate(data['events']):
            group_id = event['group_id']
            if group_id not in catalog:
                catalog[group_id] = {'group_id': group_id, 'members': [], 'aliases': [], 'observed_on_ranks': [rank],
                                     'classification_evidence': [], 'event_count': 0}
            group=catalog[group_id]
            if rank not in group['observed_on_ranks']:
                group['observed_on_ranks'].append(rank)
            size=event.get('args',{}).get('rank_size')
            if str(size).isdigit() and int(size)>0:
                runtime_sizes[group_id].add(int(size))
            names = contexts[i]
            category = None
            if 'allreduce' in event['name'].casefold() and not any(LOG_CONTEXT.search(n) for n in names) and any(GRAD_CONTEXT.search(n) for n in names):
                category = 'Grad AR'
            start = Decimal(str(event['absolute_start_us'])) - origin
            duration = Decimal(str(event['duration_us']))
            value = dict(event, event_id=f"{rank}:{event['ordinal']}", stage=stage_map.get(rank),
                pp_stage=stage_map.get(rank), start_us=str(start), end_us=str(start + duration),
                cpu_context=names, metric_category=category,
                op_type=re.split(r'__\d', event['name'])[0])
            all_events.append(value)
            catalog[group_id]['event_count'] += 1
    for group_id, group in catalog.items():
        candidates = {role for role, _ in role_evidence[group_id]}
        role = next(iter(candidates)) if len(candidates) == 1 and 'unresolved' not in candidates else 'unresolved'
        reasons = sorted({reason for _, reason in role_evidence[group_id] if reason})
        if len(export_ids[group_id])==1:
            group['export_group_id']=next(iter(export_ids[group_id]))
        sizes_seen=runtime_sizes[group_id]
        usages=causal_usages[group_id]
        if usages:
            group['causal_usages']=dict(sorted(usages.items()))
            if set(usages)=={'gradient_clip'} and usages['gradient_clip']==group['event_count']:
                group['explicit_purpose']='梯度裁剪'
                reasons.append('全组已采集事件通过 HCCL 异步连线关联到梯度裁剪')
        if len(sizes_seen)==1:
            size=next(iter(sizes_seen))
            group['observed_rank_size']=size
            candidates=candidate_axes(meshes,group['observed_on_ranks'],size,group['members'])
            if candidates:
                group['mesh_candidates']=[{'axis':axis,'domain':domain} for axis,domain in candidates]
                reasons.append('DeviceMesh 候选轴：'+', '.join(axis for axis,_ in candidates))
            if mesh_single_stage and not role_evidence[group_id] and usages:
                supported=corroborated_axis(meshes,group,size,usages)
                if supported:
                    axis,role,members=supported
                    group['members']=members
                    group['members_basis']='DeviceMesh 轴切片与运行时调用证据印证'
                    group['mesh_axis']=axis
                    reasons.append(f'HyperParallel {axis}：显式 DeviceMesh、实测参与数及异步 HCCL 调用链一致')
                    for rank in members:stage_map.setdefault(rank,0)
        member_stages = {stage_map[r] for r in group['members'] if r in stage_map}
        all_member_stages_known = bool(group['members']) and all(r in stage_map for r in group['members'])
        pp_stage = next(iter(member_stages)) if len(member_stages) == 1 and all_member_stages_known else None
        if role in DOMAINS and pp_stage is None:
            reasons.append('communicator is not proven to stay within one PP stage')
            role = 'unresolved'
        unexpected = sorted(set(group['observed_on_ranks']) - set(group['members']))
        if unexpected and role in (*DOMAINS, 'GLOBAL'):
            role = 'unresolved'
            reasons.append('observed rank is absent from recorded communicator members')
        if role == 'GLOBAL': pp_stage = None
        group.update(domain=role, kind=role.lower(), pp_stage=pp_stage,
            available_ranks=sorted(set(group['members']) & sampled),
            missing_ranks=sorted(set(group['members']) - sampled),
            unexpected_ranks=unexpected,
            complete=bool(group['members']) and set(group['members']) <= sampled,
            classification_reason='; '.join(reasons) or 'no semantic metadata or corroborated runtime role',
            observed_on_ranks=sorted(set(group['observed_on_ranks'])))
    for event in all_events:
        group = catalog[event['group_id']]
        event.update(domain=group['domain'],domain_aliases=group.get('domain_aliases',[]),classification_reason=group['classification_reason'])
        if group.get('mesh_axis') and event['stage'] is None:
            event['stage']=event['pp_stage']=group['pp_stage']
        if group.get('explicit_purpose'):
            event['purpose_category']=group['explicit_purpose']
        if event['domain'] == 'GLOBAL':
            event['source_metric_category'] = event.get('metric_category')
            event['purpose_category'], event['purpose_reason'] = purpose_of_event(event)
            event['metric_category'] = event['purpose_category']
    stage_count = sizes.get('PP') or (max(stage_map.values()) + 1 if stage_map else 1)
    reports = {}
    base = output_dir / 'domains'
    for domain in (*DOMAINS, 'GLOBAL', 'unresolved'):
        groups = sorted((g for g in catalog.values() if g['domain'] == domain), key=lambda g: (g['pp_stage'] if g['pp_stage'] is not None else -1, g['group_id']))
        events = [e for e in all_events if e['domain'] == domain]
        shared=[{'group_id':g['group_id'],'source_domain':g['domain'],'report':'../'+g['domain']+'/report.json',
                 'members':g['members'],'basis':'CP=1时初始化复用同一HCCL句柄，不重复计事件'}
                for g in catalog.values() if domain in g.get('domain_aliases',[])]
        local_warnings = list(dict.fromkeys(warnings))
        if domain in DOMAINS and sizes.get(domain) == 1 and not events:
            local_warnings.append(f'{domain}=1，本次未启用跨卡{domain}通信；不以其他单卡通信组冒充该域。')
        if any(g['missing_ranks'] for g in groups):
            local_warnings.append('部分通信组仅采集到部分卡：图中只呈现实际采集数据，缺失卡不补造算子。')
        compute_regions = _source_compute_regions(domain, groups, pp_report, output_dir) if domain in (*DOMAINS, 'GLOBAL') else []
        report = {'schema_version': 1, 'feature_id': f'{domain.lower()}-schedule', 'domain': domain,
            'parallel_size': sizes.get(domain) or (len(groups[0]['members']) if groups and len({len(g['members']) for g in groups})==1 else len(shared[0]['members']) if shared else None), 'pp_stages': list(range(stage_count)),
            'shared_group_refs':shared,
            'stage_scoped': domain in DOMAINS,
            'rank_stages': {str(rank): stage_map.get(rank) for rank in sorted(sampled)},
            'time_unit': 'us', 'time_origin_us': str(origin), 'profiling_path': str(profiling),
            'groups': groups, 'events': events, 'steps': pp_report.get('steps', {}),
            'compute_regions': compute_regions, 'micro_flows': [], 'warnings': local_warnings,
            'compute_region_scope_note': '灰色micro块沿用原PP报告相同rank/step的计算调度示意，不代表重新计算本通信域的未掩盖量。',
            'sources': pp_report.get('sources', []), 'semantic_group_sources': semantic_sources, 'cache': cache_status,
            'stats': {'rank_count': len({e['rank'] for e in events}), 'group_count': len(groups),
                      'event_count': len(events), 'compute_region_count': len(compute_regions)},
            'empty_reason': (f'{domain}=1，未启用跨卡通信' if sizes.get(domain) == 1 else '未发现可确认归属于该域的通信组') if not events else None}
        directory = base / domain
        _dump(directory / 'report.json', report)
        _dump(directory / 'group_catalog.json', groups)
        _csv(directory / 'communication_events.csv', events, ['event_id', 'group_id', 'domain', 'pp_stage', 'rank', 'step', 'name', 'start_us', 'duration_us', 'absolute_start_us', 'metric_category', 'purpose_category', 'purpose_reason', 'classification_reason', 'cpu_context'])
        summary = []
        grouped = defaultdict(list)
        for e in events: grouped[(e['pp_stage'], e['group_id'], e['rank'], e['step'])].append(e)
        for (stage, group_id, rank, step), records in sorted(grouped.items(), key=lambda pair: str(pair[0])):
            summary.append({'pp_stage': stage, 'group_id': group_id, 'rank': rank, 'step': step,
                            'event_count': len(records), 'duration_us': str(sum((Decimal(e['duration_us']) for e in records), Decimal(0)))})
        _csv(directory / 'rank_summary.csv', summary, ['pp_stage', 'group_id', 'rank', 'step', 'event_count', 'duration_us'])
        if domain == 'GLOBAL':
            by_purpose = defaultdict(list)
            for event in events: by_purpose[event['purpose_category']].append(event)
            purposes = [{'purpose_category': purpose, 'event_count': len(records),
                         'duration_us': str(sum((Decimal(e['duration_us']) for e in records), Decimal(0))),
                         'event_ids': [e['event_id'] for e in records],
                         'op_types': dict(Counter(e['op_type'] for e in records)),
                         'evidence_note': 'Specific recorded gradient/timer/barrier CPU context; temporal overlap is not a claimed causal link.'}
                        for purpose, records in sorted(by_purpose.items())]
            _csv(directory / 'purpose_summary.csv', purposes, ['purpose_category', 'event_count', 'duration_us', 'op_types', 'event_ids', 'evidence_note'])
        reports[domain] = report
    identity_counts = Counter(e['event_id'] for e in all_events)
    assigned = Counter(e['domain'] for e in all_events)
    verified_pp = {e['event_id'] for e in all_events if e['domain'] == 'PP'}
    audit = {'schema_version': 1, 'input_event_count': len(all_events), 'unique_event_count': len(identity_counts),
        'duplicate_event_ids': [key for key, count in identity_counts.items() if count != 1],
        'assigned_event_counts': dict(assigned), 'closure_ok': len(identity_counts) == len(all_events) and sum(assigned.values()) == len(all_events),
        'pp_report_event_count': len(pp_event_ids), 'pp_classified_event_count': len(verified_pp),
        'pp_report_not_covered_ids': sorted(pp_event_ids - verified_pp),
        'additional_pp_event_ids': sorted(verified_pp - pp_event_ids),
        'groups': list(catalog.values()), 'events': all_events, 'sources': pp_report.get('sources', []),
        'semantic_group_sources': semantic_sources,
        'cache': cache_status, 'overrides': overrides,
        'notes': ['通信域与度量分类分别保留：DP域梯度AllReduce仍属于DP，metric_category可为Grad AR。',
                  'GLOBAL身份要求明确world名称及完整world成员；梯度用途不决定通信域。',
                  '相同成员的不同真实group_id分别保留；空元数据通信组也保留在目录。']}
    _dump(base / 'audit.json', audit)
    _dump(base / 'sources.json', {'rank_sources': pp_report.get('sources', []),
                                'semantic_group_sources': semantic_sources})
    return reports
