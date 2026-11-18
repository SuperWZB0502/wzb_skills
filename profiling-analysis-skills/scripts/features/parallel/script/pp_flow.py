"""Map verified PP payloads to micro compute blocks and forward/backward paths.

Host scheduling can enter the next forward call before the preceding payload is
sent. Source attribution therefore uses a completed framework phase at the real
payload launch, plus the receiving operator's enclosing phase on its own rank.
"""
from collections import defaultdict
from decimal import Decimal

D=Decimal
EPS=D('.001')


def enrich_micro_flows(report,ranks):
    origin=D(report['time_origin_us']);regions=report.get('compute_regions',[])
    groups={g['group_id']:g for g in report['groups']}
    tasks={r['rank']:{t['source_ordinal']:t for t in r.get('enrichment',{}).get('protocol_tasks',[])} for r in ranks}
    index=defaultdict(list);receivers=defaultdict(list);proofs={};warnings=[]
    for r in regions:index[(r['group_id'],r['step'],r['rank'],r['phase'],r['micro'])].append(r)
    for event in report['events']:
        for edge in event.get('transfers',[]):
            for key in ['source_region_id','target_region_id','target_event_id','micro','phase','compute_evidence']:
                edge.pop(key,None)
            if edge['direction']=='recv':
                key=(edge['source_event_id'],edge['source_rank'],event['rank'],tuple(sorted(edge['source_ordinals'])))
                receivers[key].append((event,edge))
    for event in report['events']:
        group=groups[event['group_id']]
        if group['kind']!='pp' or event['rank'] not in group['members']:continue
        members=group['members'];stage=members.index(event['rank'])
        for edge in event.get('transfers',[]):
            if edge['direction']!='send' or edge['peer_rank'] not in members:continue
            target_stage=members.index(edge['peer_rank'])
            if abs(stage-target_stage)!=1:continue
            phase='f' if target_stage>stage else 'b'
            raw=[tasks.get(event['rank'],{}).get(i) for i in edge['source_ordinals']]
            if not raw or any(t is None for t in raw):continue
            source_ids=set();attributed=0
            for t in raw:
                launch=D(t['start_us'])-origin
                candidates=[r for r in regions if r['group_id']==event['group_id'] and r['step']==event['step'] and r['rank']==event['rank'] and r['phase']==phase and D(r['framework_end_us'])<=launch+EPS]
                if candidates:
                    latest=max(D(r['framework_end_us']) for r in candidates)
                    latest_regions=[r for r in candidates if D(r['framework_end_us'])==latest]
                    if len(latest_regions)==1:
                        source_ids.add(latest_regions[0]['region_id']);attributed+=1
            if len(source_ids)!=1 or attributed!=len(raw):continue
            source=next(r for r in regions if r['region_id'] in source_ids)
            targets=index[(event['group_id'],event['step'],edge['peer_rank'],phase,source['micro'])]
            receive_key=(event['event_id'],event['rank'],edge['peer_rank'],tuple(sorted(edge['source_ordinals'])))
            paired=receivers[receive_key]
            if len(targets)!=1 or len(paired)!=1:continue
            target=targets[0];recv,recv_edge=paired[0]
            # All comparisons here are within the receiver rank's own clock.
            if not (D(target['framework_start_us'])-EPS<=D(recv['start_us']) and D(recv['end_us'])<=D(target['framework_end_us'])+EPS and D(recv['end_us'])<=D(target['display_start_us'])+EPS):
                warnings.append(event['event_id']+' 的接收算子不属于对应micro阶段，未补计算连线');continue
            metadata={'source_region_id':source['region_id'],'target_region_id':target['region_id'],
                'target_event_id':recv['event_id'],'micro':source['micro'],'phase':phase,
                'compute_evidence':'已核对载荷/接收协议 + 载荷下发前已完成的源阶段 + 接收算子所在目标micro阶段'}
            edge.update(metadata);recv_edge.update(metadata)
            proofs[(source['region_id'],target['region_id'])]={
                'source_region_id':source['region_id'],'target_region_id':target['region_id'],'phase':phase,
                'source_event_id':event['event_id'],'target_event_id':recv['event_id'],'evidence':metadata['compute_evidence']}
    flows=[]
    for group in report['groups']:
        if group['kind']!='pp':continue
        group_regions=[r for r in regions if r['group_id']==group['group_id']]
        scopes=sorted({(r['step'],r['micro']) for r in group_regions})
        for step,micro in scopes:
            ordered=[];missing=[]
            for phase,members in [('f',group['members']),('b',list(reversed(group['members'])))]:
                for rank in members:
                    matches=index[(group['group_id'],step,rank,phase,micro)]
                    if len(matches)==1:ordered.append(matches[0])
                    else:missing.append({'rank':rank,'phase':phase})
            edges=[]
            for source,target in zip(ordered,ordered[1:]):
                proof=proofs.get((source['region_id'],target['region_id']))
                if proof:edges.append(proof)
                elif source['rank']==target['rank']==group['members'][-1] and source['phase']=='f' and target['phase']=='b' and D(source['framework_end_us'])<=D(target['framework_start_us'])+EPS:
                    edges.append({'source_region_id':source['region_id'],'target_region_id':target['region_id'],'phase':'turnaround','evidence':'末级同micro前向完成后进入反向'})
            expected=2*len(group['members'])
            flows.append({'flow_id':f'{group["group_id"]}:{step}:micro:{micro}',
                'group_id':group['group_id'],'step':step,'micro':micro,'nodes':[r['region_id'] for r in ordered],
                'edges':edges,'complete':not missing and len(ordered)==expected and len(edges)==expected-1,'missing_nodes':missing})
    report['micro_flows']=flows;report['flow_warnings']=sorted(set(warnings))
    report['flow_stats']={'micro_flows':len(flows),'complete_micro_flows':sum(f['complete'] for f in flows),
        'compute_links':sum(len(f['edges']) for f in flows),'transfers_with_compute_blocks':sum('source_region_id' in t for e in report['events'] for t in e.get('transfers',[]))}
