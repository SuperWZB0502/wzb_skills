#!/usr/bin/env python3
"""Audit missing MindSpore DistComm flow ids without assuming other versions.

The supported 2.10.0 schema is independently archived with feature 2 sources.
Only known fixed headers, name fields and validated record boundaries are read.
An unsupported/partial schema never produces a confident invalid-flow diagnosis.
"""
from collections import Counter, defaultdict
import json
import mmap
from pathlib import Path
import struct

HEADER = struct.Struct('<HI')
FIXED = struct.Struct('<5Qi3Hb3?')
INVALID_FLOW = (1 << 64) - 1
VERSION = 1


def _signature(paths):
    return [{'path': str(p.resolve()), 'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} for p in paths]


def diagnose(folder, expected_names, cache_path=None):
    """Confirm classes only if TLV/trace class counts agree and all ids invalid."""
    folder = Path(folder); path = folder / 'FRAMEWORK/mindspore.op_range'
    infos = sorted(folder.glob('profiler_info*.json'))
    unavailable = {'confirmed': False, 'reason': 'No supported complete TLV evidence', 'verified_names': {}, 'file': str(path)}
    if not path.is_file() or len(infos) != 1: return unavailable
    signature = _signature([path, infos[0]])
    expected_names = dict(expected_names)
    if cache_path and Path(cache_path).is_file():
        try:
            cached = json.loads(Path(cache_path).read_text(encoding='utf-8'))
            if cached.get('version') == VERSION and cached.get('fingerprint') == signature and cached.get('expected_names') == expected_names:
                return cached['result']
        except (OSError, ValueError): pass
    info = json.loads(infos[0].read_text(encoding='utf-8'))
    if info.get('ms_version') != '2.10.0':
        return dict(unavailable, reason='Unsupported MindSpore TLV schema version', ms_version=info.get('ms_version'))
    counts = defaultdict(Counter); offsets = {}; record_count = 0
    malformed = None
    if path.stat().st_size == 0: return dict(unavailable, reason='Empty TLV file')
    with path.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        offset = 0; length = len(data)
        while offset < length:
            if length - offset < HEADER.size:
                malformed = 'Truncated record header'; break
            record_type, value_length = HEADER.unpack_from(data, offset)
            begin, end = offset + HEADER.size, offset + HEADER.size + value_length
            if end > length or value_length < FIXED.size:
                malformed = 'Invalid or truncated fixed record body'; break
            fixed = FIXED.unpack_from(data, begin)
            record_count += 1
            if fixed[6] == 3 and fixed[7] == 32:
                position = begin + FIXED.size; name = None
                while position < end:
                    if end - position < HEADER.size:
                        malformed = 'Truncated nested TLV header'; break
                    field_type, field_length = HEADER.unpack_from(data, position)
                    position += HEADER.size
                    if position + field_length > end:
                        malformed = 'Truncated nested TLV field'; break
                    if field_type == 13:
                        try: name = data[position:position + field_length].decode('utf-8')
                        except UnicodeDecodeError: malformed = 'Invalid UTF-8 operator name'; break
                    position += field_length
                if malformed: break
                if name and name.startswith('DistComm'):
                    state = 'invalid' if fixed[1] == INVALID_FLOW else 'valid'
                    counts[name][state] += 1
                    offsets.setdefault(name, {'offset': offset, 'record_type': record_type, 'tid': fixed[0],
                                              'flow_id': str(fixed[1]), 'module': fixed[6], 'event': fixed[7]})
            offset = end
    if _signature([path, infos[0]]) != signature:
        return dict(unavailable, reason='TLV changed during audit')
    verified = {}
    if not malformed:
        for name, expected in expected_names.items():
            local = counts.get(name, {})
            if local.get('valid', 0) == 0 and local.get('invalid', 0) == expected and expected > 0:
                verified[name] = {'invalid_count': expected, 'valid_count': 0, 'sample': offsets[name]}
    result = {'confirmed': bool(verified), 'verified_names': verified, 'file': str(path),
              'ms_version': info['ms_version'], 'record_count': record_count, 'counts': {k: dict(v) for k, v in counts.items()},
              'malformed': malformed, 'fingerprint': signature,
              'reason': 'Every trace-matched DistComm LaunchTask in each confirmed class has INVALID_FLOW_ID' if verified else (malformed or 'TLV/trace counts did not independently confirm an all-invalid class')}
    if cache_path:
        cache_path = Path(cache_path); cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'version': VERSION, 'fingerprint': signature,
            'expected_names': expected_names, 'result': result}, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        temporary.replace(cache_path)
    return result
