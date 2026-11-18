"""Check every details reference against original kernel/communication exports."""
import argparse,csv,json
from collections import defaultdict
from pathlib import Path
import ijson
from operator_details import SHAPE_FIELDS,decimal,json_numbers,number_fields,step_id
from workflow_support import load_json,write_json
from operator_performance import collect_hardware,flops_for_record


def validate(output_dir):
    out=Path(output_dir);details=load_json(out/'operator_details.json');pp=load_json(out/'report.json');refs=details['compute_event_refs'];records=details['compute_records'];errors=[];counts=defaultdict(int);csv_rows={};comms={};matrices={}
    wanted=defaultdict(set)
    for identity,ref in refs.items():wanted[int(identity.split(':')[1])].add(ref[1])
    for source in pp['sources']:
        rank=int(source['rank']);folder=Path(source['trace']).parent;path=folder/'kernel_details.csv'
        if path.exists():
            with path.open(encoding='utf-8-sig',newline='') as handle:
                csv_rows[rank]={line:row for line,row in enumerate(csv.DictReader(handle),2) if line in wanted[rank]}
        comms[rank]=json_numbers(folder/'communication.json') if (folder/'communication.json').exists() else {}
        matrices[rank]=json_numbers(folder/'communication_matrix.json') if (folder/'communication_matrix.json').exists() else {}
    def fail(message):
        if len(errors)<100:errors.append(message)
    if details.get('hardware')!=collect_hardware(pp):fail('hardware peak differs from original per-rank device info')
    for index,record in enumerate(records):
        if record.get('performance')!=flops_for_record(record):fail(f'compute record {index}: FLOPs formula mismatch')
    with (out/'unified_report.json').open('rb') as handle:
        for event in ijson.items(handle,'events.item'):
            identity=event['event_id'];rank=int(event['rank']);step=str(event['step'])
            if event['kind']=='compute':
                counts['compute_events']+=1
                if identity not in refs:counts['compute_without_unique_csv']+=1;continue
                index,line=refs[identity];row=csv_rows.get(rank,{}).get(line)
                if row is None or not 0<=index<len(records):fail(f'{identity}: missing CSV line/reference');continue
                counts['compute_csv_references']+=1
                if any(records[index].get(field,'')!=row.get(field,'') for field in SHAPE_FIELDS):fail(f'{identity}: I/O differs from CSV')
                if not (row.get('Name',row.get('Op Name',''))==event['name'] or row.get('Name',row.get('Op Name','')).endswith('/'+event['name'])):fail(f'{identity}: kernel name mismatch')
                if decimal(row['Start Time(us)'])!=decimal(event['absolute_start_us']):fail(f'{identity}: exact start mismatch')
                if abs(decimal(row['Duration(us)'])-decimal(event['duration_us']))>decimal('.001'):fail(f'{identity}: duration exceeds 1ns export precision')
                args=event.get('args') or {}
                if row.get('Stream ID') and row['Stream ID']!=str(args.get('Physic Stream Id',event.get('tid',''))):fail(f'{identity}: stream mismatch')
                if args.get('Task Id') is not None and row.get('Task ID') and row['Task ID']!=str(args['Task Id']):fail(f'{identity}: task mismatch')
            elif event['kind']=='communication':
                counts['communication_events']+=1;detail=details['communication'].get(identity)
                if not detail:fail(f'{identity}: missing communication entry');continue
                if detail['status']!='matched':counts['communication_without_unique_export']+=1;continue
                candidates=[raw for stepkey,sections in comms[rank].items() if step_id(stepkey)==step for ops in sections.values() for key,raw in ops.items() if key==detail['record_key']]
                if len(candidates)!=1:fail(f'{identity}: communication source not unique');continue
                raw=candidates[0];counts['communication_export_references']+=1
                time=raw.get('Communication Time Info',{})
                if detail['record_key'].rpartition('@')[0]!=event['name'] or abs(decimal(time.get('Start Timestamp(us)'))-decimal(event['absolute_start_us']))>decimal('.5') or abs(decimal(time.get('Elapse Time(ms)'))*1000-decimal(event['duration_us']))>decimal('.001'):fail(f'{identity}: communication association mismatch')
                if detail['time']!=number_fields(raw.get('Communication Time Info',{})):fail(f'{identity}: communication time differs from source')
                for transport in detail['transports']:
                    expected=raw.get('Communication Bandwidth Info',{}).get(transport['transport'],{})
                    if {k:v for k,v in transport.items() if k!='transport'}!=number_fields(expected):fail(f'{identity}: bandwidth differs from source')
                    counts['transport_records']+=1
                for link in detail['links']:
                    pair=f'{link["raw_source_rank"]}-{link["raw_target_rank"]}'
                    candidates=[entry for key,sections in matrices[rank].items() if step_id(key)==step for ops in sections.values() for sample,links in ops.items() if sample==link['sample'] for endpoint,entry in links.items() if endpoint==pair]
                    if len(candidates)!=1 or number_fields(candidates[0])!=link['raw']:fail(f'{identity}: matrix sample differs from source')
                    if link['raw'].get('op_name',link['raw'].get('Op Name'))!=event['name']:fail(f'{identity}: matrix sample belongs to another op')
                    if link['sample'].rpartition('@')[2]!=detail['record_key'].rpartition('@')[2]:fail(f'{identity}: matrix group suffix mismatch')
                    counts['matrix_link_records']+=1
                expected={(int(event['rank']),int(t['peer_rank'])) if t['direction']=='send' else (int(t['peer_rank']),int(event['rank'])) for t in event.get('transfers',[]) if t.get('direction') in ('send','recv') and t.get('peer_rank') is not None}
                for endpoint in detail['endpoints']:
                    if (endpoint['source_rank'],endpoint['target_rank']) not in expected:fail(f'{identity}: endpoint has no existing protocol proof')
                    counts['proven_endpoints']+=1
    result={'passed':not errors,'counts':dict(counts),'errors':errors};write_json(out/'operator_details_validation.json',result);return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args();result=validate(args.output_dir);print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result['passed'] else 2)
