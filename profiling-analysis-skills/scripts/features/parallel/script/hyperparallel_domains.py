"""Read observed DeviceMesh layouts without assuming communicator creation order."""
import ast
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path
import re


MESH = re.compile(r"DeviceMesh\('[^']+',\s*(\[.*\]),\s*mesh_dim_names=(\([^)]*\))\)")
AXIS_DOMAINS = {'dp_shard':'DP','dp_replicate':'DP','fsdp_shard':'DP',
                'fsdp_replicate':'DP','ep':'EP','ep_fsdp':'DP-Modulo-EP',
                'tp':'TP','cp':'CP','pp':'PP'}
FSDP_SCOPE=('pre_forward unshard:','pre_backward unshard:','post_backward:','root_backward reduce:')
EXPERT_SCOPE='moonep::all_to_all_v::'
CLIP_SCOPE='hy106.phase.grad_clip'
SHARD_AXES={'fsdp':{'dp_shard','fsdp_shard'},
            'expert_exchange':{'ep'},'expert_fsdp':{'ep_fsdp'}}


def mesh_log_files(profiling, log_dir=None):
    folders=[Path(log_dir)] if log_dir else [Path(profiling)/'logs',Path(profiling).parent/'run_log']
    return sorted({file for folder in folders for file in folder.glob('*.log')})


def _coordinates(mesh, prefix=()):
    if isinstance(mesh,list):
        for i,value in enumerate(mesh):
            yield from _coordinates(value,prefix+(i,))
    elif type(mesh) is int and mesh>=0:
        yield prefix,mesh


def mesh_evidence(profiling, log_dir=None):
    groups=set()
    for path in mesh_log_files(profiling,log_dir):
        with path.open(encoding='utf-8',errors='replace') as stream:
            for line in stream:
                match=MESH.search(line)
                if not match:
                    continue
                try:
                    mesh, names=(ast.literal_eval(value) for value in match.groups())
                except (ValueError,SyntaxError):
                    continue
                coordinates=list(_coordinates(mesh))
                if not coordinates or not isinstance(names,tuple):
                    continue
                if any(len(point)!=len(names) for point,_ in coordinates):
                    continue
                if len({rank for _,rank in coordinates})!=len(coordinates):
                    continue
                for axis,name in enumerate(names):
                    if not isinstance(name,str) or name not in AXIS_DOMAINS:
                        continue
                    partitions=defaultdict(list)
                    for point,rank in coordinates:
                        partitions[point[:axis]+point[axis+1:]].append(rank)
                    for members in partitions.values():
                        if len(members)>1:
                            groups.add((name,tuple(sorted(members))))
    return sorted(groups)


def candidate_axes(meshes, observed_ranks, participants, members=()):
    """Rank count gives a candidate. Only an equal recorded member set corroborates it."""
    observed=set(observed_ranks)
    recorded=set(members)
    return sorted({(name,AXIS_DOMAINS[name]) for name, mesh in meshes
                   if len(mesh)==participants and observed.issubset(mesh)
                   and (not recorded or recorded==set(mesh))})


def single_pipeline_stage(profiling, log_dir=None):
    stages=set()
    for path in mesh_log_files(profiling,log_dir):
        with path.open(encoding='utf-8',errors='replace') as stream:
            for line in stream:
                if 'Initializing parallel state' in line:
                    stages.update(re.findall(r'\bpp_size\s*=?\s*(\d+)\b',line))
    return stages=={'1'}


def corroborated_axis(meshes, group, participants, usages):
    """Require a matching call-site purpose and a unique concrete mesh slice."""
    axes=set().union(*(SHARD_AXES.get(usage,set()) for usage in usages))
    observed=set(group['observed_on_ranks'])
    recorded=set(group['members'])
    matches={(axis,members) for axis,members in meshes
             if axis in axes and len(members)==participants and observed.issubset(members)
             and (not recorded or recorded==set(members))}
    if len(matches)!=1:
        return None
    axis,members=matches.pop()
    return axis,AXIS_DOMAINS[axis],list(members)


def exported_group_ids(events, path):
    """Join the trace lane to the exporter ID using exact step and event names."""
    if not path.is_file():
        return {}
    try:
        with path.open(encoding='utf-8-sig') as stream:
            record=json.load(stream)
    except (OSError,ValueError):
        return {}
    if not isinstance(record,dict):
        return {}
    names=defaultdict(set)
    for step,kinds in record.items():
        if not isinstance(kinds,dict):
            continue
        for kind in ('p2p','collective'):
            group=kinds.get(kind)
            if not isinstance(group,dict):
                continue
            for key in group:
                name,separator,identity=key.rpartition('@')
                if separator and identity:
                    names[(str(step).removeprefix('step'),name)].add(identity)
    matches=defaultdict(set)
    for event in events:
        matches[event['group_id']].update(names[(str(event.get('step')),event['name'])])
    return matches


