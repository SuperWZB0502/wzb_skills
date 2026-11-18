"""Adam device envelopes and independent checkpoint layer replay segments.

Uses fingerprint-validated device/Python/flow indices from timeline_streams.
The CLI refreshes annotations from existing parse products without rescanning traces.
"""
from collections import Counter, defaultdict
from decimal import Decimal as D
import gzip
import gc
import json
from pathlib import Path
import re

CHECKPOINT_BACKWARD = re.compile(r'(?:tensor_parallel/random|utils/checkpoint)\.py\(\d+\):backward$')
REPLAY_FORWARD = re.compile(r'(?:transformer_block|transformer_layer)\.py\(\d+\):custom_forward$')
ADAM = re.compile('adam', re.I)


def annotate_layer_numbers(markers, report, evidence=None):
    """Name proven per-layer block replay segments in reverse local-layer order."""
    from mindspeed_domains import model_evidence
    evidence=evidence or model_evidence(report['profiling_path'],report.get('model_log_dir'))
    config=evidence['values'];replays=defaultdict(list)
    for replay in markers.get('recompute_regions',[]):replays[replay['parent_region_id']].append(replay)
    counts=config.get('recompute_num_layers');layers=config.get('num_layer_list')
    valid=config.get('recompute_method')=='block' and config.get('virtual_pipeline_model_parallel_size') in (None,1)
    for parent,records in replays.items():
        stage=records[0].get('stage');expected=counts[stage] if isinstance(counts,list) and isinstance(stage,int) and stage<len(counts) else counts
        total=layers[stage] if isinstance(layers,list) and isinstance(stage,int) and stage<len(layers) else None
        proven=valid and isinstance(expected,int) and expected>0 and len(records)==expected and (total is None or expected<=total)
        for index,replay in enumerate(sorted(records,key=lambda value:D(value['display_start_us']))):
            number=expected-index if proven else None
            replay.update(layer_number=number,label=f'L{number}r' if number is not None else 'L?r',
                          layer_number_basis='block重算前K个本地层；完整K段证据按设备显示时间逆序编号' if proven else '层号证据不足，不按不完整片段补造')
            if proven and isinstance(layers,list):replay['global_layer_number']=sum(layers[:stage])+number
    markers['layer_configuration']={'values':{key:config.get(key) for key in ('num_layer_list','recompute_num_layers','recompute_method')},'sources':evidence['sources']}
    return markers


def _read_index(path):
    # These acyclic JSON indices contain many tiny lists; avoid repeated GC scans.
    enabled = gc.isenabled()
    try:
        if enabled: gc.disable()
        with gzip.open(path, 'rt') as handle: return json.load(handle)
    finally:
        if enabled: gc.enable()


