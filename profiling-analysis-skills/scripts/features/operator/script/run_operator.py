#!/usr/bin/env python3
"""Generate Chinese operator details from exported Ascend profiling."""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from assemble_report import assemble
from operator_data import collect, discover


VERSION = 2
HERE = Path(__file__).resolve().parent


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(sources):
    return [[str(path), stat.st_size, stat.st_mtime_ns]
            for path in sources if (stat := path.stat())]


def validate(report):
    if report['version'] != VERSION or not report['cards']:
        raise ValueError('算子分析缓存版本不匹配或卡列表为空，请重新分析')
    for card in report['cards']:
        rows = card['rows']
        covered = set()
        for kind, statistics in card['statistics'].items():
            for group in statistics:
                members = group['members']
                if not members or len(members) != group['count']:
                    raise ValueError('算子分组数量与明细不一致')
                for index in members:
                    row = rows[index]
                    if (index in covered or row['kind'] != kind or row['type'] != group['type'] or
                            (kind == 'compute' and row['core'] != group['core'])):
                        raise ValueError('算子分组键与明细不一致')
                    covered.add(index)
        if len(covered) != len(rows):
            raise ValueError('算子分组未覆盖完整列表')


def render(report, html):
    data = json.dumps(report['cards'], ensure_ascii=False, separators=(',', ':')).encode()
    payload = base64.b64encode(gzip.compress(data, mtime=0)).decode('ascii')
    template = (HERE / 'operator_details.html').read_text(encoding='utf-8')
    script = (HERE / 'operator_details.js').read_text(encoding='utf-8')
    section = template.replace('__OPERATOR_PAYLOAD__', payload).replace('__OPERATOR_SCRIPT__', script)
    # Assemble before replacing the target, including when it is a previously generated page.
    preview = html.with_name(f'.{html.name}.{os.getpid()}.section')
    try:
        atomic_write(preview, section)
        assemble({'operator': preview}, html)
    finally:
        preview.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiling', type=Path)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--html', type=Path)
    parser.add_argument('--step')
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--browser-check', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--render-only', action='store_true')
    mode.add_argument('--validate-only', action='store_true')
    args = parser.parse_args(argv)
    out = args.output_dir.expanduser().resolve()
    html = (args.html or out / 'report.html').expanduser().resolve()
    cache = out / 'report.json'
    started = time.perf_counter()
    try:
        cached = json.loads(cache.read_text(encoding='utf-8')) if cache.is_file() else {}
        if args.render_only or args.validate_only:
            if not cached:
                raise ValueError('缺少算子分析结果，请先正常运行')
            root = Path(cached['profiling_path'])
            if args.profiling and discover(args.profiling)[0] != root:
                raise ValueError('已有结果与 --profiling 不一致')
            report = cached
            execution = 'render-only' if args.render_only else 'validate-only'
        else:
            if args.profiling is None:
                raise ValueError('首次分析需要 --profiling')
            root, sources = discover(args.profiling)
            state = fingerprint(sources)
            if cached and cached['profiling_path'] != str(root):
                raise ValueError('该输出目录已绑定另一个 profiling')
            report = cached
            execution = 'cached'
            if (args.refresh or cached.get('version') != VERSION or cached.get('sources') != state or
                    cached.get('step') != args.step):
                cards, issues = collect(sources, args.step)
                if fingerprint(sources) != state:
                    raise ValueError('原始算子数据在读取期间发生变化')
                report = {'version': VERSION, 'profiling_path': str(root), 'sources': state,
                          'step': args.step, 'cards': cards, 'issues': issues}
                execution = 'parsed'
        protected = root if root.is_dir() else root.parent
        if (html.suffix.lower() not in ('.html', '.htm') or protected in (out, html) or
                protected in out.parents or protected in html.parents):
            raise ValueError('输出目录和 HTML 必须位于原始 profiling 之外，HTML 使用 .html 或 .htm 后缀')
        validate(report)
        if execution == 'parsed':
            atomic_write(cache, json.dumps(report, ensure_ascii=False, separators=(',', ':')))
        if not args.validate_only:
            render(report, html)
            if args.browser_check:
                from browser_check import check_html
                print(json.dumps(check_html(html, out), ensure_ascii=False))
        statistics = {'cards': len(report['cards']), 'operators': 0, 'compute_groups': 0, 'communication_groups': 0}
        for card in report['cards']:
            statistics['operators'] += len(card['rows'])
            statistics['compute_groups'] += len(card['statistics']['compute'])
            statistics['communication_groups'] += len(card['statistics']['communication'])
        status = 'partial' if report['issues'] else 'complete'
        workflow = {'schema_version': 1, 'feature': 'operator', 'status': status, 'mode': execution,
                    'statistics': statistics,
                    'capabilities': {'operator': {'status': status, 'reason': '；'.join(report['issues'])}},
                    'artifacts': {'report': str(cache), 'html': str(html)},
                    'wall_seconds': round(time.perf_counter() - started, 3)}
        atomic_write(out / 'workflow.json', json.dumps(workflow, ensure_ascii=False, indent=2) + '\n')
        summary = ['# 算子详情', '', f'状态：{status}',
                   f'卡数：{statistics["cards"]}，算子记录：{statistics["operators"]}',
                   *['- ' + issue for issue in report['issues']]]
        atomic_write(out / 'summary.md', '\n'.join(summary) + '\n')
        print(json.dumps(workflow, ensure_ascii=False))
        return 0
    except (ValueError, OSError, ImportError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
