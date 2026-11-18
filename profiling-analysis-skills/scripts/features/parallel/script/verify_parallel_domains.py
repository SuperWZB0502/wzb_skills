#!/usr/bin/env python3
"""Verify derived domain files against independently extracted feature-2 caches."""
import argparse
from collections import Counter
from decimal import Decimal
import csv
import json
from pathlib import Path
from parallel_domain_registry import STAGE_DOMAINS


def verify(output_dir):
    output_dir = Path(output_dir)
    audit = json.loads((output_dir / 'domains/audit.json').read_text(encoding='utf-8'))
    pp = json.loads((output_dir / 'report.json').read_text(encoding='utf-8'))
    caches = [json.loads(path.read_text(encoding='utf-8')) for path in sorted((output_dir / 'cache').glob('rank_*.json'))]
    original = {f"{r['rank']}:{e['ordinal']}": e for r in caches for e in r['events']}
    extracted = {e['event_id']: e for e in audit['events']}
    assert audit['closure_ok'] and not audit['duplicate_event_ids']
    assert set(original) == set(extracted), 'Group-lane events were lost or fabricated'
    for identity, event in extracted.items():
        raw = original[identity]
        for key in ('rank', 'group_id', 'ordinal', 'name', 'absolute_start_us', 'duration_us', 'step'):
            assert str(event[key]) == str(raw[key]), (identity, key)
        assert Decimal(event['start_us']) == Decimal(raw['absolute_start_us']) - Decimal(pp['time_origin_us'])
        assert Decimal(event['end_us']) == Decimal(event['start_us']) + Decimal(event['duration_us'])
    all_ids = []; groups_by_domain = {}; result = {}
    original_regions = {r['region_id']: r for r in pp.get('compute_regions', [])}
    for domain in (*STAGE_DOMAINS, 'GLOBAL', 'unresolved'):
        data = json.loads((output_dir / f'domains/{domain}/report.json').read_text(encoding='utf-8'))
        ids = [e['event_id'] for e in data['events']]
        assert all(extracted[identity]['domain'] == domain for identity in ids)
        assert len(ids) == len(set(ids))
        assert data['steps'] == pp['steps'] and data['time_origin_us'] == pp['time_origin_us']
        for g in data['groups']:
            assert g['domain'] == domain
            assert set(g['available_ranks']).isdisjoint(g['missing_ranks'])
            assert set(g['available_ranks']) | set(g['missing_ranks']) == set(g['members'])
        region_ids = []
        for region in data.get('compute_regions', []):
            source = original_regions[region['source_region_id']]
            assert 'segments' not in region, 'Do not duplicate fine-grained PP segments in domain reports'
            assert region['source_group_id'] == source['group_id']
            assert region['source_segments_count'] == len(source.get('segments', []))
            assert Path(region['source_report']).resolve() == (output_dir / 'report.json').resolve()
            assert (output_dir / 'domains' / domain / region['source_report_relative']).resolve() == (output_dir / 'report.json').resolve()
            assert region['region_id'] == f"{domain.lower()}:{region['group_id']}:{source['region_id']}"
            group = next(g for g in data['groups'] if g['group_id'] == region['group_id'])
            assert region['rank'] in group['available_ranks']
            if domain == 'GLOBAL':
                assert group['pp_stage'] is None
                assert region['stage'] == region['pp_stage'] == data['rank_stages'][str(region['rank'])]
            else:
                assert region['stage'] == region['pp_stage'] == group['pp_stage']
            for key, value in source.items():
                if key not in ('segments', 'region_id', 'group_id'):
                    assert region[key] == value, (region['region_id'], key)
            region_ids.append(region['region_id'])
        assert len(region_ids) == len(set(region_ids))
        expected_regions = {(g['group_id'], source['region_id']) for g in data['groups']
                            for source in original_regions.values()
                            if domain != 'unresolved' and source['rank'] in g['available_ranks']
                            and (domain == 'GLOBAL' or source.get('stage') == g['pp_stage'])
                            and source.get('phase') in ('f', 'b') and source.get('micro') is not None
                            and str(source.get('step')) in pp.get('steps', {})}
        assert {(r['group_id'], r['source_region_id']) for r in data.get('compute_regions', [])} == expected_regions
        assert data.get('micro_flows') == [], 'Collectives without verified P2P evidence must not invent micro connections'
        if domain != 'unresolved':
            assert all(e['pp_stage'] in data['pp_stages'] for e in data['events'])
            assert all(e['rank'] in next(g['members'] for g in data['groups'] if g['group_id'] == e['group_id']) for e in data['events'])
        if domain == 'GLOBAL':
            assert data['stage_scoped'] is False
            for group in data['groups']:
                assert len(group['members']) == data['parallel_size']
                assert set(group['members']) == set(range(data['parallel_size']))
            for event in data['events']:
                assert event['stage'] == event['pp_stage'] == data['rank_stages'][str(event['rank'])]
            with (output_dir / 'domains/GLOBAL/purpose_summary.csv').open(encoding='utf-8-sig', newline='') as stream:
                purpose_rows = list(csv.DictReader(stream))
            assert sum(int(row['event_count']) for row in purpose_rows) == len(ids)
            assert sum((Decimal(row['duration_us']) for row in purpose_rows), Decimal(0)) == sum((Decimal(e['duration_us']) for e in data['events']), Decimal(0))
            purpose_ids = [identity for row in purpose_rows for identity in json.loads(row['event_ids'])]
            assert len(purpose_ids) == len(set(purpose_ids)) and set(purpose_ids) == set(ids)
        groups_by_domain[domain] = data['groups']
        result[domain] = {'group_count': len(data['groups']), 'event_count': len(ids),
            'compute_region_count': len(region_ids),
            'duration_us': str(sum((Decimal(e['duration_us']) for e in data['events']), Decimal(0))),
            'groups_by_pp_stage': dict(Counter(g['pp_stage'] for g in data['groups']))}
        all_ids.extend(ids)
    pp_ids = {e['event_id'] for e in audit['events'] if e['domain'] == 'PP'}
    assert set(all_ids) | pp_ids == set(original)
    assert not (set(all_ids) & pp_ids)
    assert len(all_ids) + len(pp_ids) == len(original)
    assert not audit['pp_report_not_covered_ids'] and not audit['additional_pp_event_ids']
    dp_ep_same_members = []
    for dp in groups_by_domain['DP']:
        for ep in groups_by_domain['EP']:
            if set(dp['members']) == set(ep['members']):
                assert dp['group_id'] != ep['group_id']
                dp_ep_same_members.append({'dp_group_id': dp['group_id'], 'ep_group_id': ep['group_id'],
                                           'members': dp['members'], 'pp_stage': dp['pp_stage']})
    return {'passed': True, 'input_group_lane_events': len(original), 'unique_audited_events': len(extracted),
            'pp_events_preserved': len(pp_ids), 'domains': result,
            'separate_dp_ep_communicators_with_equal_members': dp_ep_same_members,
            'gradient_allreduce_in_unresolved': sum(e.get('metric_category') == 'Grad AR' and e['domain'] == 'unresolved' for e in audit['events']),
            'gradient_allreduce_in_global': sum(e.get('metric_category') == 'Grad AR' and e['domain'] == 'GLOBAL' for e in audit['events']),
            'source': 'Original feature 2 fingerprint-validated group-lane event caches, not feature 1 aggregates',
            'checks': ['No lost, duplicate or invented communication event', 'Raw identifiers and Decimal timestamps preserved',
                       'Common PP time origin and step windows preserved', 'PP event identities unchanged',
                       'Separate DP and EP groups with equal rank membership retained', 'Missing ranks produce no fabricated events',
                       'Domain micro blocks reference unchanged same-rank/step PP regions without duplicating fine-grained segments',
                       'No fabricated collective micro-flow connections']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'output')
    parser.add_argument('--result', type=Path, default=Path(__file__).resolve().parents[1] / 'validation/domain_backend_verification.json')
    args = parser.parse_args()
    data = verify(args.output_dir)
    args.result.parent.mkdir(parents=True, exist_ok=True)
    args.result.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in data.items() if key != 'separate_dp_ep_communicators_with_equal_members'}, ensure_ascii=False))
