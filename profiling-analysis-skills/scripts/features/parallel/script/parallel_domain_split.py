#!/usr/bin/env python3
"""Extract the 5D parallel communication graph and an independent global domain.

TEXT inputs: profiler_info*.json, profiler_metadata.json, trace_view.json.
Communication uses HCCL Group lanes; compute/copy use recorded device tasks,
Free uses Overlap Analysis, and Python stacks require association evidence.
Uses ijson for streaming. Timestamps retain Decimal precision and a common origin.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import csv
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from pp_enrichment import Collector, enrich_report
from pp_flow import enrich_micro_flows
from parallel_domain_registry import DOMAINS as DOMAIN_ORDER

FEATURE_DIR=Path(__file__).absolute().parents[1]
ROOT=FEATURE_DIR.parent
VERSION=5
START='<!-- MSINSIGHT:pp-schedule:START -->'
END='<!-- MSINSIGHT:pp-schedule:END -->'
PLACEHOLDER='<!-- MSINSIGHT_FEATURES_END -->'
GROUP_LANE=re.compile(r'^(?:\d+:)?Group (.+) Communication$')
STEP_MARKER=re.compile(r'ProfilerStep#(\d+)')
COMM_PROCESSES={'communication','hccl'}


def decimal(value,context):
    try:
        result=Decimal(str(value))
        if not result.is_finite() or result<0:raise InvalidOperation
    except (InvalidOperation,ValueError):
        raise ValueError(f'{context} 不是有效非负时间：{value!r}') from None
    return result


def dump_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name('.'+path.name+'.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,separators=(',',':'),default=str,allow_nan=False)+'\n',encoding='utf-8')
    tmp.replace(path)


def iter_events(path):
    try:import ijson
    except ImportError:
        raise ValueError('缺少轻量流式解析依赖：python3 -m pip install -r '+str(FEATURE_DIR/'script/requirements.txt')) from None
    with path.open('rb') as stream:
        first=b''
        while not first:
            char=stream.read(1)
            if not char:raise ValueError(f'{path} 是空trace')
            if not char.isspace():first=char
        if first not in (b'[',b'{'):raise ValueError(f'{path} 不是JSON trace')
        stream.seek(0)
        try:
            yield from ijson.items(stream,'item' if first==b'[' else 'traceEvents.item')
        except (ValueError,ijson.JSONError) as error:
            raise ValueError(f'{path} trace解析失败或文件不完整：{error}') from error


def sample_path(document):
    match=re.search(r'样例\s*profiling\s*[：:]\s*`?([^\n`]+)',document.read_text(encoding='utf-8'),re.I)
    if not match:raise ValueError(f'{document} 中没有样例profiling路径')
    result=Path(match.group(1).strip()).expanduser()
    return result if result.is_absolute() else document.parent/result


def discover(profiling,allow_partial=False):
    if not profiling.is_dir():raise ValueError(f'profiling目录不存在：{profiling}')
    folders=[]
    for parent,dirs,_ in os.walk(profiling):
        folder=Path(parent)
        if folder.name.endswith(('_ascend_pt','_ascend_ms')):
            folders.append(folder);dirs[:]=[]
        else:dirs[:]=sorted(d for d in dirs if d not in {'FRAMEWORK','ASCEND_PROFILER_OUTPUT','logs'} and not d.startswith('PROF_'))
    if not folders:raise ValueError('未找到 *_ascend_ms 或 *_ascend_pt 的TEXT profiling目录')
    ranks={}
    for folder in sorted(folders):
        infos=list(folder.glob('profiler_info*.json'))
        if len(infos)!=1:
            if allow_partial:continue
            raise ValueError(f'{folder} 需要唯一profiler_info*.json')
        info=json.loads(infos[0].read_text(encoding='utf-8'))
        rank=info.get('ms_profiler_info',{}).get('rank_id',info.get('rank_id'))
        name_rank=re.fullmatch(r'profiler_info_(\d+)\.json',infos[0].name)
        if rank is None and name_rank:rank=name_rank.group(1)
        if not re.fullmatch(r'\d+',str(rank)):raise ValueError(f'{infos[0]} 缺少全局rank_id')
        rank=int(rank)
        if name_rank and int(name_rank.group(1))!=rank:raise ValueError(f'{infos[0]} 卡号与文件名不一致')
        if rank in ranks:raise ValueError(f'重复rank {rank}，请指定一次完整采集目录')
        metadata=folder/'profiler_metadata.json';trace=folder/'ASCEND_PROFILER_OUTPUT/trace_view.json'
        if not metadata.is_file() or not trace.is_file():
            if allow_partial:continue
            raise ValueError(f'rank {rank} 缺少 profiler_metadata.json 或 trace_view.json')
        ranks[rank]={'folder':folder,'info':infos[0],'metadata':metadata,'trace':trace}
    return ranks


def fingerprint(paths):
    return [{'path':str(p.resolve()),'size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for p in paths]


def read_rank(rank,source,cache_path,refresh=False,observer=None):
    signature=fingerprint([source['trace'],source['metadata'],source['info']])
    if not refresh and cache_path.exists():
        try:
            cached=json.loads(cache_path.read_text(encoding='utf-8'))
            if cached.get('version')==VERSION and cached.get('fingerprint')==signature and cached.get('rank')==rank:
                return cached,True
        except (ValueError,OSError):pass
    metadata=json.loads(source['metadata'].read_text(encoding='utf-8'))
    processes={};threads={};candidates=[];steps=[];contexts=[];count=0;collector=Collector();trace_begin=None;trace_end=None
    from domain_trace import RELEVANT_CONTEXT
    for ordinal,event in enumerate(iter_events(source['trace'])):
        count+=1
        if not isinstance(event,dict):raise ValueError(f'{source["trace"]} 包含非对象事件')
        if observer is not None:observer(event,ordinal)
        collector.feed(event,ordinal)
        ph=event.get('ph');name=str(event.get('name',''))
        pid=str(event.get('pid',''));tid=str(event.get('tid',''))
        if ph=='M':
            args=event.get('args') or {}
            if name=='process_name':processes[pid]=str(args.get('name',''))
            elif name=='thread_name':threads[(pid,tid)]=str(args.get('name',''))
        elif ph=='X':
            begin=Decimal(str(event.get('ts',0)));finish=begin+Decimal(str(event.get('dur',0)))
            trace_begin=begin if trace_begin is None else min(trace_begin,begin);trace_end=finish if trace_end is None else max(trace_end,finish)
            if '.py(' in name and RELEVANT_CONTEXT.search(name):contexts.append({'name':name,'absolute_start_us':str(event['ts']),'duration_us':str(event.get('dur',0))})
            marker=STEP_MARKER.search(name)
            if marker:
                begin=decimal(event.get('ts'),'ProfilerStep ts');duration=decimal(event.get('dur'),'ProfilerStep dur')
                steps.append({'step':marker.group(1),'absolute_start_us':str(begin),'duration_us':str(duration)})
            if name.lower().startswith(('hcom_','hccl_')) or GROUP_LANE.fullmatch(threads.get((pid,tid),'')):
                candidates.append({'ordinal':ordinal,'pid':pid,'tid':tid,'name':name,
                                   'ts':event.get('ts'),'dur':event.get('dur'),'args':event.get('args') or {}})
    records=[]
    for event in candidates:
        if processes.get(event['pid'],'').casefold() not in COMM_PROCESSES:continue
        lane=threads.get((event['pid'],event['tid']),'');match=GROUP_LANE.fullmatch(lane)
        if not match:continue
        begin=decimal(event.pop('ts'),f'rank {rank} {event["name"]} ts')
        duration=decimal(event.pop('dur'),f'rank {rank} {event["name"]} dur')
        matching_steps={s['step'] for s in steps if Decimal(s['absolute_start_us'])<=begin<Decimal(s['absolute_start_us'])+Decimal(s['duration_us'])}
        event.update({'rank':rank,'group_id':match.group(1),'lane':lane,
                      'absolute_start_us':str(begin),'duration_us':str(duration),
                      'step':next(iter(matching_steps)) if len(matching_steps)==1 else 'unassigned'})
        records.append(event)
    pp_groups={identity for identity,detail in metadata.get('parallel_group_info',{}).items()
               if pp_order([int(r) for r in detail.get('global_ranks',[])],metadata,detail)[0] is not None}
    enrichment=collector.finish(steps,pp_groups)
    result={'enrichment':enrichment,'version':VERSION,'rank':rank,'fingerprint':signature,'metadata':metadata,
            'steps':steps,'events':records,'contexts':contexts,'trace_bounds':{'start_us':str(trace_begin or 0),'end_us':str(trace_end or 1)},'trace_event_count':count}
    # Normalize Decimal args before using the same structure for cold and cached runs.
    result=json.loads(json.dumps(result,default=str,ensure_ascii=False))
    dump_json(cache_path,result)
    return result,False


def pp_order(members,metadata,details):
    from global_communication import is_global_name
    if any(is_global_name(details.get(k,'')) for k in ('pg_name','type','group_name')):return None,None
    label=str(details.get('pg_name',details.get('type',details.get('group_name','')))).lower()
    if re.match(r'^(pp|pipeline)(?:$|[_ -])',label):return list(members),'显式PP元数据成员顺序'
    config=metadata.get('distributed_args',{})
    try:world=int(config['world_size']);pp=int(config['pipeline_model_parallel_size'])
    except (KeyError,ValueError,TypeError):return None,None
    if pp<=1 or world<=0 or world%pp or len(members)!=pp:return None,None
    stride=world//pp
    if len(set(members))!=pp:return None,None
    first=min(members)
    if first>=stride or sorted(members)!=list(range(first,world,stride)):return None,None
    return sorted(members),f'distributed_args + PP rank步长{stride}'


def group_catalog(ranks):
    groups={};warnings=[]
    for row in ranks:
        metadata=row['metadata']
        for identity,detail in metadata.get('parallel_group_info',{}).items():
            members=[int(r) for r in detail.get('global_ranks',[])]
            group=groups.setdefault(identity,{'group_id':identity,'members':members,'aliases':[],
                                              'kind':'other','order_evidence':'元数据成员顺序'})
            if set(group['members'])!=set(members):raise ValueError(f'同一通信组 {identity} 的成员元数据不一致')
            alias=str(detail.get('group_name',identity))
            if alias not in group['aliases']:group['aliases'].append(alias)
            ordered,evidence=pp_order(members,metadata,detail)
            if ordered is not None:group.update({'members':ordered,'kind':'pp','order_evidence':evidence})
        for event in row['events']:
            identity=event['group_id']
            group=groups.setdefault(identity,{'group_id':identity,'members':[], 'aliases':[identity],
                                              'kind':'other','order_evidence':'缺少通信组元数据'})
            group.setdefault('observed_ranks',set()).add(event['rank'])
            group['event_count']=group.get('event_count',0)+1
    catalog=[]
    for group in groups.values():
        if not group.get('event_count'):continue
        observed=group.pop('observed_ranks');group['available_ranks']=sorted(observed)
        group['missing_ranks']=sorted(set(group['members'])-observed)
        group['unexpected_ranks']=sorted(observed-set(group['members'])) if group['members'] else []
        group['complete']=bool(group['members']) and not group['missing_ranks'] and not group['unexpected_ranks']
        group['aliases'].sort();catalog.append(group)
    return sorted(catalog,key=lambda g:(g['kind']!='pp',g['aliases'][0],g['group_id']))


def build_report(ranks,catalog,selectors=None,all_groups=False,step_filter=None):
    selected=[]
    if selectors:
        for selector in selectors:
            matches=[g for g in catalog if g['group_id']==selector or selector in g['aliases']]
            if len(matches)!=1:raise ValueError(f'通信组 {selector!r} 匹配{len(matches)}项，请用 --list-groups 查看完整组标识')
            if matches[0] not in selected:selected.append(matches[0])
    else:selected=[g for g in catalog if all_groups or g['kind']=='pp']
    # A PP communicator is optional: DP/EP/CP/TP, GLOBAL and hardware lanes remain usable.
    identities={g['group_id'] for g in selected}
    raw=[event for row in ranks for event in row['events'] if event['group_id'] in identities and (step_filter is None or event['step']==step_filter)]
    steps=[s for row in ranks for s in row['steps']]
    candidates=[Decimal(s['absolute_start_us']) for s in steps] or [Decimal(e['absolute_start_us']) for row in ranks for e in row['events']] or [Decimal(row.get('trace_bounds',{}).get('start_us','0')) for row in ranks]
    origin=min(candidates,default=Decimal(0))
    bounds={}
    for row in ranks:
        for s in row['steps']:
            begin=Decimal(s['absolute_start_us'])-origin;end=begin+Decimal(s['duration_us'])
            old=bounds.setdefault(s['step'],{'start_us':str(begin),'end_us':str(end)})
            old['start_us']=str(min(Decimal(old['start_us']),begin));old['end_us']=str(max(Decimal(old['end_us']),end))
    events=[];warnings=[];mapping={g['group_id']:g for g in selected}
    for source in raw:
        event=dict(source);group=mapping[event['group_id']]
        event['stage']=group['members'].index(event['rank']) if group['kind']=='pp' and event['rank'] in group['members'] else None
        begin=Decimal(event['absolute_start_us'])-origin;end=begin+Decimal(event['duration_us'])
        event.update({'event_id':str(event['rank'])+':'+str(event['ordinal']),
                      'start_us':str(begin),'end_us':str(end)})
        events.append(event)
        if event['step'] not in bounds:bounds[event['step']]={'start_us':str(begin),'end_us':str(end)}
        else:
            bounds[event['step']]['start_us']=str(min(Decimal(bounds[event['step']]['start_us']),begin))
            bounds[event['step']]['end_us']=str(max(Decimal(bounds[event['step']]['end_us']),end))
    for group in selected:
        if group['missing_ranks']:warnings.append(f'组 {group["aliases"][0]} 缺少成员卡采集：{group["missing_ranks"]}')
        if group['unexpected_ranks']:warnings.append(f'组 {group["aliases"][0]} 存在不在元数据成员中的卡：{group["unexpected_ranks"]}')
    if any(e['step']=='unassigned' for e in events):warnings.append('部分事件无法唯一关联ProfilerStep，单独放入“未分步”')
    events.sort(key=lambda e:(e['rank'],Decimal(e['absolute_start_us']),e['ordinal']))
    if not bounds:
        end=max((Decimal(row.get('trace_bounds',{}).get('end_us','1'))-origin for row in ranks),default=Decimal(1))
        bounds['unassigned']={'start_us':'0','end_us':str(max(end,Decimal(1)))}
        warnings.append('缺少ProfilerStep标记，按完整采集范围显示未分步事件。')
    if step_filter is not None and step_filter not in bounds:raise ValueError(f'没有Step {step_filter}的时间范围')
    used={step_filter} if step_filter is not None else set(bounds)
    return {'schema_version':2,'feature_id':'pp-schedule','time_unit':'us','time_origin_us':str(origin),
            'generated_at':datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
            'groups':selected,'events':events,'steps':{k:v for k,v in sorted(bounds.items(),key=lambda p:(not p[0].isdigit(),int(p[0]) if p[0].isdigit() else p[0])) if k in used},
            'warnings':warnings}


def write_csv(path,report):
    buf=io.StringIO(newline='');writer=csv.writer(buf)
    writer.writerow(['通信组','卡号','Stage','Step','泳道PID','泳道TID','算子','绝对起始时间(μs)','相对起始时间(μs)','持续时间(μs)','相对结束时间(μs)'])
    for e in report['events']:
        writer.writerow([e['group_id'],e['rank'],'' if e['stage'] is None else e['stage'],e['step'],e['pid'],e['tid'],e['name'],e['absolute_start_us'],e['start_us'],e['duration_us'],e['end_us']])
    path.write_text(buf.getvalue(),encoding='utf-8-sig')


def render_sections(report,domain_reports=None,unified_report=None):
    """Render one unified graph, retaining the original PP-only renderer."""
    if unified_report is not None:
        from timeline_payload import encode_report
        from render_parallel import timeline_section
        from layer_markers import collect_layer_regions
        from presentation_summary import collect_summary,encode_summary
        from operator_details import build_details,encode_details
        detail_data=build_details(report,iter(unified_report.get('events',[])))
        encoded=encode_report(unified_report,detail_data.get('compute_event_refs',{}))
        details=encode_details(detail_data,embedded_refs=True)
        return timeline_section(encoded,encode_summary(collect_summary(report)),details,collect_layer_regions(unified_report))
    template=(FEATURE_DIR/'script/parallel_domain_split.html').read_text(encoding='utf-8')
    sections=[]
    for domain,payload in [('PP',report)]+[(d,domain_reports[d]) for d in DOMAIN_ORDER[1:] if domain_reports and d in domain_reports]:
        section=template if domain=='PP' else template.replace('pp-schedule',domain.lower()+'-schedule')
        encoded=json.dumps(payload,ensure_ascii=False,default=str).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
        sections.append(section.replace('__DATA__',encoded))
    return '\n'.join(sections)


def update_html(path,report,domain_reports=None,unified_report=None):
    block=START+'\n'+render_sections(report,domain_reports,unified_report)+'\n'+END
    existing=path.read_text(encoding='utf-8') if path.exists() else ''
    if START in existing:
        if END not in existing:raise ValueError('HTML的功能二区块不完整，请先修复标记')
        begin=existing.index(START);end=existing.index(END,begin)+len(END);result=existing[:begin]+block+existing[end:]
    elif PLACEHOLDER in existing:result=existing.replace(PLACEHOLDER,block+'\n'+PLACEHOLDER,1)
    elif existing.strip():
        match=re.search(r'</body\s*>',existing,re.I)
        if not match:raise ValueError('已有HTML缺少body，未覆盖')
        result=existing[:match.start()]+block+'\n'+existing[match.start():]
    else:result='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MSINSIGHT 功能演示</title><style>body{margin:0;background:#f5f7fa}</style></head><body><main>'+block+'\n'+PLACEHOLDER+'</main></body></html>'
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name('.'+path.name+'.tmp');temp.write_text(result,encoding='utf-8');temp.replace(path)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiling',type=Path)
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--no-html',action='store_true')
    parser.add_argument('--log-dir',type=Path)
    parser.add_argument('--allow-partial',action='store_true')
    parser.add_argument('--communication-only',action='store_true',help='统一图仅解析通信；跳过设备流、栈与阶段标记')
    parser.add_argument('--document',type=Path,default=ROOT/'MSINSIGHT_核心功能拆解.md')
    parser.add_argument('--output-dir',type=Path,default=FEATURE_DIR/'output')
    parser.add_argument('--html',type=Path,default=ROOT/'MSINSIGHT_功能演示.html')
    parser.add_argument('--group',action='append',help='完整通信组标识或唯一的组别名；可多次指定')
    parser.add_argument('--all-groups',action='store_true')
    parser.add_argument('--list-groups',action='store_true')
    parser.add_argument('--step',help='指定ProfilerStep编号')
    parser.add_argument('--refresh',action='store_true',help='忽略缓存，重新扫描原始trace')
    parser.add_argument('--pp-only',action='store_true',help='仅生成PP模块，默认同时输出DP/EP/CP/TP及全局通信域')
    parser.add_argument('--domain-roles',type=Path,help='可选JSON对象：完整通信组标识到基础域或TP-Extend-EP/DP-Modulo-EP/DP-With-CP/DP-With-CP-Modulo-EP的显式映射，结果记录此证据')
    args=parser.parse_args(argv);started=time.perf_counter()
    try:
        profiling=(args.profiling or sample_path(args.document)).expanduser().resolve()
        sources=discover(profiling,args.allow_partial);ranks=[];hits=0
        if not sources:raise ValueError('没有具备trace、元数据和rank信息的TEXT采集卡')
        if args.workers<1:raise ValueError('workers必须大于0')
        if args.workers>1:
            from parallel_workers import prepare_rank_inputs
            prepare_rank_inputs(sources,args.output_dir,args.workers,args.refresh)
        for rank,source in sorted(sources.items()):
            print(f'读取 rank {rank}：{source["trace"].name}',file=sys.stderr,flush=True)
            row,hit=read_rank(rank,source,args.output_dir/'cache'/f'rank_{rank}.json',args.refresh)
            ranks.append(row);hits+=int(hit)
            print(f'  {"缓存" if hit else "扫描完成"}，通信事件{len(row["events"])}',file=sys.stderr,flush=True)
        catalog=group_catalog(ranks)
        # Explicit exported pg_name/mappings can identify PP even without the
        # default rank-stride configuration. Never drop those events upstream.
        from parallel_domains import _semantic_group_file
        from domain_trace import SEMANTIC_ROLES
        semantic_groups,_,_= _semantic_group_file(profiling,ranks)
        supplied_roles=json.loads(args.domain_roles.read_text(encoding='utf-8')) if args.domain_roles else {}
        supplied_roles=supplied_roles.get('communication_group_roles',supplied_roles) if isinstance(supplied_roles,dict) else {}
        for group in catalog:
            semantic=semantic_groups.get(group['group_id'],{})
            role=supplied_roles.get(group['group_id']) or next((supplied_roles[x] for x in group['aliases'] if x in supplied_roles),None) or (SEMANTIC_ROLES.get(str(semantic.get('pg_name','')).casefold()) if not semantic.get('conflict') else None)
            if role=='PP':group.update(kind='pp',order_evidence='explicit group-domain mapping/exported pg_name; metadata member order')
            elif (role or semantic.get('conflict')) and group['kind']=='pp':group['kind']='other'
        dump_json(args.output_dir/'group_catalog.json',catalog)
        if args.list_groups:
            print(json.dumps(catalog,ensure_ascii=False,indent=2));return 0
        report=build_report(ranks,catalog,args.group,args.all_groups,args.step)
        from timeline_training import micro_configuration
        report['micro_configuration']=micro_configuration(profiling,ranks,args.log_dir)
        report['model_log_dir']=str(args.log_dir or profiling.parent/'run_log')
        enrich_report(report,ranks)
        enrich_micro_flows(report,ranks)
        report['profiling_path']=str(profiling)
        report['stats']={'cache_hits':hits,'rank_count':len(ranks),'trace_bytes':sum(s['trace'].stat().st_size for s in sources.values()),'elapsed_seconds':round(time.perf_counter()-started,6)}
        report['sources']=[{'rank':rank,'trace':str(source['trace']),'metadata':str(source['metadata'])} for rank,source in sorted(sources.items())]
        dump_json(args.output_dir/'report.json',report)
        write_csv(args.output_dir/'communication_events.csv',report)
        dump_json(args.output_dir/'compute_regions.json',report['compute_regions'])
        dump_json(args.output_dir/'micro_flows.json',report['micro_flows'])
        dump_json(args.output_dir/'transfer_directions.json',[{'event_id':e['event_id'],'rank':e['rank'],'name':e['name'],'transfers':e['transfers'],'transfer_note':e.get('transfer_note','')} for e in report['events']])
        domain_reports={};unified_report=None
        if not args.pp_only:
            from parallel_domains import build_domain_reports
            missing_raw_index=not args.communication_only and any(
                not (args.output_dir/f'timeline_cache/raw_rank_{rank}.json.gz').is_file() for rank in sources)
            roles=json.loads(args.domain_roles.read_text(encoding='utf-8')) if args.domain_roles else None
            if roles is not None and not isinstance(roles,dict):raise ValueError('--domain-roles需要JSON对象，键为完整通信组标识')
            domain_reports=build_domain_reports(profiling,ranks,report,args.output_dir,
                cache_dir=args.output_dir/'domain_cache',overrides=roles,log_dir=args.log_dir)
            pp_output=args.output_dir/'domains/PP'
            dump_json(pp_output/'report.json',report)
            dump_json(pp_output/'group_catalog.json',report['groups'])
            write_csv(pp_output/'communication_events.csv',report)
            from timeline_streams import collect_timeline_streams
            from unified_timeline import build_unified_report
            report['rank_stages']={str(k):v for payload in domain_reports.values() for k,v in payload.get('rank_stages',{}).items()}
            if args.communication_only:
                streams={'stack_requested':False,'events':[],'stacks':{},'communication_stacks':{},'micro_stacks':{},'rank_info':{},'sources':[],'stats':{},'notes':['本次按communication-only模式仅处理通信，设备流、调用栈及opt/r未请求。']}
                markers={}
            else:
                if args.workers>1:
                    from parallel_workers import prepare_stream_inputs
                    prepare_stream_inputs(ranks,report,args.output_dir,args.workers)
                streams=collect_timeline_streams(profiling,ranks,report,args.output_dir)
                if missing_raw_index:
                    from hyperparallel_domains import mesh_evidence
                    if mesh_evidence(profiling,args.log_dir):
                        domain_reports=build_domain_reports(profiling,ranks,report,args.output_dir,
                            cache_dir=args.output_dir/'domain_cache',overrides=roles,log_dir=args.log_dir)
                        report['rank_stages']={str(k):v for payload in domain_reports.values() for k,v in payload.get('rank_stages',{}).items()}
                from phase_markers import collect_phase_markers
                markers=collect_phase_markers(report,streams,args.output_dir)
            domain_order=(*DOMAIN_ORDER,'unresolved') if domain_reports.get('unresolved',{}).get('events') else DOMAIN_ORDER
            dump_json(args.output_dir/'domains/index.json',{
                'order':list(domain_order), 'time_unit':'us',
                'time_origin_us':report['time_origin_us'],
                'modules':[{'domain':d,'report':d+'/report.json',
                    'events':len(payload['events']),'groups':len(payload['groups'])}
                    for d,payload in [('PP',report)]+[(d,domain_reports[d]) for d in domain_order[1:]]]})
            unified_report=build_unified_report(report,domain_reports,streams,markers)
            unified_report['analysis_options']={'communication_only':args.communication_only}
            dump_json(args.output_dir/'unified_report.json',unified_report)
            dump_json(args.output_dir/'unified_summary.json',{
                'title':unified_report['title'],'stats':unified_report['stats'],
                'time_origin_us':unified_report['time_origin_us'],'rank_info':unified_report['rank_info'],
                'notes':unified_report['notes'],'sources':unified_report['sources']})
        if unified_report is not None:
            from refresh_domains import record_state
            record_state(args.output_dir)
        if not args.no_html:update_html(args.html,report,domain_reports,unified_report)
    except (ValueError,OSError,KeyError,TypeError,RuntimeError) as error:
        parser.exit(2,f'错误：{error}\n')
    print(json.dumps({'html':str(args.html.absolute()),'groups':len(report['groups']),'events':len(report['events']),
        'domains':['PP']+[d for d in DOMAIN_ORDER[1:] if d in domain_reports],
        'unified_event_count':len(unified_report['events']) if unified_report is not None else None,
        'html_bytes':args.html.stat().st_size if args.html.exists() else None,
        'warnings':report['warnings'],**report['stats'],
        'total_elapsed_seconds':round(time.perf_counter()-started,6)},ensure_ascii=False))
    return 0


if __name__=='__main__':sys.exit(main())