def causal_group_usages(events, index, trace):
    """Correlate HCCL events with CPU annotations using recorded flow endpoints."""
    from timeline_streams import Node, TrackIndex, _d, _tick

    if not index.is_file() or not trace.is_file():
        return {}
    try:
        with gzip.open(index,'rt',encoding='utf-8') as stream:
            raw=json.load(stream)
        stat=trace.stat()
        if not any(Path(item['path']).resolve()==trace.resolve() and
                   item['size']==stat.st_size and item['mtime_ns']==stat.st_mtime_ns
                   for item in raw['fingerprint']):
            return {}
    except (OSError,ValueError,KeyError,TypeError):
        return {}

    processes=raw['processes']
    names=raw['names']
    launches=defaultdict(list)
    enqueues=defaultdict(list)
    queues={}
    scopes={}
    for track in raw['tracks']:
        pid,tid=track['pid'],track['tid']
        process=processes.get(pid,'').casefold()
        dequeues, annotations=[],[]
        for ordinal,begin,end,name_id,category,args in track['nodes']:
            name=names[name_id]
            args=args or {}
            if process=='cann' and name=='Node@launch' and 'connection_id' in args:
                launches[(pid,tid,begin,str(args['connection_id']))].append(end)
            if process!='python':
                continue
            if category=='enqueue' and 'correlation_id' in args:
                enqueues[(pid,tid,begin,str(args['correlation_id']))].append((name,end))
            elif category=='dequeue' and 'correlation_id' in args:
                dequeues.append(Node(ordinal,pid,tid,begin,end,name,category,args))
            elif category=='cpu_op' and (name.startswith((*FSDP_SCOPE,EXPERT_SCOPE)) or name==CLIP_SCOPE):
                annotations.append(Node(ordinal,pid,tid,begin,end,name,category,None))
        if dequeues:queues[(pid,tid)]=TrackIndex(dequeues)
        if annotations:scopes[(pid,tid)]=TrackIndex(annotations)

    device_flows=defaultdict(list)
    async_flows=defaultdict(list)
    for flow in raw['flows']:
        if flow['category'] not in ('HostToDevice','async_task_queue'):
            continue
        sources=[point for point in flow['points'] if point[0]=='s']
        targets=[point for point in flow['points'] if point[0]=='f']
        if len(sources)!=1 or len(targets)!=1:continue
        source,target=sources[0],targets[0]
        endpoint=tuple(target[1:4])
        if flow['category']=='HostToDevice':device_flows[endpoint].append(source)
        else:async_flows[(*endpoint,str(flow['id']))].append(source)

    origin=_d(raw['origin'])
    usages=defaultdict(Counter)
    for event in events:
        connection=event.get('args',{}).get('connection_id')
        tick=_tick(event['absolute_start_us'],origin)
        matched=device_flows[(str(event['pid']),str(event['tid']),tick)]
        if len(matched)!=1 or connection is None:continue
        pid,tid,begin=matched[0][1:4]
        ends=launches[(pid,tid,begin,str(connection))]
        if len(ends)!=1:continue
        dequeue=[node for (cpu,thread),track in queues.items() if thread==tid
                 for node in track.contains(begin,ends[0])]
        if len(dequeue)!=1:continue
        node=dequeue[0]
        correlation=str(node.args['correlation_id'])
        matched=async_flows[(node.pid,node.tid,node.begin,correlation)]
        if len(matched)!=1:continue
        cpu,thread,enqueue_start=matched[0][1:4]
        enqueued=enqueues[(cpu,thread,enqueue_start,correlation)]
        enclosing=scopes.get((cpu,thread))
        if len(enqueued)!=1 or enclosing is None:continue
        name,enqueue_end=enqueued[0]
        collective=node.name.removeprefix('Dequeue@').casefold()
        if name.removeprefix('Enqueue@').casefold()!=collective:continue
        parents=enclosing.contains(enqueue_start,enqueue_end)
        usage=None
        if collective=='hcclallreduce' and any(parent.name==CLIP_SCOPE for parent in parents):
            usage='gradient_clip'
        elif collective=='hcclalltoallv' and any(parent.name.startswith(EXPERT_SCOPE) for parent in parents):
            usage='expert_exchange'
        elif collective in ('hcclallgather','hcclreducescatter'):
            fsdp=[p for p in parents if p.name.startswith(FSDP_SCOPE)]
            if fsdp:
                owner=min(fsdp,key=lambda p:p.end-p.begin)
                usage='expert_fsdp' if '.experts' in owner.name else 'fsdp'
        if usage:usages[event['group_id']][usage]+=1
    return usages
