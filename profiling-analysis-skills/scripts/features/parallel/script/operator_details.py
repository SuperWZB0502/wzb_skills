"""Source-backed compute I/O and communication details, independent of the UI.

Adapted field rules: msinsight KernelParse::ParseShapeInfoData,
CommunicationRapidSaxHandler and CommunicationMatrixRapidHandler.
Joins require exact device timestamps/name/available IDs; duration tolerance is
one CSV last decimal place (capped at 0.001us), accounting for exported
duration quantization. Communication
uses unique name+Step+elapsed time and a <=0.5us exported timestamp tolerance.
Matrix top/middle records are sampled links, never full per-operation matrices.
"""
from collections import Counter, defaultdict
from decimal import Decimal as D, InvalidOperation
import csv
import hashlib
import json
from pathlib import Path
import re
import ijson
from workflow_support import load_json, write_json, file_state, digest, lock_file
from operator_performance import flops_for_record, collect_hardware, hardware_files

VERSION = 5
SHAPE_FIELDS = ('Input Shapes','Input Data Types','Input Formats','Output Shapes','Output Data Types','Output Formats')


def decimal(value):
    try:
        number=D(str(value).strip())
        return number if number.is_finite() else None
    except (ValueError,InvalidOperation,TypeError):return None


def step_id(value):
    return re.sub(r'^step','',str(value),flags=re.I)


def json_numbers(path):
    with Path(path).open() as handle:return json.load(handle,parse_float=D)


def number_fields(value):
    return {key:str(val) if isinstance(val,D) else val for key,val in value.items() if not isinstance(val,(dict,list))}


def global_link(a,b,group,rank):
    members=list(map(int,group.get('members',[])));possibilities=set()
    if a in members and b in members and rank in (a,b):possibilities.add((a,b))
    if 0<=a<len(members) and 0<=b<len(members) and rank in (members[a],members[b]):possibilities.add((members[a],members[b]))
    if len(possibilities)==1:
        source,target=next(iter(possibilities))
        return {'source_rank':source,'target_rank':target,'rank_basis':'组成员顺序及本卡端点校验；全局/组内解释唯一'}
    return {'source_rank':None,'target_rank':None,'rank_basis':'全局/组内rank解释不唯一，保留原始端点'}


