"""Normalize exported Ascend kernel data and aggregate operator types."""
from bisect import bisect_right
from collections import defaultdict
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import re
import sqlite3
from contextlib import closing


COMPUTE = 'compute'
COMMUNICATION = 'communication'
COMMUNICATION_CORES = {'HCCL', 'COMMUNICATION'}
PMU_TIME_COLUMNS = {'aicore_time', 'aiv_time', 'aic_mac_time', 'aic_scalar_time',
                    'aic_mte1_time', 'aic_mte2_time', 'aic_fixpipe_time',
                    'aiv_vec_time', 'aiv_scalar_time', 'aiv_mte2_time', 'aiv_mte3_time'}
DETAIL_FIELDS = ('name', 'type', 'core', 'start_ms', 'duration', 'wait', 'block',
                 'input_shapes', 'input_types', 'input_formats', 'output_shapes',
                 'output_types', 'output_formats')
TEXT_FIELDS = dict(zip(DETAIL_FIELDS, (
    'Name', 'Type', 'Accelerator Core', 'Start Time(us)', 'Duration(us)', 'Wait Time(us)',
    'Block Num', 'Input Shapes', 'Input Data Types', 'Input Formats', 'Output Shapes',
    'Output Data Types', 'Output Formats',
)))
DB_FIELDS = dict(zip(DETAIL_FIELDS[7:], (
    'inputShapes', 'inputDataTypes', 'inputFormats', 'outputShapes', 'outputDataTypes', 'outputFormats',
)))


def discover(value):
    requested = Path(value).expanduser().resolve()
    if requested.is_file():
        if requested.suffix.lower() != '.db' and not requested.name.startswith(('kernel_details', 'op_summary')):
            raise ValueError('算子详情需要 kernel_details.csv、op_summary CSV 或 Ascend Profiling DB')
        return requested, [requested]
    if not requested.is_dir():
        raise ValueError(f'输入目录不存在：{requested}')
    root = requested / 'profiling' if (requested / 'profiling').is_dir() else requested
    root = root.parent if root.name == 'ASCEND_PROFILER_OUTPUT' else root
    sources = []
    for folder in sorted({root, *root.rglob('ASCEND_PROFILER_OUTPUT')}):
        text = sorted({*folder.glob('kernel_details*.csv'), *folder.glob('op_summary*.csv')})
        if text:
            sources.extend(path for path in text if path.name.startswith('kernel_details') or
                           not any(p.name.startswith('kernel_details') for p in text))
        else:
            sources.extend(sorted({*folder.glob('ascend_pytorch_profiler*.db'), *folder.glob('msprof*.db'),
                                   *folder.glob('ascend_mindspore_profiler*.db')}))
    if not sources:
        raise ValueError('未找到算子明细 CSV 或 Ascend Profiling DB')
    return root, sources


def decimal(value):
    try:
        number = Decimal(str(value).strip())
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def display_text(value):
    if value is None or str(value).strip() in ('', 'N/A', 'NA'):
        return None
    return str(value).strip()


def format_number(number, places):
    if number is None:
        return None
    return str(number.quantize(Decimal((0, (1,), -places)), rounding=ROUND_HALF_UP))


def read_text(path):
    csv.field_size_limit(16 * 1024 * 1024)
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        headers = reader.fieldnames or []
        aliases = {
            'name': ('Name', 'OP Name'), 'type': ('Type', 'OP Type'),
            'core': ('Accelerator Core', 'Task Type'),
            'start_ms': ('Start Time(us)', 'Task Start Time(us)', 'Task Start Time'),
            'duration': ('Duration(us)', 'Task Duration(us)', 'Task Duration'),
            'wait': ('Wait Time(us)', 'Task Wait Time(us)', 'Task Wait Time'),
            'block': ('Block Num', 'Block Dim', 'Block Dimension'),
        }
        columns = {key: next((name for name in aliases.get(key, (default,)) if name in headers), None)
                   for key, default in TEXT_FIELDS.items()}
        if any(columns[key] is None for key in ('name', 'type', 'core', 'duration')):
            raise ValueError(f'算子 CSV 缺少名称、类型、加速器核或时长字段：{path.name}')
        pmu_columns = sorted(column for column in headers if column.lower().startswith(
            ('aic_', 'aiv_', 'aicore_', 'cube_utilization')))
        for entry in reader:
            row = {key: display_text(entry.get(column)) for key, column in columns.items()}
            row['start_ms'] = decimal(row['start_ms'])
            row['duration'] = decimal(row['duration'])
            row['wait'] = decimal(row['wait'])
            row['step'] = display_text(entry.get('Step Id') or entry.get('Step ID'))
            device = display_text(entry.get('Device_id') or entry.get('Device ID')) or '0'
            row['kind'] = COMMUNICATION if row['core'] in COMMUNICATION_CORES else COMPUTE
            row['pmu'] = {key: value for key in pmu_columns if (value := display_text(entry.get(key))) is not None}
            yield device, row


