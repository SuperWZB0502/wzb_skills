"""Read lightweight, sourced configuration for the timeline's information row.

Independent of feature 1: metadata, Megatron log arguments and per-rank Step CSV
are the evidence. This is presentation data, so changing colors never scans traces.
"""
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from timeline_colors import color_config
from timeline_training import micro_configuration

ORDER=('PP','DP','EP','CP','TP')
FIELDS={'PP':'pipeline_model_parallel_size','DP':'data_parallel_size',
        'EP':'expert_model_parallel_size','CP':'context_parallel_size','TP':'tensor_model_parallel_size',
        'total_ranks':'world_size'}


def collect_summary(report, log_dir=None):
    rows=[];values={key:set() for key in FIELDS};sources=[];warnings=[];step_times={}
    for source in report.get('sources',[]):
        if 'rank' not in source or not source.get('metadata'):continue
        path=Path(source['metadata'])
        if not path.is_file():continue
        metadata=json.loads(path.read_text());rows.append({'rank':source['rank'],'metadata':metadata})
        args=metadata.get('distributed_args',{})
        for key,field in FIELDS.items():
            value=args.get(field)
            if isinstance(value,int) and not isinstance(value,bool) and value>0:values[key].add(value)
        sources.append({'rank':source['rank'],'file':str(path),'kind':'distributed_args'})
        csv_path=Path(source['trace']).parent/'step_trace_time.csv'
        if not csv_path.is_file():continue
        with csv_path.open(encoding='utf-8-sig',newline='') as handle:
            for row in csv.DictReader(handle):
                step=str(row.get('Step','')).strip()
                if step not in report['steps']:continue
                try:
                    duration=sum((Decimal(row[key]) for key in ('Computing','Communication(Not Overlapped)','Free')),Decimal(0))
                    if not duration.is_finite() or duration<0:continue
                except (KeyError,InvalidOperation,TypeError):continue
                step_times[step]=max(step_times.get(step,Decimal(0)),duration)
        sources.append({'rank':source['rank'],'file':str(csv_path),'kind':'step_csv_us'})
    micro=micro_configuration(Path(report['profiling_path']),rows,log_dir)
    for key,field in FIELDS.items():
        vals=values[key]
        if not vals:vals.update(micro['mesh_args'].get(field,[]))
    summary={key:next(iter(vals)) if len(vals)==1 else None for key,vals in values.items()}
    for key,vals in values.items():
        if len(vals)>1:warnings.append(f'{key}配置来源冲突，保持未知')
    summary['MBN']=micro['expected_mbn']
    warnings.extend(micro['warnings']);sources.extend(micro['sources'])
    return {'summary':summary,'step_time_us':{step:format(value,'f') for step,value in step_times.items()},
            'domain_order':list(ORDER),'domain_colors':color_config()['domains'],'sources':sources,'warnings':warnings,
            'step_time_basis':'当前Step实采卡的最大(Computing + Communication(Not Overlapped) + Free)，单位微秒；显示为秒'}


def encode_summary(summary):
    return json.dumps(summary,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
