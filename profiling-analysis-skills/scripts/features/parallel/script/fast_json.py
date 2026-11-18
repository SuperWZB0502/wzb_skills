"""Bounded JSON chunks: avoid millions of tiny writes into gzip/TextIOWrapper."""
import json


def iter_json_bytes(value, chunk_items=4096):
    def scalar(item):
        return json.dumps(item,ensure_ascii=False,separators=(',',':'),default=str).encode('utf-8')
    if isinstance(value,dict):
        yield b'{'
        for i,(key,item) in enumerate(value.items()):
            if i:yield b','
            if isinstance(key,str):encoded_key=scalar(key)
            elif key is True:encoded_key=b'"true"'
            elif key is False:encoded_key=b'"false"'
            elif key is None:encoded_key=b'"null"'
            elif isinstance(key,(int,float)):encoded_key=scalar(str(key))
            else:raise TypeError('Unsupported JSON key: '+repr(key))
            yield encoded_key+b':'
            yield from iter_json_bytes(item,chunk_items)
        yield b'}'
    elif isinstance(value,list):
        yield b'['
        # Track/flow entries can themselves contain very large node arrays.
        if value and isinstance(value[0],dict) and any(key in value[0] for key in ('nodes','points')):
            for i,item in enumerate(value):
                if i:yield b','
                yield from iter_json_bytes(item,chunk_items)
        else:
            for start in range(0,len(value),chunk_items):
                if start:yield b','
                yield scalar(value[start:start+chunk_items])[1:-1]
        yield b']'
    else:
        yield scalar(value)
