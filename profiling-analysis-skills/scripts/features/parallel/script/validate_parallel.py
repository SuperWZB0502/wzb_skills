"""Dataset-independent structural/conservation checks; optional complete source-field digest comparison."""
import argparse,hashlib,json
from collections import Counter,defaultdict
from decimal import Decimal as D
from pathlib import Path
import ijson
from workflow_support import load_json,write_json
from parallel_domain_registry import DOMAINS

FIELDS=('rank','stage','step','pid','tid','name','absolute_start_us','start_us','duration_us','end_us')
def event_digest(e,kind,domain):return hashlib.sha256(json.dumps([kind,domain,*[None if e.get(k) is None else str(e[k]) for k in FIELDS]],ensure_ascii=False,separators=(',',':')).encode()).digest()
def validate(output_dir,deep=False,communication_only=False):
    out=Path(output_dir);pp=load_json(out/'report.json');summary=load_json(out/'unified_summary.json');origin=D(pp['time_origin_us']);errors=[];notes=[];domain_ids=set();expected={};domain_counts=Counter();ranks=set();source_count=0
    for domain in (*DOMAINS,'unresolved'):
        path=out/f'domains/{domain}/report.json'
        report=pp if domain=='PP' else load_json(path) if path.exists() else {'events':[]}
        for e in report['events']:
            if str(e['step']) not in pp['steps']:continue
            identity=e['event_id'];source_count+=1
            if identity in domain_ids:errors.append(f'重复通信域归属：{identity}')
            domain_ids.add(identity)
            if domain!='unresolved' or summary.get('stats',{}).get('by_domain',{}).get('unresolved',0):
                domain_counts[domain]+=1
                if deep:expected[identity]=event_digest(e,'communication',domain)
    raw_count=0
    for path in sorted((out/'cache').glob('rank_*.json')):
        row=load_json(path);ranks.add(int(row['rank']));raw_count+=sum(str(e['step']) in pp['steps'] for e in row['events'])
    if raw_count!=source_count:errors.append(f'通信原始事件守恒失败：{raw_count} != {source_count}')
    if deep and not communication_only and (out/'streams/report.json').exists():
        with (out/'streams/report.json').open('rb') as handle:
            for e in ijson.items(handle,'events.item'):
                if str(e['step']) in pp['steps']:expected[e['event_id']]=event_digest(e,e['kind'],None)
    markers=load_json(out/'phase_markers.json') if not communication_only and (out/'phase_markers.json').exists() else {}
    marker_ids={identity for r in markers.get('optimizer_regions',[])+markers.get('recompute_regions',[]) for identity in r.get('event_ids',[])}
    marker_events={};adam_events=defaultdict(set)
    actual=Counter();stack_status_counts=Counter();actual_domains=Counter();by_step=defaultdict(Counter);seen=set();stack_ids=set();actual_stacks=set();checked=0
    with (out/'unified_report.json').open('rb') as handle:
        for e in ijson.items(handle,'events.item'):
            identity=e['event_id'];checked+=1
            if identity in seen:errors.append(f'统一事件重复：{identity}')
            seen.add(identity);actual[e['kind']]+=1;by_step[str(e['step'])][e['kind']]+=1
            if e['kind']=='communication':actual_domains[e['domain']]+=1
            if D(e['start_us'])+D(e['duration_us'])!=D(e['end_us']):errors.append(f'时间不闭合：{identity}')
            if D(e['absolute_start_us'])-origin!=D(e['start_us']):errors.append(f'时间原点不一致：{identity}')
            if e['kind']!='free':stack_status_counts[e.get('stack_status','unavailable')]+=1
            if e.get('stack_id'):actual_stacks.add(e['stack_id'])
            if identity in marker_ids:marker_events[identity]=e
            if e['kind']=='compute' and 'adam' in e['name'].lower():adam_events[(int(e['rank']),str(e['step']))].add(identity)
            if deep and expected.pop(identity,None)!=event_digest(e,e['kind'],e.get('domain')):errors.append(f'事件字段与来源不同：{identity}')
    if expected:errors.append(f'{len(expected)}个来源事件未出现在统一图中')
    if actual!=Counter(summary['stats']['by_kind']):errors.append('事件流汇总与统一报告不符')
    if actual_domains!=domain_counts:errors.append('通信域汇总与来源不符')
    # Annotations must be backed by the actual displayed device events and retain
    # independent checkpoint identities, even when their parent b is clipped.
    marker_checks=0
    if markers.get('schema_version',0)>=2:
        parent_map={r['region_id']:r for r in pp.get('compute_regions',[])};replay_sources=set();replay_events=set()
        for region in markers.get('optimizer_regions',[])+markers.get('recompute_regions',[]):
            marker_checks+=1;identity=region['region_id'];ids=region.get('event_ids',[])
            evidence=[marker_events[eid] for eid in ids if eid in marker_events]
            if len(evidence)!=len(ids):errors.append(f'阶段标记引用不存在的设备事件：{identity}')
            if any(e['kind']!='compute' or int(e['rank'])!=int(region['rank']) or str(e['step'])!=str(region['step']) for e in evidence):errors.append(f'阶段标记关联的卡/Step/流不一致：{identity}')
            if evidence and (D(region['start_us'])!=min(D(e['start_us']) for e in evidence) or D(region['end_us'])!=max(D(e['end_us']) for e in evidence)):errors.append(f'阶段包络与设备证据起止不一致：{identity}')
            if region['phase']=='opt':
                if set(ids)!=adam_events[(int(region['rank']),str(region['step']))]:errors.append(f'opt未完整对应本卡本Step的Adam设备算子：{identity}')
            else:
                parent=parent_map.get(region['parent_region_id']);host=region.get('framework_replays',[])
                if not parent or parent['phase']!='b':errors.append(f'重计算缺少父b：{identity}')
                if len(host)!=1:errors.append(f'重计算跨层合并：{identity}')
                elif (region['rank'],host[0]['ordinal']) in replay_sources:errors.append(f'层重执行被重复绘制：{identity}')
                else:replay_sources.add((region['rank'],host[0]['ordinal']))
                if replay_events.intersection(ids):errors.append(f'设备算子重复归属多个重计算层：{identity}')
                replay_events.update(ids)
                if parent and region['displayable'] and not (D(parent['display_start_us'])<=D(region['display_start_us'])<D(region['display_end_us'])<=D(parent['display_end_us'])):errors.append(f'重计算显示区间越过父b：{identity}')
    stacks=out/'streams/stack_table.json'
    if stacks.exists() and not communication_only:
        stack_ids=set(load_json(stacks))
        if not actual_stacks<=stack_ids:errors.append('事件引用不存在的Python栈')
    from free_boundaries import analyze
    free=analyze(out) if not communication_only else {'status':'not_requested','checks':[]}
    for check in free.get('checks',[]):
        if check['step']=='unassigned':continue
        if check['explanation']=='unexplained_difference':notes.append({'capability':'free','needs_agent':True,'reason':check})
    mbn=pp.get('micro_configuration',{}).get('expected_mbn')
    counts=pp.get('enrichment_stats',{}).get('phase_counts',{})
    unknown_micro=[{'scope':key,**count} for key,count in counts.items() if mbn is not None and (count['f']!=mbn or count['b']!=mbn)]
    requested=stack_status_counts['available']+stack_status_counts['unavailable']
    stack_percent=format(D(stack_status_counts['available'])*100/D(requested),'.2f') if requested else None
    result={'stack_status_counts':dict(stack_status_counts),'stack_available_percent':stack_percent,'stack_ratio_scope':'已请求调用栈分析的非Free事件，包含通信/计算/拷贝', 'schema_version':1,'passed':not errors,'level':'deep' if deep else 'standard','event_count':checked,'by_kind':dict(actual),'by_domain':dict(actual_domains),'by_step':{k:dict(v) for k,v in by_step.items()},
        'raw_communication_events':raw_count,'unresolved_events':domain_counts.get('unresolved',source_count-sum(domain_counts.values())),'expected_mbn':mbn,'incomplete_micro_scopes':unknown_micro,
        'phase_marker_checks':marker_checks,'free_status':free['status'],'free_checks':free.get('checks',[]),'notes':notes,'errors':errors[:100]}
    write_json(out/'validation.json',result);return result
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--deep',action='store_true');p.add_argument('--communication-only',action='store_true');a=p.parse_args();r=validate(a.output_dir,a.deep,a.communication_only);print(r);raise SystemExit(0 if r['passed'] else 2)
