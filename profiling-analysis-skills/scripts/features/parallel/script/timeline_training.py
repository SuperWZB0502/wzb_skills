"""Small, independent log/metadata guard for sequential micro numbering."""
from pathlib import Path
import re
import sys

sys.path.append(str(Path(__file__).resolve().parents[3]))
from training_log import mesh_training_arguments

ARGUMENT = re.compile(r'^\s{2}(\w+)\s+\.{2,}\s+(.*?)\s*$')


def micro_configuration(profiling, ranks, log_dir=None):
    values = {}; sources = []; warnings = []
    wanted = ('global_batch_size', 'micro_batch_size', 'data_parallel_size', 'rampup_batch_size')
    for rank in sorted({0, *(int(row['rank']) for row in ranks)}):
        path = (Path(log_dir) if log_dir is not None else Path(profiling).parent / 'run_log') / f'worker_{rank}.log'
        if not path.is_file(): continue
        for line_number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
            match = ARGUMENT.match(line)
            if match and match[1] in wanted:
                key, raw = match.groups()
                values.setdefault(key, set()).add(raw)
                sources.append({'file': str(path), 'line': line_number, 'key': key, 'value': raw})
    # Metadata also supplies the DP dimension; mismatches disable inference.
    for row in ranks:
        value = row['metadata'].get('distributed_args', {}).get('data_parallel_size')
        if value is not None: values.setdefault('data_parallel_size', set()).add(str(value))
    mesh_args=mesh_training_arguments(Path(log_dir) if log_dir else Path(profiling).parent/'run_log')
    for key,records in mesh_args.items():
        if key in wanted:values.setdefault(key,set()).update(str(value) for value,_ in records)
        sources.extend(source for _,source in records)
    result = {'expected_mbn': None, 'sources': sources, 'warnings': warnings,
              'mesh_args':{key:sorted({value for value,_ in records}) for key,records in mesh_args.items()}}
    if any(len(v) != 1 for v in values.values()):
        warnings.append('微批次数配置来源不一致，未使用配置推断micro编号。')
        return result
    if values.get('rampup_batch_size', {'None'}) != {'None'}:
        warnings.append('启用了batch size rampup，未将静态GBS作为当前Step的micro数。')
        return result
    parsed = {}
    for key in wanted[:3]:
        records = values.get(key)
        if not records: return result
        raw = next(iter(records))
        if not raw.isdigit() or int(raw) <= 0: return result
        parsed[key] = int(raw)
    divisor = parsed['micro_batch_size'] * parsed['data_parallel_size']
    if parsed['global_batch_size'] % divisor:
        warnings.append('GBS/(DP×MBS)不是整数，未使用配置推断micro编号。')
        return result
    result.update(expected_mbn=parsed['global_batch_size'] // divisor,
                  GBS=parsed['global_batch_size'], MBS=parsed['micro_batch_size'],
                  DP=parsed['data_parallel_size'], formula='GBS/(DP*MBS)')
    return result