def table_columns(db, table):
    return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}


def required_columns(db, table, columns):
    return set(columns) <= table_columns(db, table)


def db_decimal(ns):
    return Decimal(ns) / 1000 if ns is not None else None


def read_db(path):
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        if not (required_columns(db, 'TASK', ('globalTaskId', 'startNs', 'endNs', 'deviceId')) and
                required_columns(db, 'STRING_IDS', ('id', 'value'))):
            raise ValueError(f'不支持的 Ascend Profiling DB 结构：{path.name}')
        compute_columns = table_columns(db, 'COMPUTE_TASK_INFO')
        comm_columns = table_columns(db, 'COMMUNICATION_OP')
        task_columns = table_columns(db, 'TASK')
        devices = ({str(row['deviceId']): str(row['rankId']) for row in
                    db.execute('SELECT rankId, deviceId FROM RANK_DEVICE_MAP')}
                   if required_columns(db, 'RANK_DEVICE_MAP', ('rankId', 'deviceId')) else {})
        hosts = ([row[0] for row in db.execute('SELECT DISTINCT hostName FROM HOST_INFO WHERE hostName IS NOT NULL')]
                 if required_columns(db, 'HOST_INFO', ('hostName',)) else [])
        host = display_text(hosts[0]) if len(hosts) == 1 else None
        origin = (db_decimal(db.execute('SELECT MIN(startTimeNs) FROM SESSION_TIME_INFO').fetchone()[0])
                  if required_columns(db, 'SESSION_TIME_INFO', ('startTimeNs',)) else None)
        step_ranges = (list(db.execute('SELECT id, startNs, endNs FROM STEP_TIME ORDER BY startNs'))
                       if required_columns(db, 'STEP_TIME', ('id', 'startNs', 'endNs')) else [])
        starts = [row['startNs'] for row in step_ranges]

        def step_for(ns):
            i = bisect_right(starts, ns) - 1 if ns is not None else -1
            return str(step_ranges[i]['id']) if i >= 0 and ns < step_ranges[i]['endNs'] else None

        pmus = defaultdict(dict)
        if required_columns(db, 'TASK_PMU_INFO', ('globalTaskId', 'name', 'value')):
            for task, name, value in db.execute('SELECT p.globalTaskId, s.value, p.value FROM TASK_PMU_INFO p '
                                                'JOIN STRING_IDS s ON s.id=p.name ORDER BY p.rowid'):
                if name in PMU_TIME_COLUMNS:
                    pmus[task][f'{name}(us)'] = format(db_decimal(value), 'f') if value is not None else None
                elif name is not None:
                    pmus[task][name] = display_text(value)
        if {'globalTaskId', 'name', 'opType', 'taskType'} <= compute_columns:
            string_fields = {'name': 'name', 'type': 'opType', 'core': 'taskType', **DB_FIELDS}
            fields = []
            for alias, column in string_fields.items():
                expression = (f'(SELECT value FROM STRING_IDS WHERE id=c.{column})'
                              if column in compute_columns else 'NULL')
                fields.append(f'{expression} AS {alias}')
            block = next((col for col in ('blockDim', 'blockNum') if col in compute_columns), None)
            fields.extend((f'c.{block} AS block' if block else 'NULL AS block',
                           'c.waitNs AS wait' if 'waitNs' in compute_columns else 'NULL AS wait'))
            sql = ('SELECT t.globalTaskId, t.deviceId, t.startNs, t.endNs, ' + ', '.join(fields) +
                   ' FROM COMPUTE_TASK_INFO c JOIN TASK t ON t.globalTaskId=c.globalTaskId')
            for entry in db.execute(sql):
                start, end = entry['startNs'], entry['endNs']
                row = {key: display_text(entry[key]) for key in (*string_fields, 'block')}
                row.update(start_ms=db_decimal(start), duration=db_decimal(end-start) if None not in (start, end) else None,
                           wait=db_decimal(entry['wait']), step=step_for(start), kind=COMPUTE,
                           pmu=pmus.get(entry['globalTaskId'], {}), host=host, origin=origin)
                yield devices.get(str(entry['deviceId']), str(entry['deviceId'])), row
        if {'opName', 'opType', 'startNs', 'endNs', 'connectionId'} <= comm_columns and 'connectionId' in task_columns:
            wait = 'c.waitNs' if 'waitNs' in comm_columns else 'NULL'
            sql = ('SELECT t.deviceId, n.value, o.value, c.startNs, c.endNs, ' + wait +
                   ' FROM COMMUNICATION_OP c JOIN STRING_IDS n ON n.id=c.opName '
                   'JOIN STRING_IDS o ON o.id=c.opType JOIN (SELECT DISTINCT deviceId,connectionId FROM TASK) t '
                   'ON t.connectionId=c.connectionId')
            for device, name, typ, start, end, wait in db.execute(sql):
                row = dict.fromkeys(DETAIL_FIELDS)
                row.update(name=display_text(name), type=display_text(typ), start_ms=db_decimal(start),
                           duration=db_decimal(end-start) if None not in (start, end) else None, wait=db_decimal(wait),
                           step=step_for(start), kind=COMMUNICATION, pmu={}, host=host, origin=origin)
                yield devices.get(str(device), str(device)), row


