#!/usr/bin/env python3
"""Assemble rendered feature sections into a fresh, self-contained HTML report.

Only consumes the outputs of the bundled Python renderers. Does not read the
previous destination HTML or ask an agent to generate markup.
"""
import argparse
import hashlib
from html import escape
import json
import os
from pathlib import Path

SECTIONS = {
    'overview': ('<!-- MSINSIGHT:rank-step-time:START -->', '<!-- MSINSIGHT:rank-step-time:END -->'),
    'parallel': ('<!-- MSINSIGHT:pp-schedule:START -->', '<!-- MSINSIGHT:pp-schedule:END -->'),
}


def assemble(feature_pages, html):
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
        for token in ('__DATA__', '__SUMMARY__', '__DETAILS__', '__DETAIL_UI__', '__COLUMN_UI__'):
            if token in section:
                raise ValueError(f'{name}功能模板尚未完成注入：{token}')
        blocks.append(section)
    # Match the established feature renderers' document shell; all styles,
    # interactions, data and watermarks live inside their maintained sections.
    title = escape('MSINSIGHT 功能演示')
    content = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
               '<meta name="viewport" content="width=device-width,initial-scale=1">'
               f'<title>{title}</title><style>body{{margin:0;background:#f5f7fa}}</style>'
               '</head><body>' + '\n'.join(blocks) + '</body></html>')
    html = Path(html).expanduser().resolve()
    html.parent.mkdir(parents=True, exist_ok=True)
    temp = html.with_name('.' + html.name + f'.{os.getpid()}.tmp')
    try:
        temp.write_text(content, encoding='utf-8')
        temp.replace(html)
    finally:
        temp.unlink(missing_ok=True)
    return {'html': str(html), 'features': [name for name in SECTIONS if name in feature_pages],
            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(), 'bytes': html.stat().st_size}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--overview', type=Path)
    p.add_argument('--parallel', type=Path)
    p.add_argument('--html', type=Path, required=True)
    args = p.parse_args()
    try:
        result = assemble({key: value for key, value in vars(args).items() if key in SECTIONS and value}, args.html)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
