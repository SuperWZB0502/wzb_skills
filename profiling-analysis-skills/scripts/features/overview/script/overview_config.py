#!/usr/bin/env python3
"""Extract training configuration from small profiling metadata and sibling logs.

No traces, training execution, directory-name configuration guesses, or Feature2
dependencies. Memory is a cumulative framework allocator high watermark near
collection end, not an independently measured peak within the selected step.
"""
from __future__ import annotations

import argparse
import ast
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re


SUMMARY_KEYS = {
    'profiling_ranks': 'world_size', 'GBS': 'global_batch_size',
    'TP': 'tensor_model_parallel_size', 'CP': 'context_parallel_size',
    'EP': 'expert_model_parallel_size', 'DP': 'data_parallel_size',
    'PP': 'pipeline_model_parallel_size', 'MBS': 'micro_batch_size',
}
ARGUMENT = re.compile(r'^\s{2}(\w+)\s+\.{2,}\s+(.*?)\s*$')
MEMORY = re.compile(
    r'\[Rank (\d+)\] \(after (\d+) iterations\) memory \(MB\)'
    r' \| allocated: ([0-9.]+) \| max allocated: ([0-9.]+)'
    r' \| reserved: ([0-9.]+) \| max reserved: ([0-9.]+)')


def _value(raw):
    try:
        return ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return raw


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _vector(value, pp):
    return (isinstance(value, list) and len(value) == pp and
            all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in value))


