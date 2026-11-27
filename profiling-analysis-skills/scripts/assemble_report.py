#!/usr/bin/env python3
"""Assemble rendered feature sections into a fresh, self-contained HTML report.

Only consumes the outputs of the bundled Python renderers. Does not read the
previous destination HTML or ask an agent to generate markup.
"""
import argparse
import base64
import gzip
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re

SECTIONS = {
    'overview': ('<!-- MSINSIGHT:rank-step-time:START -->', '<!-- MSINSIGHT:rank-step-time:END -->'),
    'parallel': ('<!-- MSINSIGHT:pp-schedule:START -->', '<!-- MSINSIGHT:pp-schedule:END -->'),
    'operator': ('<!-- MSINSIGHT:operator-details:START -->', '<!-- MSINSIGHT:operator-details:END -->'),
}
PAYLOAD = re.compile(r'(<script type="application/json" id="(?:rank-step-time-data|pp-schedule-data|'
                     r'parallel-presentation-data|operator-details-data)">)(.*?)(</script>)',re.S)
WATERMARK = re.compile(r'^(?:/\* 页面水印：[^\n]*|body::after\{[^\n]*data:image/svg\+xml[^\n]*)\n',re.M)


def portable_section(section, aliases):
    patterns=[]
    for root,label in sorted(aliases.items(),key=lambda item:len(str(item[0])),reverse=True):
        parts=re.split(r'[/\\]',str(Path(root).resolve()))
        regex=r'[/\\]'.join(re.escape(part) for part in parts)
        patterns.append((re.compile(regex+r'(?=$|[\\/\s"\'<>:;,])',re.I if os.name=='nt' else 0),label))

    def clean(value):
        if isinstance(value,str):
            for pattern,label in patterns:
                value=pattern.sub(lambda _:label,value)
            return value
        if isinstance(value,list):
            return [clean(item) for item in value]
        if not isinstance(value,dict):
            return value
        if (isinstance(value.get('data'),str) and isinstance(value.get('sha256'),str)
                and isinstance(value.get('uncompressed_bytes'),int)):
            raw=json.loads(gzip.decompress(base64.b64decode(value['data'])))
            sanitized=clean(raw)
            if sanitized==raw:return value
            packed=json.dumps(sanitized,ensure_ascii=False,separators=(',',':')).encode('utf-8')
            return {**value,'data':base64.b64encode(gzip.compress(packed,mtime=0)).decode('ascii'),
                    'sha256':hashlib.sha256(packed).hexdigest(),'uncompressed_bytes':len(packed)}
        result={clean(key):clean(item) for key,item in value.items()}
        if 'metadata' in result and 'event_chunks' in result:
            result['uncompressed_bytes']=result['metadata']['uncompressed_bytes']+sum(
                part['uncompressed_bytes'] for part in result['event_chunks'])
        return result

    def replace(match):
        data=json.dumps(clean(json.loads(match[2])),ensure_ascii=False,separators=(',',':')).replace('<','\\u003c')
        return match[1]+data+match[3]

    section=WATERMARK.sub('',section)
    return PAYLOAD.sub(replace,section) if patterns else section


def assemble(feature_pages, html, aliases=None):
    """Validate sections in canonical order and atomically publish one fresh page."""
    unknown = set(feature_pages) - set(SECTIONS)
    if unknown or not feature_pages:
        raise ValueError('需要至少一个有效功能页面；未知功能：' + str(sorted(unknown)))
    blocks = []
    for name, (start, end) in SECTIONS.items():
        if name not in feature_pages:
            continue
        text = Path(feature_pages[name]).read_text(encoding='utf-8')
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError(f'{name}功能段标记缺失或重复：{feature_pages[name]}')
        a = text.index(start)
        b = text.index(end)
        if b <= a:
            raise ValueError(f'{name}功能段标记顺序错误')
        section = text[a:b + len(end)]
        for token in ('__DATA__', '__SUMMARY__', '__DETAILS__', '__DETAIL_UI__', '__COLUMN_UI__',
                      '__OPERATOR_PAYLOAD__', '__OPERATOR_SCRIPT__'):
            if token in section:
                raise ValueError(f'{name}功能模板尚未完成注入：{token}')
        blocks.append(portable_section(section,aliases or {}))
    # Match the feature renderers' document shell.
    title = escape('Profiling 分析报告')
    content = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               f'<title>{title}</title><style>body{{margin:0;background:#f5f7fa}}</style>'
               '</head><body>' + '\n'.join(blocks) + '</body></html>')
    html = Path(html).expanduser().resolve()
    html.parent.mkdir(parents=True, exist_ok=True)
    temp = html.with_name('.' + html.name + f'.{os.getpid()}.tmp')
    try:
        temp.write_bytes(content.encode('utf-8'))
        temp.replace(html)
    finally:
        temp.unlink(missing_ok=True)
    return {'html': str(html), 'features': [name for name in SECTIONS if name in feature_pages],
            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(), 'bytes': html.stat().st_size}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--overview', type=Path)
    p.add_argument('--parallel', type=Path)
    p.add_argument('--operator', type=Path)
    p.add_argument('--html', type=Path, required=True)
    args = p.parse_args()
    try:
        result = assemble({key: value for key, value in vars(args).items() if key in SECTIONS and value}, args.html)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
