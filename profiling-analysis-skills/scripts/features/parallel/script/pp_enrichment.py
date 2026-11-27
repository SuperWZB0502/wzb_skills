"""Evidence-based peer directions and micro forward/backward compute overlays.

Raw compute evidence is intersected with framework phases and excludes the
selected group communication. Each phase also has one contiguous display block
for the lightweight scheduling diagram. Micro order is inferred only for noninterleaved balanced steps.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

D=Decimal
GROUP=re.compile(r'^(?:\d+:)?Group (.+) Communication$')
PHASE=re.compile(r'(?:^|/)pipeline_parallel/schedules\.py\(\d+\):(forward_step|backward_step)$')
PROFILE_PHASE={'hy106.phase.fwd':'f','hy106.phase.bwd':'b'}
INVALID_RANK=2**32-1
COLLECTIVE=re.compile(r'(?:allreduce|allgather|reducescatter|alltoall|broadcast|barrier|gather|scatter)$',re.I)


def merge(ranges):
    result=[]
    for begin,end in sorted((D(str(a)),D(str(b))) for a,b in ranges):
        if end<=begin:continue
        if result and begin<=result[-1][1]:result[-1][1]=max(result[-1][1],end)
        else:result.append([begin,end])
    return result


def intersection(ranges,begin,end):
    return [[max(a,begin),min(b,end)] for a,b in ranges if b>begin and a<end]


def subtract(ranges,excluded):
    result=[]
    for begin,end in ranges:
        cursor=begin
        for left,right in excluded:
            if right<=cursor:continue
            if left>=end:break
            if left>cursor:result.append([cursor,min(left,end)])
            cursor=max(cursor,right)
            if cursor>=end:break
        if cursor<end:result.append([cursor,end])
    return result


def step_for(begin,steps):
    hits={s['step'] for s in steps if D(s['absolute_start_us'])<=begin<D(s['absolute_start_us'])+D(s['duration_us'])}
    return next(iter(hits)) if len(hits)==1 else 'unassigned'


def _time(event):
    try:
        begin=D(str(event['ts']));duration=D(str(event['dur']))
        if not begin.is_finite() or not duration.is_finite() or begin<0 or duration<0:return None
        return begin,begin+duration
    except (KeyError,InvalidOperation,ValueError):return None


class Collector:
    def __init__(self):
        self.processes={};self.threads={};self.sort_order={};self.order={}
        self.phase_candidates=[];self.scheduler=[];self.compute=[];self.tasks=[];self.protocol_tasks=[]

    def feed(self,event,ordinal):
        ph=event.get('ph');name=str(event.get('name',''));pid=str(event.get('pid',''));tid=str(event.get('tid',''))
        if ph=='M':
            args=event.get('args') or {}
            if name=='process_name':self.processes[pid]=str(args.get('name','')).lower()
            elif name=='thread_name':
                self.threads[(pid,tid)]=str(args.get('name',''));self.order[(pid,tid)]=ordinal
            elif name=='thread_sort_index':
                try:self.sort_order[(pid,tid)]=int(args['sort_index'])
                except (KeyError,TypeError,ValueError):pass
            return
        if ph!='X':return
        phase=PHASE.search(name)
        profile_phase=PROFILE_PHASE.get(name) if event.get('cat')=='cpu_op' else None
        if phase or profile_phase:
            span=_time(event)
            if span:
                args=event.get('args') or {};micro=None
                for key,value in args.items():
                    if re.sub('[^a-z0-9]','',str(key).lower()) in {'microbatchid','microbatch','microbatchindex','microid'}:
                        try:micro=int(value)
                        except (ValueError,TypeError):pass
                kind=profile_phase or ('f' if phase.group(1)=='forward_step' else 'b')
                self.phase_candidates.append({'kind':kind,
                    'absolute_start_us':str(span[0]),'duration_us':str(span[1]-span[0]),'source_name':name,
                    'source_ordinal':ordinal,'explicit_micro':micro})
        elif 'forward_backward_pipelining_without_interleaving' in name:
            span=_time(event)
            if span:self.scheduler.append(span)
        elif name=='Computing':
            span=_time(event)
            if span:self.compute.append((pid,span))
        args=event.get('args')
        if not args:return
        # Most CPU records have no peer fields; avoid normalizing every large args map.
        if not any('rank' in str(key).lower() for key in args):return
        values={re.sub('[^a-z0-9]','',str(k).lower()):v for k,v in args.items()}
        src=values.get('srcrank',values.get('sourcerank'));dst=values.get('dstrank',values.get('destinationrank'))
        size=values.get('sizebyte',values.get('transitsize',values.get('size',0)))
        try:src=int(src);dst=int(dst);size=int(size)
        except (TypeError,ValueError,OverflowError):return
        span=_time(event)
        if span:
            task={'pid':pid,'tid':tid,'start_us':str(span[0]),'end_us':str(span[1]),
                  'src_rank':src,'dst_rank':dst,'bytes':size,'name':name,'source_ordinal':ordinal,
                  'transport_type':str(values.get('transporttype','')),'rdma_type':str(values.get('rdmatype','')),
                  'link_type':str(values.get('linktype',''))}
            self.protocol_tasks.append(task)
            if size>0 and 0<=src<INVALID_RANK and 0<=dst<INVALID_RANK and src!=dst:self.tasks.append(task)

    def finish(self,steps,allowed_groups):
        planes={}
        for pid in set(p for p,_ in self.threads):
            if self.processes.get(pid) not in {'communication','hccl'}:continue
            entries=[(key,name) for key,name in self.threads.items() if key[0]==pid]
            entries.sort(key=lambda pair:self.sort_order.get(pair[0],int(pair[0][1]) if pair[0][1].lstrip('-').isdigit() else self.order[pair[0]]))
            parent=None
            for key,name in entries:
                match=GROUP.fullmatch(name)
                if match:parent=match.group(1)
                elif name.startswith('Plane') and parent in allowed_groups:planes[key]=parent
                else:parent=None
        tasks=[]
        for task in self.tasks:
            group=planes.get((task['pid'],task['tid']))
            if group:
                item=dict(task);item['group_id']=group;tasks.append(item)
        protocol=[]
        for task in self.protocol_tasks:
            group=planes.get((task['pid'],task['tid']))
            if group:protocol.append({**task,'group_id':group})
        phases=[]
        seen=set()
        for raw in sorted(self.phase_candidates,key=lambda p:D(p['absolute_start_us'])):
            identity=(raw['kind'],raw['absolute_start_us'],raw['duration_us'])
            if identity in seen:continue
            seen.add(identity);item=dict(raw);item['step']=step_for(D(item['absolute_start_us']),steps);phases.append(item)
        computing=merge([span for pid,span in self.compute if self.processes.get(pid)=='overlap analysis'])
        return {'phases':phases,'noninterleaved_steps':sorted({step_for(a,steps) for a,b in self.scheduler}),
                'compute_ranges':[[str(a),str(b)] for a,b in computing],'peer_tasks':tasks,'protocol_tasks':protocol,
                'basis':'trace Python scheduler phases + Overlap Analysis Computing + Plane src/dst'}


def resolve_peer(rank,src,dst,members):
    try:src=int(src);dst=int(dst)
    except (TypeError,ValueError):return None
    if min(src,dst)<0 or max(src,dst)>=INVALID_RANK or src==dst:return None
    candidates=set()
    if src in members and dst in members:candidates.add((src,dst))
    if 0<=src<len(members) and 0<=dst<len(members):candidates.add((members[src],members[dst]))
    possibilities=set()
    for source,target in candidates:
        if source==rank and target!=rank:possibilities.add(('send',target))
        elif target==rank and source!=rank:possibilities.add(('recv',source))
    if len(possibilities)!=1:return None
    direction,peer=next(iter(possibilities))
    return {'direction':direction,'peer_rank':peer,'peer_stage':members.index(peer)}


def payload_tasks(tasks):
    """Distinguish legacy RDMA data/notify/wait sequences; never use a size threshold.

    Sender: RDMASend payload(s), RDMASend notify, Notify_Wait.
    Receiver handshake sends separated by Notify_Wait are not data payloads.
    Explicit RDMA_SEND_PAYLOAD is authoritative when available.
    """
    streams=defaultdict(list);result=[]
    for task in tasks:streams[(task.get('group_id'),task.get('pid'),task.get('tid'))].append(task)
    for stream in streams.values():
        stream.sort(key=lambda t:(D(t['start_us']),t.get('source_ordinal',0)))
        i=0
        while i<len(stream):
            task=stream[i];name=task['name'].lower();kind=task.get('rdma_type','').upper()
            if kind=='RDMA_SEND_PAYLOAD' or name in {'rdmasendpayload','rdmarecvpayload'}:
                result.append(task);i+=1;continue
            if name=='rdmasend' and not kind:
                end=i
                while end<len(stream) and stream[end]['name'].lower()=='rdmasend' and not stream[end].get('rdma_type'):end+=1
                if end-i>=2 and end<len(stream) and stream[end]['name'].lower() in {'notify_wait','notifywait'}:
                    result.extend(stream[i:end-1])
                i=end;continue
            if name in {'memcpy','reduce_inline','rdmarecv'} and task.get('bytes',0)>0:
                result.append(task)
            i+=1
    return result


def receive_waits(tasks):
    """Legacy receive handshake: send ready, wait data, send ack, copy payload."""
    streams=defaultdict(list);result=[]
    for task in tasks:streams[(task.get('group_id'),task.get('pid'),task.get('tid'))].append(task)
    for stream in streams.values():
        stream.sort(key=lambda t:(D(t['start_us']),t.get('source_ordinal',0)))
        for i in range(1,len(stream)-2):
            if [t['name'].lower() for t in stream[i-1:i+3]]==['rdmasend','notify_wait','rdmasend','memcpy']:
                result.append({**stream[i],'receive_bytes':stream[i+2]['bytes']})
    return result


def add_transfers(report,cache):
    evidence=defaultdict(dict)
    def add(event,peer,task,source_rank,source_event,wait=None):
        key=(peer['direction'],peer['peer_rank'])
        entry=evidence[event['event_id']].setdefault(key,{**peer,
            'evidence':('RDMA载荷协议序列' if task['name'].lower().startswith('rdma') else 'Plane数据搬运任务') if wait is None else '对端RDMA载荷 + 本端接收协议/字节数/通信区间交叉核对',
            'task_count':0,'source_rank':source_rank,'source_event_id':source_event['event_id'],
            'source_ordinals':[],'raw_peer_fields':[],'payload_bytes':0,'matched_waits':[]})
        if task.get('source_ordinal') in entry['source_ordinals']:return
        entry['task_count']+=1;entry['source_ordinals'].append(task.get('source_ordinal'))
        entry['payload_bytes']+=task['bytes']
        fields={'src_rank':task['src_rank'],'dst_rank':task['dst_rank']}
        if fields not in entry['raw_peer_fields']:entry['raw_peer_fields'].append(fields)
        if wait:entry['matched_waits'].append({'rank':event['rank'],'source_ordinal':wait.get('source_ordinal'),
            'start_us':wait['start_us'],'end_us':wait['end_us'],'receive_bytes':wait['receive_bytes']})
    def contains(event,task):
        return event['pid']==task['pid'] and D(event['absolute_start_us'])<=D(task['start_us']) and D(task['end_us'])<=D(event['absolute_start_us'])+D(event['duration_us'])+D('.001')
    receiver_waits={rank:receive_waits(raw.get('protocol_tasks',[])) for rank,raw in cache.items()}
    for group in report['groups']:
        members=group['members'];by_rank={rank:[e for e in report['events'] if e['rank']==rank and e['group_id']==group['group_id']] for rank in cache}
        for rank,raw in cache.items():
            protocol=[t for t in raw.get('protocol_tasks',raw.get('peer_tasks',[])) if t['group_id']==group['group_id']]
            for task in payload_tasks(protocol):
                if task['bytes']<=0:continue
                peer=resolve_peer(rank,task['src_rank'],task['dst_rank'],members)
                if peer is None:continue
                matches=[e for e in by_rank.get(rank,[]) if not e.get('transfer_note') and contains(e,task)]
                if len(matches)!=1:continue
                source_op=matches[0]
                add(source_op,peer,task,rank,source_op)
                if peer['direction']!='send' or not task['name'].lower().startswith('rdma'):continue
                remote=peer['peer_rank'];receive_candidates=[]
                for target in by_rank.get(remote,[]):
                    if target.get('transfer_note') or target['step']!=source_op['step']:continue
                    waits=[]
                    for wait in receiver_waits.get(remote,[]):
                        if wait['group_id']!=group['group_id'] or wait['name'].lower() not in {'notify_wait','notifywait'}:continue
                        who=resolve_peer(remote,wait['src_rank'],wait['dst_rank'],members)
                        if who is None or who['peer_rank']!=rank or not contains(target,wait):continue
                        # Endpoint clocks can differ by hundreds of us. Correlate
                        # complete source op with the receive wait, not tiny launch timestamps.
                        source_begin=D(source_op['absolute_start_us']);source_end=source_begin+D(source_op['duration_us'])
                        if wait['receive_bytes']==task['bytes'] and source_begin<=D(wait['end_us']) and D(wait['start_us'])<=source_end:waits.append(wait)
                    if len(waits)==1:receive_candidates.append((target,waits[0]))
                if len(receive_candidates)==1:
                    target,wait=receive_candidates[0]
                    add(target,{'direction':'recv','peer_rank':rank,'peer_stage':members.index(rank)},task,rank,source_op,wait)
    for event in report['events']:event['transfers']=list(evidence[event['event_id']].values())


def add_profile_phases(report, cache, origin, regions, phase_stats):
    """An explicit whole-step pair is independent of PP groups and microbatch IDs."""
    for rank, raw in cache.items():
        by_step=defaultdict(list)
        for phase in raw.get('phases',[]):
            if phase['source_name'] in PROFILE_PHASE and phase['step'] in report['steps']:
                by_step[phase['step']].append(phase)
        for step, values in by_step.items():
            if len(values)!=2 or [p['kind'] for p in values]!=['f','b']:
                continue
            if any(r['rank']==rank and r['step']==step and r['phase'] in ('f','b') for r in regions):
                continue
            forward, backward=values
            if D(forward['absolute_start_us'])+D(forward['duration_us'])>D(backward['absolute_start_us']):
                continue
            phase_stats[f'{rank}:{step}']={'f':1,'b':1}
            compute=merge(raw.get('compute_ranges',[]))
            for phase in values:
                begin=D(phase['absolute_start_us']);end=begin+D(phase['duration_us'])
                kind=phase['kind']
                segments=intersection(compute,begin,end)
                regions.append({'region_id':f'profile-phase:{rank}:{step}:{kind}','group_id':None,
                    'rank':rank,'stage':None,'step':step,'micro':None,'phase':kind,'label':'1'+kind,
                    'start_us':str(begin-origin),'end_us':str(end-origin),
                    'display_start_us':str(begin-origin),'display_end_us':str(end-origin),
                    'display_basis':'显式采集区间',
                    'segments':[{'start_us':str(a-origin),'end_us':str(b-origin)} for a,b in segments],
                    'duration_us':str(sum((b-a for a,b in segments),D(0))),
                    'framework_start_us':str(begin-origin),'framework_end_us':str(end-origin),
                    'source_name':phase['source_name'],'source_ordinal':phase['source_ordinal'],
                    'basis':'整步内唯一显式前向、反向阶段；1 表示该阶段首次出现。'})


def enrich_report(report,ranks):
    origin=D(report['time_origin_us']);regions=[];warnings=[];phase_stats={}
    expected_mbn=report.get('micro_configuration',{}).get('expected_mbn')
    warnings.extend(report.get('micro_configuration',{}).get('warnings',[]))
    cache={row['rank']:row.get('enrichment',{}) for row in ranks}
    for event in report['events']:
        event['op_type']=event['name'].split('__',1)[0];event['transfers']=[]
        event['transfer_note']='集合通信：不按流水线点对点收发绘制箭头' if COLLECTIVE.search(event['op_type']) else ''
    add_transfers(report,cache)
    for group in report['groups']:
        members=group['members']
        for rank,raw in cache.items():
            ops=[e for e in report['events'] if e['rank']==rank and e['group_id']==group['group_id']]
            if group['kind']!='pp' or rank not in members:continue
            phases=raw.get('phases',[]);compute=merge(raw.get('compute_ranges',[]))
            stage=members.index(rank)
            masked=merge([[e['absolute_start_us'],str(D(e['absolute_start_us'])+D(e['duration_us']))] for e in ops])
            by_step=defaultdict(list)
            for phase in phases:by_step[phase['step']].append(phase)
            for step,values in by_step.items():
                if step not in report['steps']:continue
                values=sorted(values,key=lambda p:D(p['absolute_start_us']))
                counts={kind:sum(p['kind']==kind for p in values) for kind in ('f','b')}
                serial=step in raw.get('noninterleaved_steps',[]) and counts['f']==counts['b'] and counts['f']>0
                if expected_mbn is not None and (counts['f']!=expected_mbn or counts['b']!=expected_mbn):
                    serial=False
                    warnings.append(f'Rank {rank} Step {step} 实际框架标记为{counts["f"]}f/{counts["b"]}b，配置MBN={expected_mbn}，不按不完整调用顺序补micro编号')
                phase_stats[str(rank)+':'+step]=counts
                explicit=[p['explicit_micro'] for p in values if p.get('explicit_micro') is not None]
                offset=1 if explicit and min(explicit)==0 else 0
                ordinal={'f':0,'b':0}
                for phase in values:
                    kind=phase['kind'];ordinal[kind]+=1
                    micro=phase.get('explicit_micro')
                    if micro is not None:micro+=offset;basis='显式micro标记'
                    elif serial:micro=ordinal[kind];basis='非交错流水线F/B调用顺序'
                    else:
                        warnings.append(f'Rank {rank} Step {step} 的micro顺序缺少可验证依据，未补编号');continue
                    begin=D(phase['absolute_start_us']);end=begin+D(phase['duration_us'])
                    segments=subtract(intersection(compute,begin,end),masked)
                    if not segments:continue
                    white=subtract([[begin,end]],masked)
                    candidates=[]
                    for left,right in white:
                        actual=intersection(segments,left,right)
                        if actual:candidates.append((sum((b-a for a,b in actual),D(0)),actual[0][0],actual[-1][1]))
                    _,display_begin,display_end=max(candidates,key=lambda item:item[0])
                    relative=[{'start_us':str(a-origin),'end_us':str(b-origin)} for a,b in segments]
                    regions.append({'region_id':f'{group["group_id"]}:{rank}:{step}:{kind}:{micro}',
                        'group_id':group['group_id'],'rank':rank,'stage':stage,'step':step,'micro':micro,'phase':kind,'label':str(micro)+kind,
                        'start_us':str(segments[0][0]-origin),'end_us':str(segments[-1][1]-origin),
                        'display_start_us':str(display_begin-origin),'display_end_us':str(display_end-origin),
                        'display_basis':'通信扣除后的连续白区间中，实际计算覆盖量最大的一段；显示为单个调度示意块',
                        'segments':relative,'duration_us':str(sum((b-a for a,b in segments),D(0))),
                        'framework_start_us':str(begin-origin),'framework_end_us':str(end-origin),
                        'source_name':phase.get('source_name'),'source_ordinal':phase.get('source_ordinal'),
                        'basis':'device_compute ∩ framework_phase − selected_group_communication；'+basis})
    add_profile_phases(report,cache,origin,regions,phase_stats)
    report['compute_regions']=regions;report['enrichment_warnings']=sorted(set(warnings))
    report['enrichment_stats']={'compute_regions':len(regions),'compute_segments':sum(len(r['segments']) for r in regions),
       'events_with_directions':sum(bool(e['transfers']) for e in report['events']),
       'direction_edges':sum(len(e['transfers']) for e in report['events']),
       'collective_events_without_pipeline_arrows':sum(bool(e['transfer_note']) for e in report['events']),'phase_counts':phase_stats}