def source_label(source, device):
    if source.suffix.lower() != '.db':
        rank_files = list(source.parent.parent.glob('profiler_info_*.json'))
        if len(rank_files) == 1 and (match := re.fullmatch(r'profiler_info_(\d+)', rank_files[0].stem)):
            return match[1]
    return device


def summarize(rows):
    groups = defaultdict(list)
    for index, row in enumerate(rows):
        kind = row['kind']
        groups[(kind, row['type'], row['core'] if kind == COMPUTE else None)].append(index)
    statistics = {COMPUTE: [], COMMUNICATION: []}
    for (kind, typ, core), members in groups.items():
        values = [rows[i]['duration'] for i in members]
        total = sum(values)
        statistics[kind].append({
            'type': typ, 'core': core, 'count': len(members), 'total': format_number(total, 2),
            'avg': format_number(total / len(members), 2),
            'min': format_number(min(values), 2), 'max': format_number(max(values), 2),
            'members': members,
        })
    return statistics


def collect(sources, step=None):
    cards = []
    issues = []
    by_source_device = defaultdict(list)
    for source in sources:
        reader = read_db if source.suffix.lower() == '.db' else read_text
        missing_duration = 0
        count = 0
        try:
            for device, row in reader(source):
                count += 1
                if row['duration'] is None or row['duration'] < 0:
                    missing_duration += 1
                    continue
                by_source_device[source, device].append(row)
        except (OSError, sqlite3.Error, ValueError, csv.Error) as exc:
            raise ValueError(f'{source.name} 解析失败：{exc}') from exc
        if missing_duration:
            issues.append(f'{source.name} 有 {missing_duration} 条记录缺少有效时长')
        if not count:
            issues.append(f'{source.name} 中未发现算子记录')
    # Multiple collection files can share a device number. Preserve every card independently.
    for (source, device), original in by_source_device.items():
        origin = original[0].get('origin') or min((row['start_ms'] for row in original if row['start_ms']), default=None)
        host = original[0].get('host')
        rows = [row for row in original if step is None or row['step'] == step]
        metrics = sorted({metric for row in rows for metric in row['pmu']})
        statistics = summarize(rows)
        for row in rows:
            start = row['start_ms']
            row['start_ms'] = format_number((start-origin) / 1000, 2) if start and origin is not None else None
            row['duration'] = format_number(row['duration'], 3)
            row['wait'] = format_number(row['wait'], 3)
            row.pop('origin', None)
            row.pop('host', None)
        cards.append({'rank': source_label(source, device), 'source': source.parent.parent.name if
                      source.parent.name == 'ASCEND_PROFILER_OUTPUT' else source.parent.name,
                      'rows': rows, 'statistics': statistics, 'metrics': metrics, 'host': host,
                      'format': 'db' if source.suffix.lower() == '.db' else 'text'})
    cards.sort(key=lambda card: (int(card['rank']) if card['rank'].isdigit() else float('inf'),
                                card['rank'], card['source']))
    if not cards:
        raise ValueError('输入中没有可统计的算子记录')
    if step is not None and not any(card['rows'] for card in cards):
        issues.append(f'未找到 Step {step} 的算子记录')
    return cards, issues
