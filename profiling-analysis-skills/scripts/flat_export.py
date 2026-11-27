"""Adapt a flat one-device TEXT export for the existing rank-directory readers."""
import csv
import json
import os
from pathlib import Path
import re
import shutil


EXPORT_FILES = (
    'trace_view.json', 'step_trace_time.csv', 'kernel_details.csv',
    'operator_details.csv', 'op_statistic.csv', 'api_statistic.csv',
    'communication.json', 'communication_matrix.json',
)
RANK_LOG = re.compile(r'^\[RANK (\d+)/\d+\].*Initializing parallel state')


def identify_export(requested):
    root = requested.parent if requested.name == 'ASCEND_PROFILER_OUTPUT' else requested
    export = root / 'ASCEND_PROFILER_OUTPUT'
    if all((export / name).is_file() for name in ('trace_view.json', 'step_trace_time.csv')):
        return root
    return None


def prepare_export(root, out, log_dir):
    export = root / 'ASCEND_PROFILER_OUTPUT'
    with (export / 'step_trace_time.csv').open(encoding='utf-8-sig', newline='') as stream:
        devices = {(row.get('Device_id') or '').strip() for row in csv.DictReader(stream)}
    if len(devices) != 1 or not next(iter(devices)).isdigit():
        raise ValueError('平铺 TEXT 导出需要明确的单一 Device_id；多卡采集请提供逐卡目录。')
    device = int(next(iter(devices)))
    logged_ranks = set()
    if log_dir:
        for log in Path(log_dir).glob('*.log'):
            with log.open(encoding='utf-8', errors='replace') as stream:
                for text in stream:
                    if match := RANK_LOG.match(text):
                        logged_ranks.add(int(match[1]))
    infos = sorted(root.glob('profiler_info*.json'))
    if len(infos) > 1:
        raise ValueError('平铺导出含多个 profiler_info，无法确认采集卡。')
    if infos:
        info = json.loads(infos[0].read_text(encoding='utf-8-sig'))
        rank = info.get('ms_profiler_info', {}).get('rank_id', info.get('rank_id'))
        if rank is None and (match := re.fullmatch(r'profiler_info_(\d+)', infos[0].stem)):
            rank = int(match[1])
        if not str(rank).isdigit():
            raise ValueError('profiler_info 中缺少有效 rank_id。')
        rank, basis = int(rank), 'profiler_info'
    elif logged_ranks == {device}:
        rank, basis = device, '训练初始化日志与 Device_id 一致'
    else:
        rank, basis = device, '仅作本地 Device_id 使用，全局 rank 未确认'
    staging = out / '.input' / 'profiling'
    folder = staging / f'rank{rank}_ascend_pt'
    destination = folder / 'ASCEND_PROFILER_OUTPUT'
    state_path = out / '.input' / 'flat_export.json'
    previous = json.loads(state_path.read_text(encoding='utf-8')) if state_path.is_file() else {}
    sources = {name: export / name for name in EXPORT_FILES if (export / name).is_file()}
    for name in ('profiler_metadata.json', 'communication_group.json'):
        if (root / name).is_file():
            sources[name] = root / name
    if infos:
        sources['profiler_info.json'] = infos[0]
    sources_state = {name: [str(file), file.stat().st_size, file.stat().st_mtime_ns]
                     for name, file in sources.items()}
    if previous and (previous['source'] != str(root) or previous['rank'] != rank):
        raise ValueError('缓存输入或卡号已变化，请为本次采集使用独立输出目录。')
    if previous and set(previous['files']) - set(sources):
        raise ValueError('原始采集文件已减少，请为当前数据使用独立输出目录。')
    destination.mkdir(parents=True, exist_ok=True)
    for name,file in sources.items():
        if name in EXPORT_FILES:
            target=destination/name
        elif name=='communication_group.json':
            target=staging/name
        else:
            target=folder/name
        if target.is_file() and previous.get('files',{}).get(name)==sources_state[name]:
            continue
        temporary=target.with_name('.'+target.name+f'.{os.getpid()}.tmp')
        try:
            shutil.copy2(file,temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    for name, value in [('profiler_info.json', {'rank_id': rank}), ('profiler_metadata.json', {})]:
        path=folder/name
        if not path.exists():
            path.write_text(json.dumps(value),encoding='utf-8')
    state={'source':str(root),'analysis_input':str(staging),'rank':rank,'rank_basis':basis,'files':sources_state}
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    return staging, state