def build_details(report,events):
    records=[];record_ids={};refs={};communications={};sources=[];counts=Counter();kernel_index={};comm_index=defaultdict(list);matrix_index=defaultdict(list);rank_reasons={};groups={}
    for group in report.get('groups',[]):groups[str(group['group_id'])]=group
    # Metadata supplies full group membership even when only some ranks were sampled.
    for source in report.get('sources',[]):
        if 'rank' not in source:continue
        rank=int(source['rank']);folder=Path(source['trace']).parent
        if source.get('metadata') and Path(source['metadata']).is_file():
            metadata=load_json(source['metadata'])
            for identity,value in metadata.get('parallel_group_info',{}).items():
                groups.setdefault(str(identity),{'members':value.get('global_ranks',[])})
        path=folder/'kernel_details.csv';index=defaultdict(list)
        if path.is_file():
            sources.append({'rank':rank,'kind':'compute_csv',**file_state(path)})
            with path.open(encoding='utf-8-sig',newline='') as handle:
                reader=csv.DictReader(handle)
                for line,row in enumerate(reader,2):
                    start,duration=decimal(row.get('Start Time(us)')),decimal(row.get('Duration(us)'))
                    if start is None or duration is None:continue
                    detail={key:row.get(key,'') for key in SHAPE_FIELDS};detail['type']=row.get('Type',row.get('Op Type',''))
                    detail['accelerator_core']=row.get('Accelerator Core','')
                    exported=decimal(row.get('FLOPs',row.get('flops')))
                    if exported is not None and exported>=0:detail['exported_flops']=str(exported)
                    detail['performance']=flops_for_record(detail)
                    signature=json.dumps(detail,ensure_ascii=False,sort_keys=True,separators=(',',':'))
                    if signature not in record_ids:record_ids[signature]=len(records);records.append(detail)
                    tolerance=min(D('.001'),D(10)**duration.as_tuple().exponent)
                    index[start].append((row.get('Name',row.get('Op Name','')),str(row.get('Stream ID','')).strip(),str(row.get('Task ID','')).strip(),record_ids[signature],line,step_id(row.get('Step ID','')),duration,tolerance))
            rank_reasons[str(rank)]='CSV行与设备事件未能唯一精确关联'
        else:rank_reasons[str(rank)]='采集中没有kernel_details.csv'
        kernel_index[rank]=index
        path=folder/'communication.json'
        if path.is_file():
            sources.append({'rank':rank,'kind':'communication_json',**file_state(path)})
            for step,sections in json_numbers(path).items():
                for section,ops in sections.items():
                    for key,detail in ops.items():
                        name,sep,group_hash=key.rpartition('@')
                        if not sep:name=key;group_hash=''
                        if name=='Total Op Info' or not isinstance(detail,dict):continue
                        comm_index[(rank,step_id(step),name)].append((key,group_hash,detail))
        path=folder/'communication_matrix.json'
        if path.is_file():
            sources.append({'rank':rank,'kind':'communication_matrix_json',**file_state(path)})
            for step,sections in json_numbers(path).items():
                for section,ops in sections.items():
                    for key,links in ops.items():
                        base,sep,group_hash=key.rpartition('@')
                        if not sep:base=key;group_hash=''
                        if 'total' in base.lower() or not isinstance(links,dict):continue
                        for endpoints,detail in links.items():
                            name=detail.get('op_name',detail.get('Op Name'))
                            match=re.fullmatch(r'(\d+)-(\d+)',str(endpoints))
                            if not name or not match:continue
                            matrix_index[(rank,step_id(step),str(name),group_hash)].append({'raw_source_rank':int(match[1]),'raw_target_rank':int(match[2]),'sample':key,'raw':number_fields(detail)})
    for event in events:
        kind=event.get('kind');rank=int(event['rank']);identity=str(event['event_id']);step=str(event['step'])
        if kind=='compute':
            counts['compute_total']+=1;args=event.get('args',{});start=decimal(event.get('absolute_start_us'));duration=decimal(event.get('duration_us'))
            candidates=kernel_index.get(rank,{}).get(start,[])
            stream=str(args.get('Physic Stream Id',event.get('tid','')));task=args.get('Task Id')
            found=[row for row in candidates if (row[0]==event['name'] or row[0].endswith('/'+event['name'])) and (not row[1] or row[1]==stream) and (task is None or not row[2] or row[2]==str(task)) and (not row[5] or row[5]==step) and duration is not None and abs(row[6]-duration)<=row[7]]
            if len(found)==1:
                row=found[0];refs[identity]=[row[3],row[4]];counts['compute_matched']+=1
                counts['compute_with_flops' if records[row[3]]['performance']['flops'] is not None else 'compute_flops_unknown']+=1
                if any(records[row[3]].get(key) not in ('','N/A','NA','None') for key in SHAPE_FIELDS):counts['compute_with_io']+=1
            else:counts['compute_unmatched']+=1
        elif kind=='communication':
            counts['communication_total']+=1;detail={'source_rank':rank,'status':'unavailable','record_key':None,'time':{},'transports':[],'links':[],'endpoints':[]}
            candidates=[]
            for key,group_hash,raw in comm_index.get((rank,step,event['name']),[]):
                time=raw.get('Communication Time Info',{});stamp=decimal(time.get('Start Timestamp(us)'));elapsed=decimal(time.get('Elapse Time(ms)'))
                if stamp is not None and elapsed is not None and abs(stamp-D(str(event['absolute_start_us'])))<=D('.5') and abs(elapsed*1000-D(str(event['duration_us'])))<=D('.001'):candidates.append((key,group_hash,raw))
            group=groups.get(str(event.get('group_id')),{'members':[]})
            if len(candidates)==1:
                key,group_hash,raw=candidates[0];detail.update(status='matched',record_key=key,time=number_fields(raw.get('Communication Time Info',{})))
                detail['bandwidth_observed']=any(decimal(metrics.get(field)) is not None for metrics in raw.get('Communication Bandwidth Info',{}).values() if isinstance(metrics,dict) for field in ('Transit Size(MB)','Transit Time(ms)','Bandwidth(GB/s)'))
                for transport,metrics in raw.get('Communication Bandwidth Info',{}).items():
                    if not isinstance(metrics,dict):continue
                    if any((decimal(metrics.get(field)) or 0)>0 for field in ('Transit Size(MB)','Transit Time(ms)','Bandwidth(GB/s)')):detail['transports'].append({'transport':transport,**number_fields(metrics)})
                for link in matrix_index.get((rank,step,event['name'],group_hash),[]):
                    # Raw matrix top/middle entries refer to named individual ops,
                    # but contain only a sample of their links. Preserve that scope.
                    mapped=global_link(link['raw_source_rank'],link['raw_target_rank'],group,rank)
                    detail['links'].append({**link,**mapped,'scope':'本次算子在top/middle/bottom矩阵中有明确op_name的部分链路，非完整矩阵'})
                counts['communication_matched']+=1
                if detail['transports']:counts['communication_with_bandwidth']+=1
                if detail['links']:counts['communication_with_matrix_links']+=1
            else:
                detail['reason']='communication.json缺失，或算子名/Step/起止持续时间不能唯一关联'
                counts['communication_unmatched']+=1
            for transfer in event.get('transfers',[]):
                if transfer.get('direction') not in ('send','recv') or transfer.get('peer_rank') is None:continue
                peer=int(transfer['peer_rank']);source,target=(rank,peer) if transfer['direction']=='send' else (peer,rank)
                detail['endpoints'].append({'source_rank':source,'target_rank':target,'payload_bytes':transfer.get('payload_bytes'),'basis':transfer.get('evidence'),'evidence_rank':transfer.get('source_rank')})
            if detail['endpoints']:counts['communication_with_proven_endpoints']+=1
            detail['members']=list(group.get('members',[]));communications[identity]=detail
    return {'schema_version':VERSION,'compute_event_refs':refs,'compute_records':records,'communication':communications,
            'hardware':collect_hardware(report),
            'sources':sources,'compute_unavailable_reasons':rank_reasons,'stats':dict(counts),
            'notes':['计算输入/输出为profiling记录的shape/dtype/format元数据；未采集实际Tensor值。',
                     '计算关联按rank、精确绝对时间、名称及可用Stream/Task字段唯一匹配，持续时间只允许CSV末位量化误差（最多0.001us，即1ns），不用最近时间。',
                     '通信时间与带宽保留原始导出字段、单位及传输类型；不把包含等待的算子时长当成链路传输时间。',
                     '通信矩阵仅接受同Step、同算子op_name、同communication.json组后缀的样本，不外推到其他算子。',
                     '集合通信不虚构单一发送/接收端；未确认全局rank时保留组内原始字段。']}


