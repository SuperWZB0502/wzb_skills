#!/usr/bin/env python3
"""Verify device-lane extraction, CSV Free closure and every available stack."""
from collections import Counter, defaultdict
from decimal import Decimal
import argparse
import csv
import gzip
import json
from pathlib import Path
import re
import struct


def _contains(outer, inner):
    return Decimal(outer['start_us']) <= Decimal(inner['start_us']) and Decimal(outer['end_us']) >= Decimal(inner['end_us'])


def verify(output_dir):
    import ijson
    output_dir = Path(output_dir)
    from free_boundaries import analyze
    free_checks = {(str(r['rank']),r['step']):r for r in analyze(output_dir)['checks']}
    report = json.loads((output_dir / 'streams/report.json').read_text(encoding='utf-8'))
    pp = json.loads((output_dir / 'report.json').read_text(encoding='utf-8'))
    raw_ranks = [json.loads(p.read_text(encoding='utf-8')) for p in sorted((output_dir / 'cache').glob('rank_*.json'))]
    cache_versions = {}
    for rank in (row['rank'] for row in raw_ranks):
        versions = {}
        for prefix, expected in [('rank', 4), ('raw_rank', 1)]:
            with gzip.open(output_dir / f'timeline_cache/{prefix}_{rank}.json.gz', 'rb') as stream:
                version = next(ijson.items(stream, 'version'))
            assert version == expected
            versions[prefix] = version
        cache_versions[str(rank)] = versions
    raw_comm = {f"{r['rank']}:{e['ordinal']}" for r in raw_ranks for e in r['events']}
    micro = {r['region_id']: r for r in pp.get('compute_regions', [])}
    assert set(report['communication_stacks']) == raw_comm
    assert set(report['micro_stacks']) == set(micro)
    ids = [e['event_id'] for e in report['events']]
    assert len(ids) == len(set(ids))
    event_by_id = {e['event_id']: e for e in report['events']}
    by_rank = defaultdict(list)
    free = defaultdict(Decimal)
    available = {}
    for event in report['events']:
        by_rank[str(event['rank'])].append(event)
        assert event['kind'] in ('compute', 'copy', 'free')
        assert event['event_id'] == f"{event['kind']}:{event['rank']}:{event['source_ordinal']}"
        assert Decimal(event['absolute_start_us']) - Decimal(report['time_origin_us']) == Decimal(event['start_us'])
        assert Decimal(event['end_us']) - Decimal(event['start_us']) == Decimal(event['duration_us'])
        assert Decimal(event['duration_us']) >= 0
        if event['kind'] == 'free':
            assert event['stack_status'] == 'not_applicable'
            free[(str(event['rank']), event['step'])] += Decimal(event['duration_us'])
        else:
            assert event['stack_status'] in ('available', 'unavailable')
            if event['stack_status'] == 'available': available[event['event_id']] = event['stack_id']
            else: assert event.get('stack_reason')
    for collection in ('communication_stacks', 'micro_stacks'):
        for identity, item in report[collection].items():
            assert item['status'] in ('available', 'unavailable')
            if item['status'] == 'available': available[identity] = item['stack_id']
            else: assert item.get('reason')
    for identity, stack_id in available.items(): assert stack_id in report['stacks']
    checked = set(); partial_checked = set(); path_categories = Counter(); stack_boundaries = Counter(); processes_by_rank = {}; tlv_checked = 0
    with gzip.open(output_dir / 'streams/associations.jsonl.gz', 'rt', encoding='utf-8') as stream:
        for line in stream:
            data = json.loads(line); rank = str(data['rank']); nodes = data['witnesses']
            metadata = data['process_metadata']; processes_by_rank[rank] = metadata
            for identity, proof in data['associations'].items():
                assert available[identity] == proof['stack_id']
                assert identity not in checked; checked.add(identity)
                target = nodes[proof['target_node']]; origin = nodes[proof['origin_node']]
                if identity in event_by_id:
                    event = event_by_id[identity]
                    assert target['ordinal'] == event['source_ordinal']
                    assert target['pid'] == event['pid'] and target['tid'] == event['tid']
                if identity in micro:
                    assert target['ordinal'] == micro[identity]['source_ordinal']
                    assert target['name'] == micro[identity]['source_name']
                if proof.get('explicit_callstack'):
                    raw = proof['explicit_callstack']['value']
                    frames = raw if isinstance(raw, list) else [f.strip() for f in re.split(r'\n|;', raw) if f.strip()]
                else:
                    parents = [nodes[key] for key in proof['python_ordinals']]
                    assert parents
                    for parent in parents:
                        assert parent['category'] == 'python_function'
                        assert (parent['pid'], parent['tid']) == (origin['pid'], origin['tid'])
                        assert _contains(parent, origin)
                        if origin.get('source_type') == 'flow_source_point':
                            assert Decimal(parent['start_us']) <= Decimal(origin['start_us']) < Decimal(parent['end_us'])
                            assert origin['start_us'] == origin['end_us']
                    for parent, child in zip(parents, parents[1:]):
                        assert _contains(parent, child)
                        assert (parent['start_us'], parent['end_us']) != (child['start_us'], child['end_us'])
                    frames = [n['name'] for n in parents]
                boundary = proof['adapter_boundary']; cut = len(frames)
                for i, name in enumerate(frames):
                    if any(re.search(r'(?:^|[/\\])' + re.escape(alias) + r'(?:[/\\]|\.)', name, re.I) for alias in boundary['aliases']):
                        cut = i; break
                assert report['stacks'][proof['stack_id']]['frames'] == frames[:cut]
                assert boundary['trimmed'] == (cut < len(frames))
                if boundary['trimmed']: assert boundary['first_adapter_frame'] == frames[cut]
                stack_boundaries['trimmed' if boundary['trimmed'] else 'no_adapter_boundary_in_recorded_parent_chain'] += 1
                downstream = target
                for edge in proof['flow_path']:
                    path_categories[edge['category']] += 1
                    source, destination = nodes[edge['source_node']], nodes[edge['target_node']]
                    if edge['category'] == 'MindSporeLaunchContainsCANNAPI':
                        assert destination['ordinal'] == downstream['ordinal']
                        assert source['tid'] == destination['tid'] == edge['same_os_thread']
                        assert metadata['processes'][source['pid']].casefold() == 'mindspore'
                        assert metadata['processes'][destination['pid']].casefold() == 'cann'
                        assert 'KernelLaunch' in source['name'] or 'LaunchTask' in source['name']
                        assert _contains(source, destination)
                    else:
                        assert edge['category'] in ('HostToDevice', 'async_npu', 'async_mindspore', 'async_task_queue')
                        contained = nodes[edge['contained_target_node']]
                        assert contained['ordinal'] == downstream['ordinal']
                        assert (contained['pid'], contained['tid']) == (destination['pid'], destination['tid'])
                        assert _contains(destination, contained)
                        for point, node in [(edge['source_point'], source), (edge['target_point'], destination)]:
                            assert (point[1], point[2]) == (node['pid'], node['tid'])
                            assert Decimal(node['start_us']) * 1000 <= Decimal(str(point[3])) <= Decimal(node['end_us']) * 1000
                        assert Decimal(str(edge['source_point'][3])) <= Decimal(str(edge['target_point'][3]))
                        if 'connection_id' in edge:
                            assert str(source.get('correlation', {}).get('connection_id')) == str(edge['connection_id'])
                            assert str(destination.get('correlation', {}).get('connection_id')) == str(edge['connection_id'])
                        if source.get('source_type') == 'flow_source_point':
                            assert source['ordinal'] == edge['source_point'][4]
                            assert source['flow_category'] == edge['category'] and source['flow_id'] == edge['flow_id']
                            assert source['phase'] == edge['source_point'][0]
                    downstream = source
                assert downstream['ordinal'] == origin['ordinal']
            for identity, proof in data.get('unavailable_associations', {}).items():
                item = report['communication_stacks'].get(identity, report['micro_stacks'].get(identity))
                assert item and item['status'] == 'unavailable' and identity not in available
                assert 'stack_id' not in item
                assert item.get('known_flow_hops', 0) == len(proof['flow_path'])
                downstream = nodes[proof['target_node']]
                if identity in raw_comm:
                    assert downstream['ordinal'] == int(identity.split(':')[1])
                    expected_names = [downstream['name']]
                    for edge in proof['flow_path']:
                        name = nodes[edge['source_node']]['name']
                        if expected_names[-1] != name: expected_names.append(name)
                    assert item['partial_chain'] == expected_names
                for edge in proof['flow_path']:
                    source, destination = nodes[edge['source_node']], nodes[edge['target_node']]
                    if edge['category'] == 'MindSporeLaunchContainsCANNAPI':
                        assert destination['ordinal'] == downstream['ordinal']
                        assert source['tid'] == destination['tid'] == edge['same_os_thread']
                        assert metadata['processes'][source['pid']].casefold() == 'mindspore'
                        assert metadata['processes'][destination['pid']].casefold() == 'cann'
                        assert 'KernelLaunch' in source['name'] or 'LaunchTask' in source['name']
                        assert _contains(source, destination)
                    else:
                        contained = nodes[edge['contained_target_node']]
                        assert contained['ordinal'] == downstream['ordinal']
                        assert (contained['pid'], contained['tid']) == (destination['pid'], destination['tid'])
                        assert _contains(destination, contained)
                        for point, node in [(edge['source_point'], source), (edge['target_point'], destination)]:
                            assert (point[1], point[2]) == (node['pid'], node['tid'])
                            assert Decimal(node['start_us']) * 1000 <= Decimal(str(point[3])) <= Decimal(node['end_us']) * 1000
                        if 'connection_id' in edge:
                            assert str(source.get('correlation', {}).get('connection_id')) == str(edge['connection_id'])
                            assert str(destination.get('correlation', {}).get('connection_id')) == str(edge['connection_id'])
                    downstream = source
                if item.get('tlv_confirmed_invalid_flow'):
                    diagnostic = proof['tlv_diagnostic']; name = diagnostic['name']
                    diagnosis = report['rank_info'][rank]['distcomm_tlv_diagnostics']
                    assert diagnosis['malformed'] is None and diagnosis['ms_version'] == '2.10.0'
                    assert diagnostic['file'] == diagnosis['file']
                    confirmed = diagnosis['verified_names'][name]
                    assert confirmed['valid_count'] == 0 and confirmed['invalid_count'] > 0
                    assert confirmed['invalid_count'] == diagnosis['trace_launch_counts'][name]
                    assert confirmed['invalid_count'] == diagnosis['counts'][name]['invalid']
                    assert diagnosis['counts'][name].get('valid', 0) == 0
                    with Path(diagnostic['file']).open('rb') as raw:
                        raw.seek(confirmed['sample']['offset'])
                        record_type, length = struct.unpack('<HI', raw.read(6))
                        assert length >= 54
                        fixed = struct.unpack('<5Qi3Hb3?', raw.read(54))
                        assert fixed[1] == (1 << 64) - 1 and fixed[6] == 3 and fixed[7] == 32
                    tlv_checked += 1
                partial_checked.add(identity)
    assert checked == set(available), 'Every available stack requires complete saved evidence'
    csv_checks = []; rank_summaries = {}
    for row in raw_ranks:
        rank = str(row['rank']); info = report['rank_info'][rank]; metadata = processes_by_rank[rank]
        main = next(s for s in info['hardware_streams'] if s['pid'] == info['compute_pid'] and s['tid'] == info['compute_tid'])
        assert metadata['processes'][main['pid']] == 'Ascend Hardware'
        assert main['kernel_count'] == max(s['kernel_count'] for s in info['hardware_streams'])
        kernels = [e for e in by_rank[rank] if e['kind'] == 'compute']
        copies = [e for e in by_rank[rank] if e['kind'] == 'copy']
        assert len(kernels) == main['kernel_count']
        assert all(e['pid'] == main['pid'] and e['tid'] == main['tid'] for e in kernels)
        assert len(copies) == sum(s['copy_count'] for s in info['hardware_streams'])
        for event in copies:
            assert metadata['processes'][event['pid']] == 'Ascend Hardware'
            task = str(event.get('args', {}).get('Task Type', '')).upper()
            assert not re.search(r'AI[_ ]?CORE|AI[_ ]?VECTOR|AI[_ ]?CPU|MIX[_ ]?AI|AIC|AIV', task)
        for event in by_rank[rank]:
            if event['kind'] == 'free':
                assert metadata['processes'][event['pid']] == 'Overlap Analysis'
                assert event['name'] == 'Free' or metadata['threads'][event['pid'] + ':' + event['tid']] == 'Free'
        trace = next(Path(p['path']) for p in row['fingerprint'] if Path(p['path']).name == 'trace_view.json')
        with (trace.parent / 'step_trace_time.csv').open(encoding='utf-8-sig', newline='') as stream:
            for csv_row in csv.DictReader(stream):
                step = csv_row['Step']; measured = free[(rank, step)]
                boundary = free_checks[(rank,step)]
                assert boundary['passed'], (rank,step,boundary)
                overlap = info['overlap_summary_us'].get(step, {})
                for key in ('Computing', 'Communication', 'Communication(Not Overlapped)'):
                    assert Decimal(overlap.get(key, 0)) == Decimal(csv_row[key]), (rank, step, key)
                csv_checks.append({'rank': int(rank), 'step': step, 'Free_us': str(measured), 'original_csv_Free_us': csv_row['Free'], 'comparison':boundary['explanation'],'step_clipped_Free_us':boundary['step_clipped_free_us'], 'passed': True})
        rank_summaries[rank] = {'main_stream': main['label'], 'compute_count': len(kernels), 'copy_count': len(copies),
            'copy_same_stream': sum(e['tid'] == main['tid'] for e in copies),
            'copy_other_streams': sum(e['tid'] != main['tid'] for e in copies),
            'free_count': sum(e['kind'] == 'free' for e in by_rank[rank]), 'copy_operations': info['copy_operation_counts']}
    return {'passed': True, 'stats': report['stats'], 'rank_summary': rank_summaries,
            'cache_versions': cache_versions,
            'free_original_csv_checks': csv_checks, 'available_associations_checked': len(checked),
            'unavailable_partial_associations_checked': len(partial_checked),
            'communication_invalid_flow_tlv_diagnostics_checked': tlv_checked,
            'causal_edge_categories': dict(path_categories), 'adapter_boundaries': dict(stack_boundaries),
            'checks': ['Real Ascend Hardware main stream, never CANN host API lane',
                       'Copies from every hardware stream; AI compute kernels named MemSet remain compute',
                       'Original Overlap Analysis and Free durations exactly match independently generated step CSV',
                       'Every available stack has saved flow/nesting evidence with original ordinals',
                       'Every Python parent fully contains its source on the identical process/thread',
                       'CANN bridge requires a unique recorded MindSpore launch and full same-OS-thread containment',
                       'Adapter cutoff reproduces the exact prefix before the first recorded msadapter frame',
                       'All original communication events and PP micro regions have an explicit stack status']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'output')
    parser.add_argument('--result', type=Path, default=Path(__file__).resolve().parents[1] / 'validation/timeline_streams_verification.json')
    args = parser.parse_args(); data = verify(args.output_dir)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(data, ensure_ascii=False))
