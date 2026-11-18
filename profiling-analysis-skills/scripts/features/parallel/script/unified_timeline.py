"""Assemble the single timeline from independently sourced communication/streams.

Raw per-domain and per-stream outputs remain the audit sources. This module
does not infer domains, reconstruct device tasks or invent Python associations.
"""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal


from parallel_domain_registry import DOMAINS


def _attach_stack(event, association):
    association = association or {}
    for key in ('stack_id', 'stack_status', 'stack_evidence', 'stack_reason',
                'stack_association', 'stack_truncated', 'association_id',
                'association_type', 'partial_chain', 'known_flow_hops'):
        if key in association:
            event[key] = association[key]
    event.setdefault('stack_status', association.get('status', 'unavailable'))
    if not event.get('stack_reason') and association.get('reason'):
        event['stack_reason'] = association['reason']


def build_unified_report(pp_report, domain_reports, streams, markers=None):
    """Return one graph with six domain filters and four independent lane types."""
    from timeline_colors import color_config
    markers = markers or {}
    steps = pp_report['steps']
    warnings = (list(pp_report.get('warnings', [])) + list(pp_report.get('enrichment_warnings', []))
                + list(streams.get('warnings', [])))
    groups, events, seen = [], [], set()
    stack_table = streams.get('stacks', streams.get('stack_table', {}))
    communication_stacks = streams.get('communication_stacks', {})
    display_domains=(*DOMAINS,'unresolved') if domain_reports.get('unresolved',{}).get('events') else DOMAINS
    reports = [('PP', pp_report)] + [(d, domain_reports[d]) for d in display_domains[1:] if d in domain_reports]
    for domain, report in reports:
        for group in report.get('groups', []):
            groups.append(dict(group, domain=domain))
        for original in report.get('events', []):
            if str(original['step']) not in steps:
                continue
            identity = original['event_id']
            if identity in seen:
                raise ValueError(f'统一时间线的通信事件重复归域：{identity}')
            seen.add(identity)
            event = dict(original, kind='communication', domain=domain)
            if streams.get('stack_requested',True):_attach_stack(event, communication_stacks.get(identity))
            else:event.update(stack_status='not_requested',stack_reason='本次仅导出通信，未请求调用栈分析。')
            events.append(event)
    for original in streams.get('events', []):
        if str(original['step']) not in steps:
            continue
        event = dict(original)
        if event['kind'] not in ('compute', 'copy', 'free'):
            raise ValueError(f'不支持的设备流类型：{event["kind"]}')
        if event['event_id'] in seen:
            raise ValueError(f'统一时间线的设备事件ID重复：{event["event_id"]}')
        seen.add(event['event_id'])
        event.setdefault('domain', None)
        event.setdefault('stack_status', 'unavailable')
        events.append(event)
    for event in events:
        if Decimal(str(event['end_us'])) != Decimal(str(event['start_us'])) + Decimal(str(event['duration_us'])):
            raise ValueError(f'事件起止与持续时间不闭合：{event["event_id"]}')
        if event.get('stack_id') and event['stack_id'] not in stack_table:
            raise ValueError(f'事件引用不存在的调用栈：{event["event_id"]}')
    rank_info = dict(streams.get('rank_info', {}))
    for source in pp_report.get('sources',[]):
        rank=str(source['rank']);rank_info.setdefault(rank,{'rank':int(rank),'stage':pp_report.get('rank_stages',{}).get(rank)})
    rank_stages = {}
    for rank, info in rank_info.items():
        if info.get('stage') is not None:
            rank_stages[str(rank)] = info['stage']
    for event in events:
        rank, stage = str(event['rank']), event.get('stage')
        if stage is None:
            continue
        if rank in rank_stages and rank_stages[rank] != stage:
            raise ValueError(f'Rank {rank} 的PP Stage信息不一致')
        rank_stages[rank] = stage
    # The original PP report retains every measured segment. A visual envelope
    # needs its exact endpoints and a reference, not 145k duplicate segments.
    regions = []
    for source in pp_report.get('compute_regions', []):
        if str(source['step']) not in steps:
            continue
        region = {key: value for key, value in source.items() if key != 'segments'}
        region['source_region_id'] = source['region_id']
        region['source_report'] = 'report.json'
        region['source_segments_count'] = len(source.get('segments', []))
        _attach_stack(region, streams.get('micro_stacks', {}).get(source['region_id']))
        regions.append(region)
    counts = Counter(event['kind'] for event in events)
    domain_counts = Counter(event['domain'] for event in events if event['kind'] == 'communication')
    stack_counts = Counter(event.get('stack_status', 'unavailable') for event in events if event['kind'] != 'free')
    notes = [
        '计算、通信、拷贝与Free各自分行显示，区间可以相互重叠，不按上下行相加解释为端到端时间。',
        '计算流采用每卡Ascend Hardware中计算任务最密集的真实stream；拷贝包括主stream与独立stream中的实际copy任务。',
        'Free来自已有Overlap Analysis记录，不由图中白色背景推断。',
        '灰色micro为已验证调度示意；详情起止及跨度对应显示边界，原始设备计算片段保留在PP报告中。',
        'Python栈仅使用可验证的关联证据；未找到关联时显示原因，不以最近或设备执行时CPU重叠调用代替。',
        '未确认通信保留在domains/unresolved及统一图的未确认选项，不强行归域或静默丢弃。',
        '计算、拷贝、Free沿用原工具普通Timeline的名称哈希与浅色主题12色色板；通信域按用户示意图固定配色。',
    ] + list(streams.get('notes', [])) + list(markers.get('notes', []))
    return {
        'schema_version': 1, 'feature_id': 'unified-parallel-timeline', 'mode': 'unified',
        'domain': 'UNIFIED', 'title': '5D并行通信图', 'domains': list(display_domains), 'includes_unresolved':True,
        'time_unit': 'us', 'time_origin_us': pp_report['time_origin_us'],
        'profiling_path': pp_report.get('profiling_path'),
        'generated_at': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'steps': steps, 'groups': groups, 'events': events,
        'compute_regions': regions, 'micro_flows': pp_report.get('micro_flows', []),
        'optimizer_regions': markers.get('optimizer_regions', []),
        'recompute_regions': markers.get('recompute_regions', []),
        'phase_marker_stats': markers.get('stats', {}), 'color_config': color_config(),
        'micro_configuration': pp_report.get('micro_configuration', {}),
        'rank_stages': rank_stages, 'rank_info': rank_info,
        'stack_table': stack_table, 'warnings': warnings, 'notes': notes,
        'sources': pp_report.get('sources', []) + streams.get('sources', []),
        'stats': {'event_count': len(events), 'by_kind': dict(counts),
                  'by_domain': dict(domain_counts), 'stack_status_counts': dict(stack_counts),
                  'rank_count': len(rank_stages), 'stream_extraction': streams.get('stats', {}),
                  'phase_markers': markers.get('stats', {})},
    }
