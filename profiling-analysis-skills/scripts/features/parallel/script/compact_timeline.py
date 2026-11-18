"""Lossless column transport for offline timelines; canonical JSON is unchanged.

Keep exact decimal strings (including trailing zeros) and missing/null separately.
Binary columns are little endian; no timestamp is rounded to a JS Number here.
"""
from array import array
import base64
import json
import re
import sys

MISSING = object()
DECIMAL = re.compile(r'^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$')
SAFE = 2**53 - 1


def numbers(values):
    if all(type(v) is int for v in values):
        lo, hi = min(values, default=0), max(values, default=0)
        kind = 'B' if lo >= 0 and hi <= 255 else 'H' if lo >= 0 and hi <= 65535 else 'I' if lo >= 0 and hi <= 4294967295 else 'i' if -2147483648 <= lo and hi <= 2147483647 else 'd'
    else:
        kind = 'd'
    result = array(kind, values)
    if sys.byteorder != 'little':
        result.byteswap()
    return {'type': kind, 'data': base64.b64encode(result.tobytes()).decode('ascii')}


def read_numbers(value):
    result = array(value['type'])
    result.frombytes(base64.b64decode(value['data'], validate=True))
    if sys.byteorder != 'little':
        result.byteswap()
    return result


def signature(value):
    # Primitive values dominate the million-event timeline. Avoid JSON-encoding
    # each string twice just to test equality/deduplicate it. Type tags preserve
    # bool/int/float distinctions and the float spelling preserves negative zero.
    if type(value) in (str, int, bool, type(None)):
        return (type(value).__name__, value)
    if type(value) is float:
        return ('float', json.dumps(value, allow_nan=False))
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, default=str)


def _decimal(values):
    if not all(isinstance(v, str) and len(v) <= 40 and DECIMAL.fullmatch(v) for v in values):
        return None
    digits = [len(v.partition('.')[2]) for v in values]
    scale = max(digits, default=0)
    if scale > 9:
        return None
    integers = []
    for v, d in zip(values, digits):
        integer = int(v.replace('.', '')) * 10**(scale-d)
        if integer == 0 and v.startswith('-'):
            return None
        integers.append(integer)
    base = min(integers)
    deltas = [v-base for v in integers]
    if max(deltas, default=0) > SAFE:
        return None
    return {'t': 'decimal', 'base': str(base), 'scale': scale,
            'safe': max(abs(base), abs(max(integers))) <= SAFE,
            'a': numbers(deltas), 'digits': encode_column(digits)}


def encode_column(values, name=''):
    present = [v is not MISSING for v in values]
    nonmissing = [v for v in values if v is not MISSING]
    # Values under an absent bit are never read. A real null stays a real null.
    if not nonmissing:
        return {'t': 'absent'}
    filled = [v if ok else nonmissing[0] for v, ok in zip(values, present)]
    first = signature(nonmissing[0])
    if all(signature(v) == first for v in nonmissing):
        result = {'t': 'constant', 'v': nonmissing[0]}
    elif name.endswith('_us') and (decimal := _decimal(filled)) is not None:
        result = decimal
    elif all(type(v) in (int, float) and (type(v) is float or abs(v) <= SAFE) for v in filled):
        result = {'t': 'number', 'a': numbers(filled)}
    elif all(isinstance(v, dict) for v in filled):
        result = {'t': 'object', 'columns': encode_columns(filled)}
    elif name in ('event_id', 'association_id') and all(isinstance(v, str) and ':' in v and v.rsplit(':', 1)[1].isdigit() and str(int(v.rsplit(':', 1)[1])) == v.rsplit(':', 1)[1] and int(v.rsplit(':', 1)[1]) <= SAFE for v in filled):
        result = {'t': 'id', 'prefix': encode_column([v.rsplit(':', 1)[0] for v in filled]),
                  'a': numbers([int(v.rsplit(':', 1)[1]) for v in filled])}
    else:
        unique, ids, lookup = [], [], {}
        for v in filled:
            key = signature(v)
            index = lookup.get(key)
            if index is None:
                index = len(unique); lookup[key] = index; unique.append(v)
            ids.append(index)
        result = {'t': 'dictionary', 'values': unique, 'a': numbers(ids)}
    if not all(present):
        result['present'] = numbers([int(v) for v in present])
    return result


def encode_columns(rows):
    keys = dict.fromkeys(k for row in rows for k in row)
    return {key: encode_column([row.get(key, MISSING) for row in rows], key) for key in keys}


def decode_column(column, count):
    kind = column['t']
    if kind == 'absent':
        return [MISSING] * count
    if kind == 'constant':
        values = [column['v']] * count
    elif kind == 'number':
        values = list(read_numbers(column['a']))
    elif kind == 'dictionary':
        values = [column['values'][i] for i in read_numbers(column['a'])]
    elif kind == 'object':
        values = decode_columns(column['columns'], count)
    elif kind == 'id':
        values = [prefix+':'+str(int(n)) for prefix, n in zip(decode_column(column['prefix'], count), read_numbers(column['a']))]
    elif kind == 'decimal':
        base, scale = int(column['base']), column['scale']
        values = []
        for delta, digits in zip(read_numbers(column['a']), decode_column(column['digits'], count)):
            n = base + int(delta)
            sign = '-' if n < 0 else ''
            raw = str(abs(n)).zfill(scale + 1)
            values.append(sign + (raw[:-scale] if scale else raw) + ('.'+raw[-scale:][:digits] if digits else ''))
    else:
        raise ValueError('Unknown column type: '+kind)
    if 'present' in column:
        values = [v if ok else MISSING for v, ok in zip(values, read_numbers(column['present']))]
    return values


def decode_columns(columns, count):
    rows = [{} for _ in range(count)]
    for key, column in columns.items():
        for row, value in zip(rows, decode_column(column, count)):
            if value is not MISSING:
                row[key] = value
    return rows
