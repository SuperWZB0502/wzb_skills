"""Lossless self-contained HTML transport; canonical JSON outputs stay intact."""
import base64
import gzip
import hashlib
import json


def _pack(value):
    raw = json.dumps(value,ensure_ascii=False,default=str,separators=(',',':'),allow_nan=False).encode('utf-8')
    compressed = gzip.compress(raw,compresslevel=6,mtime=0)
    return {'uncompressed_bytes':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(),
            'data':base64.b64encode(compressed).decode('ascii')}


def encode_report(report, compute_refs=None):
    from compact_timeline import encode_columns, numbers
    events = report.get('events', [])
    metadata = _pack({key:value for key,value in report.items() if key != 'events'})
    chunks = []
    for start in range(0, len(events), 25000):
        rows = events[start:start+25000]
        block = {'count':len(rows), 'columns':encode_columns(rows)}
        if compute_refs is not None:
            refs = [compute_refs.get(str(row['event_id'])) for row in rows]
            block['compute_refs'] = {'records':numbers([ref[0] if ref else -1 for ref in refs]),
                                     'lines':numbers([ref[1] if ref else 0 for ref in refs])}
        chunks.append(_pack(block))
    envelope = {'mode':'unified', 'encoding':'gzip-base64-columns-v1',
                'metadata':metadata, 'event_chunks':chunks, 'event_count':len(events),
                'detail_refs_embedded':compute_refs is not None,
                'uncompressed_bytes':metadata['uncompressed_bytes']+sum(c['uncompressed_bytes'] for c in chunks)}
    return json.dumps(envelope,ensure_ascii=False,separators=(',',':'))


def decode_report(encoded):
    envelope = json.loads(encoded) if isinstance(encoded,str) else encoded
    if envelope.get('encoding') == 'gzip-base64-columns-v1':
        from compact_timeline import decode_columns
        result = _unpack(envelope['metadata']); result['events'] = []
        for part in envelope['event_chunks']:
            block = _unpack(part)
            result['events'].extend(decode_columns(block['columns'], block['count']))
        if len(result['events']) != envelope['event_count']:
            raise ValueError('HTML列式事件数不匹配')
        return result
    if envelope.get('encoding') == 'gzip-base64-chunks':
        result = _unpack(envelope['metadata']); result['events'] = []
        for part in envelope['event_chunks']: result['events'].extend(_unpack(part))
        if len(result['events']) != envelope['event_count']:
            raise ValueError('HTML分块事件数不匹配')
        return result
    if envelope.get('encoding') != 'gzip-base64':
        return envelope
    return _unpack(envelope)


def _unpack(part):
    raw = gzip.decompress(base64.b64decode(part['data'],validate=True))
    if len(raw) != part['uncompressed_bytes'] or hashlib.sha256(raw).hexdigest() != part['sha256']:
        raise ValueError('HTML图表数据长度或校验和不匹配')
    return json.loads(raw)