def collect_config(profiling: Path, rank_folders: dict[int, Path], log_dir=None):
    """Return nullable configuration fields plus explicit provenance and limits.

    ``profiling_ranks`` means configured world_size; the caller separately counts
    actually collected ranks. Keys of per_rank are global integer ranks.
    """
    profiling = Path(profiling)
    log_root = Path(log_dir) if log_dir is not None else profiling.parent / 'run_log'
    result = {'summary': {key: None for key in (*SUMMARY_KEYS, 'MBN')},
              'per_rank': {}, 'notes': [], 'sources': [], 'warnings': []}
    sources, warnings = result['sources'], result['warnings']
    metadata, infos = {}, {}

    def read_json(path):
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            if path.exists():
                warnings.append(f'无法读取 {path}：{error}')
            return {}

    for rank, folder in sorted(rank_folders.items()):
        folder = Path(folder)
        path = folder / 'profiler_metadata.json'
        data = read_json(path)
        stated_rank = data.get('distributed_args', {}).get('rank')
        if stated_rank is not None and stated_rank != rank:
            warnings.append(f'卡号 {rank} 与 {path} distributed_args.rank={stated_rank} 不一致，忽略该元数据')
            data = {}
        metadata[rank] = data
        candidates = sorted(folder.glob('profiler_info*.json'))
        infos[rank] = read_json(candidates[0]) if len(candidates) == 1 else {}

    # Read only worker_0 plus explicitly requested global-rank logs, never all
    # worker logs and never profiler traces. Logs use one-based line provenance.
    log_paths = sorted({log_root / f'worker_{r}.log'
                        for r in {*rank_folders, 0}})
    logs, arguments = {}, {}
    for path in log_paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').split('\n')
        except OSError as error:
            warnings.append(f'无法读取 {path}：{error}')
            continue
        logs[path] = lines
        for line_number, line in enumerate(lines, 1):
            match = ARGUMENT.match(line)
            if match:
                key, raw = match.groups()
                arguments.setdefault(key, []).append((
                    _value(raw), {'file': str(path), 'line': line_number,
                                  'type': 'run_log_argument', 'key': key}))

    def argument(key):
        records = arguments.get(key, [])
        if not records:
            return None
        sources.extend(source for _, source in records)
        values = [value for value, _ in records]
        if any(value != values[0] for value in values[1:]):
            warnings.append(f'训练日志 {key} 不一致，保留为空')
            return None
        return values[0]

    for label, key in SUMMARY_KEYS.items():
        records = [(rank, data.get('distributed_args', {}).get(key))
                   for rank, data in metadata.items()
                   if data.get('distributed_args', {}).get(key) is not None]
        log_value = argument(key)
        if records:
            values = [value for _, value in records]
            for rank, value in records:
                sources.append({'file': str(Path(rank_folders[rank]) / 'profiler_metadata.json'),
                                'line': None, 'type': 'profiler_metadata', 'rank': rank,
                                'key': f'distributed_args.{key}', 'value': value})
            if any(value != values[0] for value in values[1:]):
                warnings.append(f'各卡元数据 {label} 不一致，保留为空')
                continue
            chosen = values[0]
            if log_value is not None and log_value != chosen:
                warnings.append(f'{label} 元数据与训练日志不一致，采用元数据 {chosen}')
        else:
            chosen = log_value
        if chosen is not None and not _positive(chosen):
            warnings.append(f'{label} 不是有效正整数，保留为空')
        else:
            result['summary'][label] = chosen

    summary = result['summary']
    if all(summary[k] is not None for k in ('GBS', 'DP', 'MBS')):
        divisor = summary['DP'] * summary['MBS']
        if summary['GBS'] % divisor == 0:
            summary['MBN'] = summary['GBS'] // divisor
            sources.append({'type': 'derived', 'file': None, 'line': None, 'key': 'MBN',
                            'formula': 'GBS / (DP * MBS)'})
        else:
            warnings.append('GBS / (DP × MBS) 不是整数，MBN 保留为空')

    pp, world = summary['PP'], summary['profiling_ranks']
    layers = argument('num_layer_list')
    recompute = argument('recompute_num_layers')
    granularity = argument('recompute_granularity')
    method = argument('recompute_method')
    virtual = argument('virtual_pipeline_model_parallel_size')
    valid_layers = _vector(layers, pp) and virtual is None
    valid_recompute = (_vector(recompute, pp) and virtual is None and
                       granularity == 'full' and method == 'block')
    if recompute is not None and not valid_recompute:
        result['notes'].append('重计算层数仅在 full/block 且每 Stage 层数列表明确时展示；uniform 的分块大小不当作层数。')

    for rank, folder in sorted(rank_folders.items()):
        row = {'stage': None, '层数': None, '重计算': None, '显存': None,
               '显存_reserved': None, 'memory_iteration': None, 'pp_group': None}
        result['per_rank'][rank] = row
        groups = metadata[rank].get('parallel_group_info', {})
        candidates = []
        for group_id, group in groups.items():
            members = group.get('global_ranks', [])
            if (pp is None or world is None or world % pp or not isinstance(members, list)
                    or len(members) != pp or rank not in members
                    or any(not isinstance(member, int) or isinstance(member, bool) for member in members)):
                continue
            # Existing exported groups carry no PP type label. Identify a PP
            # candidate by PP size plus world/PP rank stride, as Feature2 does;
            # retain this evidence instead of trusting group_rank (all zeros).
            if (len(set(members)) != pp or any(member < 0 or member >= world for member in members)
                    or any(b - a != world // pp for a, b in zip(members, members[1:]))):
                continue
            candidates.append((group_id, members, members.index(rank)))
        stages = {candidate[2] for candidate in candidates}
        if len(stages) == 1:
            group_id, members, stage = candidates[0]
            row['stage'] = stage
            row['pp_group'] = {'id': group_id, 'members': members,
                               'evidence': 'PP大小 + world_size/PP步长 + global_ranks中的位置'}
            sources.append({'file': str(Path(folder) / 'profiler_metadata.json'),
                            'line': None, 'type': 'parallel_group', 'rank': rank,
                            'key': f'parallel_group_info.{group_id}.global_ranks',
                            'value': members, 'stage': stage})
            if valid_layers:
                row['层数'] = layers[stage]
            if valid_recompute:
                if row['层数'] is not None and recompute[stage] > row['层数']:
                    warnings.append(f'卡号 {rank} 配置重计算层数超过本 Stage 层数，重计算保留为空')
                else:
                    row['重计算'] = recompute[stage]
        elif pp == 1:
            row['stage'] = 0
            if valid_layers:
                row['层数'] = layers[0]
            if valid_recompute:
                row['重计算'] = recompute[0]
        else:
            warnings.append(f'卡号 {rank} 无法从元数据唯一确定 PP Stage，相关层数保留为空')

        path = log_root / f'worker_{rank}.log'
        lines = logs.get(path, [])
        parse_line = next((i for i, line in enumerate(lines, 1)
                           if 'Start parsing profiling data' in line), None)
        memory_records = []
        for number, line in enumerate(lines, 1):
            match = MEMORY.search(line)
            if match and int(match[1]) == rank:
                try:
                    allocated, reserved = Decimal(match[4]), Decimal(match[6])
                except InvalidOperation:
                    continue
                if allocated.is_finite() and reserved.is_finite():
                    memory_records.append((number, int(match[2]), allocated, reserved))
        chosen = None
        if parse_line is not None:
            prior = [record for record in memory_records if record[0] < parse_line]
            chosen = prior[-1] if prior else None
        else:
            schedule = infos[rank].get('profiler_parameters', {}).get('schedule', {})
            schedule_keys = ('skip_first', 'wait', 'warmup', 'active', 'repeat')
            if (all(isinstance(schedule.get(k), int) and schedule[k] >= 0 for k in schedule_keys)
                    and schedule['repeat'] > 0):
                end = schedule['skip_first'] + schedule['repeat'] * (
                    schedule['wait'] + schedule['warmup'] + schedule['active'])
                matching = [record for record in memory_records if record[1] == end]
                chosen = matching[-1] if matching else None
        if chosen:
            number, iteration, allocated, reserved = chosen
            row['显存'] = format(allocated / 1024, 'f')
            row['显存_reserved'] = format(reserved / 1024, 'f')
            row['memory_iteration'] = iteration
            sources.append({'file': str(path), 'line': number, 'type': 'run_log_memory',
                            'rank': rank, 'iteration': iteration,
                            'key': 'max allocated / max reserved', 'source_unit': 'MiB',
                            'output_unit': 'GiB', 'max_allocated_mib': str(allocated),
                            'max_reserved_mib': str(reserved), 'scope': 'cumulative',
                            'capture_end_evidence': 'before profiling parsing' if parse_line else 'profiler schedule'})
        else:
            warnings.append(f'卡号 {rank} 缺少采集结束附近可关联的显存峰值日志，显存保留为空')

    result['notes'].extend([
        '配置卡数来自 world_size；实际采集卡数另按已发现的全局 rank 统计。EP 不作为 world_size 乘法项重复计算。',
        'MBN = GBS / (DP × MBS)，由训练参数计算；未知项不按目录名称推测。',
        '层数与重计算为训练配置，按元数据 PP 组的 Stage 映射；不将 worker 目录序号作为卡号。',
        '显存为采集结束附近日志的 max allocated 累计峰值（GiB）；日志 MB 实为 MiB，除以 1024。'
        '该值不是所选 Step 独立峰值，也不包括框架分配器之外的全部 NPU 占用；max reserved 单独保留。',
        '显存日志不随页面 Step 切换重算；若需独立 Step 峰值，需显存时间线或每 Step 重置峰值后重新采集。',
    ])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiling', required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    # Shared Feature1 rank discovery validates profiler_info global rank IDs.
    from cluster_step_time import discover_ranks, rank_id
    folders = {}
    for folder in discover_ranks(args.profiling):
        rank, _ = rank_id(folder)
        if rank in folders:
            raise ValueError(f'重复全局卡号 {rank}')
        folders[rank] = folder
    payload = json.dumps(collect_config(args.profiling, folders), ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding='utf-8')
        print(args.output)
    else:
        print(payload, end='')


if __name__ == '__main__':
    main()