def encode_details(data, embedded_refs=False):
    from timeline_payload import _pack
    if embedded_refs:
        data = {key:value for key,value in data.items() if key != 'compute_event_refs'}
        data['compute_refs_embedded'] = True
    return json.dumps(_pack(data),ensure_ascii=False,separators=(',',':'))


def collect_details(output_dir):
    out=Path(output_dir);report=load_json(out/'report.json');paths=[out/'unified_report.json',out/'report.json',Path(__file__),Path(__file__).with_name('operator_performance.py'),*hardware_files(report)]
    for source in report.get('sources',[]):
        folder=Path(source['trace']).parent
        paths.extend(folder/name for name in ('kernel_details.csv','communication.json','communication_matrix.json'))
        if source.get('metadata'):paths.append(Path(source['metadata']))
    states=[file_state(path) for path in paths];key=digest({'version':VERSION,'sources':states})
    meta=out/'operator_details.meta.json';payload=out/'operator_details_payload.json';result=out/'operator_details.json'
    with lock_file(out/'.operator_details.lock'):
        if meta.exists() and result.exists() and payload.exists():
            saved=load_json(meta)
            if saved.get('key')==key and saved.get('result_state')==file_state(result) and saved.get('payload_state')==file_state(payload):return payload.read_text(),dict(saved['stats'],cache_hit=True)
        with (out/'unified_report.json').open('rb') as handle:data=build_details(report,ijson.items(handle,'events.item'))
        if states!=[file_state(path) for path in paths]:raise ValueError('详情来源在解析期间改变，请重试')
        write_json(result,data);encoded=encode_details(data);payload.write_text(encoded)
        write_json(meta,{'key':key,'input_states':states,'result_state':file_state(result),'payload_state':file_state(payload),'stats':data['stats']})
    return encoded,dict(data['stats'],cache_hit=False)


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    _,stats=collect_details(args.output_dir);print(json.dumps(stats,ensure_ascii=False))