def collect_phase_markers(pp_report, streams, output_dir):
    output_dir = Path(output_dir)
    markers, recompute, sources = [], [], []
    by_rank = defaultdict(list)
    for region in pp_report['compute_regions']:
        if str(region['step']) in pp_report['steps']:
            by_rank[int(region['rank'])].append(region)
    for rank in streams['rank_info']:
        by_rank.setdefault(int(rank), [])
    stream_sources = {int(s['rank']): s for s in streams['sources']}
    for rank, regions in sorted(by_rank.items()):
        cache = output_dir / 'timeline_cache'
        derived = _read_index(cache / f'rank_{rank}.json.gz')
        source = stream_sources[rank]
        for item in source['fingerprint']:
            stat = Path(item['path']).stat()
            if stat.st_size != item['size'] or stat.st_mtime_ns != item['mtime_ns']:
                raise ValueError(f'Rank {rank}: source changed; run the full workflow')
        if derived['fingerprint'] != source['fingerprint'] or derived['time_origin_us'] != pp_report['time_origin_us']:
            raise ValueError(f'Rank {rank}: phase marker cache does not match current profiling')
        data = derived['data']
        # CPU optimizer.step and asynchronous Adam device execution have different times.
        adam_by_step = defaultdict(list)
        for event in data['events']:
            if event['kind'] == 'compute' and ADAM.search(event['name']) and str(event['step']) in pp_report['steps']:
                adam_by_step[str(event['step'])].append(event)
        for step, events in sorted(adam_by_step.items()):
            start, end = min(D(e['start_us']) for e in events), max(D(e['end_us']) for e in events)
            first = min(events, key=lambda e: D(e['start_us']))
            markers.append({'region_id': f'opt:{rank}:{step}:adam', 'rank': rank,
                'stage': streams['rank_info'][str(rank)].get('stage'), 'step': step, 'phase': 'opt', 'label': 'opt',
                'start_us': str(start), 'end_us': str(end), 'duration_us': str(end-start),
                'display_start_us': str(start), 'display_end_us': str(end),
                'source_name': first['name'], 'source_ordinal': first['source_ordinal'],
                'source_pid': first['pid'], 'source_tid': first['tid'], 'event_ids': [e['event_id'] for e in events],
                'kernel_count': len(events), 'kernel_duration_us': str(sum((D(e['duration_us']) for e in events), D(0))),
                'evidence_level': 'device_adam',
                'basis': '主计算流中名称含Adam的真实设备算子，从首个开始到末个结束；包络包含间隙，不等于Adam算子时长之和',
                **{key: first[key] for key in ('stack_status', 'stack_id', 'stack_reason') if key in first}})
        raw_path=cache/f'raw_rank_{rank}.json.gz'
        compact=cache/f'python_rank_{rank}.json.gz'
        raw=None
        if compact.exists():
            from timeline_streams import _signature
            candidate=_read_index(compact)
            if candidate.get('source_index_state')==_signature([raw_path]):raw=candidate
        if raw is None:raw=_read_index(raw_path)
        if raw['fingerprint'] != [s for s in source['fingerprint'] if Path(s['path']).name == 'trace_view.json']:
            raise ValueError(f'Rank {rank}: Python source index does not match current profiling')
        # Shared ingestion stores exact absolute ns; convert only display times.
        # Containment tests stay in one unchanged raw-index coordinate system.
        shift=(D(raw['origin'])-D(pp_report['time_origin_us']))*1000
        def raw_time(value):
            tick=D(value)+shift
            if tick==tick.to_integral_value():tick=int(tick)
            return str(D(tick)/1000)
        names = raw['names']
        backward = {str(r['source_ordinal']): r for r in regions if r['phase'] == 'b'}
        host_replays = {}
        for track in raw['tracks']:
            checkpoints, replays, phases = [], [], []
            for row in track['nodes']:
                if row[4] != 'python_function': continue
                name = names[row[3]]
                if CHECKPOINT_BACKWARD.search(name): checkpoints.append(row)
                elif REPLAY_FORWARD.search(name): replays.append(row)
                if str(row[0]) in backward: phases.append(row)
            for row in replays if phases else ():
                parents = [p for p in checkpoints if D(p[1]) <= D(row[1]) < D(row[2]) <= D(p[2])]
                phase = [p for p in phases if D(p[1]) <= D(row[1]) < D(row[2]) <= D(p[2])]
                if not parents or len(phase) != 1: continue
                checkpoint = min(parents, key=lambda p: D(p[2])-D(p[1]))
                # Deduplicate wrappers of ONE checkpoint, never merge separate layers.
                wrappers = [p for p in replays if D(checkpoint[1]) <= D(p[1]) <= D(row[1])
                            and D(row[2]) <= D(p[2]) <= D(checkpoint[2])
                            and (D(p[1]) < D(row[1]) or D(row[2]) < D(p[2]))]
                if wrappers: continue
                host_replays[str(row[0])] = {
                    'ordinal': row[0], 'pid': track['pid'], 'tid': track['tid'],
                    'start_us': raw_time(row[1]), 'end_us': raw_time(row[2]),
                    'name': names[row[3]], 'category': 'python_function',
                    'checkpoint_parent': {'ordinal': checkpoint[0], 'name': names[checkpoint[3]],
                        'start_us': raw_time(checkpoint[1]), 'end_us': raw_time(checkpoint[2])},
                    'parent_region_id': backward[str(phase[0][0])]['region_id']}
        verified = defaultdict(list)
        witnesses = data['witnesses']
        for event in data['events'] if host_replays else ():
            if event['kind'] != 'compute': continue
            proof = data['associations'].get(event['event_id'])
            if not proof: continue
            parents = [witnesses[str(ordinal)] for ordinal in proof['python_ordinals']]
            phases = [backward[str(n['ordinal'])] for n in parents if str(n['ordinal']) in backward]
            matches = [host_replays[str(n['ordinal'])] for n in parents if str(n['ordinal']) in host_replays]
            if len(phases) == 1 and len(matches) == 1 and matches[0]['parent_region_id'] == phases[0]['region_id']:
                verified[str(matches[0]['ordinal'])].append(event)
        region_map = {r['region_id']: r for r in regions}
        layer_numbers = Counter()
        for ordinal, host in sorted(host_replays.items(), key=lambda item: (item[1]['parent_region_id'], D(item[1]['start_us']))):
            region = region_map[host['parent_region_id']]
            events = verified.get(ordinal, [])
            start = min(D(e['start_us']) for e in events) if events else D(host['start_us'])
            end = max(D(e['end_us']) for e in events) if events else D(host['end_us'])
            left, right = max(start, D(region['display_start_us'])), min(end, D(region['display_end_us']))
            layer_numbers[region['region_id']] += 1
            recompute.append({'region_id': f'recompute:{region["region_id"]}:{ordinal}',
                'parent_region_id': region['region_id'], 'rank': rank, 'stage': region['stage'],
                'step': region['step'], 'micro': region['micro'], 'phase': 'r', 'label': str(region['micro'])+'r',
                'layer_replay_index': layer_numbers[region['region_id']],
                'start_us': str(start), 'end_us': str(end), 'duration_us': str(end-start),
                'display_start_us': str(left), 'display_end_us': str(right), 'displayable': right>left,
                'kernel_count': len(events), 'kernel_duration_us': str(sum((D(e['duration_us']) for e in events), D(0))),
                'evidence_level': 'device_flow' if events else 'framework_nested',
                'framework_replay_count': 1, 'framework_replays': [host],
                'event_ids': [e['event_id'] for e in events], 'source_python_events': [host],
                'basis': ('同一checkpoint层重执行经因果flow关联的设备计算包络' if events else
                          '同一线程backward_step → checkpoint backward → custom_forward包含关系；缺少设备关联，保留框架时间')+
                         '；按层独立分段并与父b显示区间取交集，不跨层填平间隔'})
        sources.append({'rank': rank, 'trace': source['trace'], 'fingerprint': source['fingerprint'],
                        'raw_index_version': raw['version'], 'association_version': derived['version']})
    parents = {r['parent_region_id'] for r in recompute}
    proven = {r['parent_region_id'] for r in recompute if r['kernel_count']}
    by_rank = Counter()
    for rank, parent in {(r['rank'], r['parent_region_id']) for r in recompute}: by_rank[str(rank)]+=1
    result = {'schema_version': 2, 'time_origin_us': pp_report['time_origin_us'],
              'optimizer_regions': markers, 'recompute_regions': recompute, 'sources': sources,
              'stats': {'optimizer_count': len(markers), 'optimizer_adam_kernels': sum(r['kernel_count'] for r in markers),
                        'recompute_micro_count': len(parents), 'recompute_by_rank': dict(by_rank),
                        'recompute_segment_count': len(recompute),
                        'displayable_recompute_segments': sum(r['displayable'] for r in recompute),
                        'framework_only_micro_count': len(parents-proven),
                        'framework_only_segment_count': sum(not r['kernel_count'] for r in recompute),
                        'proven_recompute_kernels': sum(r['kernel_count'] for r in recompute)},
              'notes': ['opt为实际Adam设备算子的首尾包络，不使用CPU optimizer.step定位。',
                        'r按checkpoint层重执行独立分段；缺少flow的段标明框架证据，未确认部分不按配置补画。']}
    annotate_layer_numbers(result,pp_report)
    (output_dir/'phase_markers.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return result


def refresh_annotations(output_dir):
    """Rebuild only annotations/colors from existing, validated parse products."""
    from workflow_support import load_json, write_json
    from timeline_colors import color_config
    out=Path(output_dir)
    pp,streams=load_json(out/'report.json'),load_json(out/'streams/report.json')
    report=load_json(out/'unified_report.json')
    if report['time_origin_us']!=pp['time_origin_us'] or report['profiling_path']!=pp['profiling_path']:
        raise ValueError('Unified report and parsed streams belong to different inputs')
    markers=collect_phase_markers(pp,streams,out)
    report.update(optimizer_regions=markers['optimizer_regions'],recompute_regions=markers['recompute_regions'],
                  phase_marker_stats=markers['stats'],color_config=color_config())
    report['stats']['phase_markers']=markers['stats']
    report['notes']=[n for n in report.get('notes',[]) if not n.startswith(('opt为','r仅标记','r按checkpoint'))]+markers['notes']
    write_json(out/'unified_report.json',report)
    summary=load_json(out/'unified_summary.json');summary.update(stats=report['stats'],notes=report['notes'])
    write_json(out/'unified_summary.json',summary)
    return markers['stats']


if __name__=='__main__':
    import argparse
    from workflow_support import lock_file
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args()
    with lock_file(args.output_dir/'.workflow.lock'):
        print(json.dumps(refresh_annotations(args.output_dir),ensure_ascii=False))
