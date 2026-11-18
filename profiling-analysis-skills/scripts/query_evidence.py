#!/usr/bin/env python3
"""Query a bounded evidence slice from the unified analysis output."""
import argparse
from decimal import Decimal, InvalidOperation
import importlib.util
import json
from pathlib import Path
import sys


def attach_details(result, directory):
    path = directory / 'operator_details.json'
    details = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    for event in result['events']:
        identity = str(event['event_id'])
        if event.get('kind') == 'compute':
            ref = details.get('compute_event_refs', {}).get(identity)
            event['operator_details'] = (
                {'status': 'matched', 'csv_line': ref[1],
                 'input_output': details['compute_records'][ref[0]]}
                if ref else {'status': 'unavailable', 'reason': details.get(
                    'compute_unavailable_reasons', {}).get(str(event['rank']),
                    '未生成算子详情，或源数据不能唯一关联')})
        elif event.get('kind', 'communication') == 'communication':
            event['operator_details'] = details.get('communication', {}).get(
                identity, {'status': 'unavailable', 'reason': '未生成算子详情或缺少源记录'})
        else:
            event['operator_details'] = {'status': 'unavailable', 'reason': '此类事件没有计算I/O或通信详情'}
    result['details_source'] = str(path) if path.is_file() else None
    result['details_notes'] = details.get('notes', [])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True, help='统一入口的输出根目录')
    parser.add_argument('--feature', choices=['overview', 'parallel'], default='parallel')
    parser.add_argument('--rank', type=int, action='append')
    parser.add_argument('--group', action='append')
    parser.add_argument('--name')
    parser.add_argument('--step')
    parser.add_argument('--kind', action='append', choices=['compute', 'communication', 'copy', 'free'])
    parser.add_argument('--domain', action='append', choices=['PP', 'DP', 'EP', 'CP', 'TP', 'GLOBAL'])
    parser.add_argument('--category', action='append', help='仅全局总览的通信分类')
    parser.add_argument('--start-us')
    parser.add_argument('--end-us')
    parser.add_argument('--absolute', action='store_true')
    parser.add_argument('--contained', action='store_true')
    parser.add_argument('--unresolved', action='store_true')
    parser.add_argument('--include-stack', action='store_true')
    parser.add_argument('--include-details', action='store_true')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000:
        parser.error('limit应为1～1000')
    try:
        bounds = [Decimal(value) if value is not None else None
                  for value in (args.start_us, args.end_us)]
        if any(value is not None and not value.is_finite() for value in bounds):
            raise InvalidOperation
        if all(value is not None for value in bounds) and bounds[0] > bounds[1]:
            parser.error('start-us不能大于end-us')
    except InvalidOperation:
        parser.error('时间边界必须是有限的微秒数值')
    if args.feature == 'overview':
        if (args.start_us is not None or args.end_us is not None) and not args.absolute:
            parser.error('全局总览时间边界为绝对微秒，需显式提供--absolute')
        if args.kind or args.domain or args.contained or args.include_stack or args.include_details:
            parser.error('kind/domain/contained/include-stack/include-details仅支持parallel')
    elif args.category:
        parser.error('category仅支持overview；parallel按domain筛选')
    script = Path(__file__).resolve().parent / 'features' / args.feature / 'script'
    if not script.is_dir():
        feature_folder={'overview':'功能01_每卡Step耗时统计','parallel':'功能02_5D并行通信图'}[args.feature]
        script=Path(__file__).resolve().parents[1]/feature_folder/'script'
    directory = args.output_dir.expanduser().resolve() / args.feature
    sys.path.insert(0, str(script))
    spec = importlib.util.spec_from_file_location('_wzb_feature_query', script / 'query_evidence.py')
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        common = dict(out=directory, ranks=args.rank, step=args.step, groups=args.group,
                      name=args.name, start=args.start_us, end=args.end_us,
                      limit=args.limit, unresolved=args.unresolved)
        if args.feature == 'overview':
            result = module.query(category=args.category, **common)
        else:
            result = module.query(kinds=args.kind, domains=args.domain, absolute=args.absolute,
                                  contained=args.contained, include_stack=args.include_stack, **common)
            if args.include_details:
                attach_details(result, directory)
        encoded = json.dumps(result, ensure_ascii=False, indent=2, default=str) + '\n'
        if args.output:
            args.output.expanduser().write_text(encoded, encoding='utf-8')
        else:
            print(encoded, end='')
    except (OSError, ValueError, KeyError, ImportError) as exc:
        parser.exit(2, f'查询失败：{exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
