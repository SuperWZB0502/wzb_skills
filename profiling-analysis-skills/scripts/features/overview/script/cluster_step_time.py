#!/usr/bin/env python3
"""Independent global overview with rank step metrics and sourced training configuration.

Source behavior: msprof_analyze.cluster_analyse.analysis.step_trace_time_analysis
(load_step_trace_time_data, analyze_step_time, dump_data). Reads exported TEXT
step_trace_time.csv files. Communication attribution streams TEXT traces with
ijson and caches derived evidence; configuration comes from metadata/run logs.
JSON uses us; legacy CSV uses ms; overview CSV uses seconds or percentages.
"""
from __future__ import annotations
import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote
from overview_config import collect_config
from overview_communication import collect_communication
from global_overview import enrich_overview, overview_csv

FEATURE_DIR = Path(__file__).absolute().parents[1]
BASE = FEATURE_DIR.parent
FIELDS = (('Computing','计算'),('Communication(Not Overlapped)','未掩盖通信'),
          ('Overlapped','掩盖通信'),('Communication','总通信'),('Free','Free'))
COLUMNS = ['卡号']+[label for _,label in FIELDS]+['端到端时间']
START = '<!-- MSINSIGHT:rank-step-time:START -->'
END = '<!-- MSINSIGHT:rank-step-time:END -->'
FEATURES_END = '<!-- MSINSIGHT_FEATURES_END -->'


def profiling_from_document(document):
    match=re.search(r'样例\s*profiling\s*[：:]\s*`?([^\n`]+)',document.read_text(encoding='utf-8'),re.I)
    if not match:
        raise ValueError(f'{document} 中没有“样例profiling：”路径')
    path=Path(match.group(1).strip()).expanduser()
    return path if path.is_absolute() else document.parent/path


def discover_ranks(profiling):
    if not profiling.is_dir():
        raise ValueError(f'profiling 目录不存在：{profiling}')
    ranks=[]
    for parent,directories,_ in os.walk(profiling):
        folder=Path(parent)
        if folder.name.endswith(('_ascend_ms','_ascend_pt')):
            ranks.append(folder);directories[:]=[]
        else:
            directories[:]=sorted(d for d in directories if d not in {
                'ASCEND_PROFILER_OUTPUT','FRAMEWORK','logs','cluster_analysis_output'
            } and not d.startswith('PROF_'))
    if not ranks:
        raise ValueError('未找到 *_ascend_ms 或 *_ascend_pt；本功能需要已导出 step_trace_time.csv 的 TEXT profiling')
    return sorted(ranks)


def rank_id(folder):
    infos=sorted(folder.glob('profiler_info*.json'))
    if len(infos)!=1:
        raise ValueError(f'{folder} 需要恰好一个 profiler_info*.json，当前有 {len(infos)} 个')
    info=infos[0];data=json.loads(info.read_text(encoding='utf-8'))
    value=data.get('ms_profiler_info',{}).get('rank_id',data.get('rank_id'))
    filename=re.fullmatch(r'profiler_info_(\d+)\.json',info.name)
    if value is None and filename:
        value=filename.group(1)
    if not re.fullmatch(r'\d+',str(value)):
        raise ValueError(f'{info} 中没有有效的全局 rank_id')
    rank=int(value)
    if filename and rank!=int(filename.group(1)):
        raise ValueError(f'{info} 文件名与 rank_id 不一致')
    return rank,info


def step_key(value):
    return (0,int(value)) if re.fullmatch(r'\d+',value) else (1,value)


def collect(profiling,selected_step=None,folders=None):
    steps,sources,rank_paths,warnings={},[],{},[]
    for folder in (folders if folders is not None else discover_ranks(profiling)):
        rank,info=rank_id(folder)
        if rank in rank_paths:
            raise ValueError(f'重复 rank {rank}：{rank_paths[rank]} 与 {folder}；请指定一次采集目录')
        rank_paths[rank]=str(folder)
        path=folder/'ASCEND_PROFILER_OUTPUT/step_trace_time.csv'
        if not path.is_file():
            raise ValueError(f'卡号 {rank} 缺少 {path}；不能将未采集数据填为 0')
        reader=csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig')))
        missing=set(['Step']+[name for name,_ in FIELDS])-set(reader.fieldnames or [])
        if missing:
            raise ValueError(f'{path} 缺少列：{", ".join(sorted(missing))}')
        seen=set()
        for line,raw in enumerate(reader,2):
            step=(raw.get('Step') or '').strip()
            if not step:
                raise ValueError(f'{path}:{line} 的 Step 为空')
            if step in seen:
                raise ValueError(f'{path}:{line} 重复 rank/Step：{rank}/{step}')
            seen.add(step);row={'卡号':rank}
            for name,label in FIELDS:
                try:
                    value=Decimal(raw[name].strip())
                    if not value.is_finite() or value<0:
                        raise InvalidOperation
                except (InvalidOperation,ValueError,AttributeError):
                    raise ValueError(f'{path}:{line} 的 {name} 不是有效非负耗时：{raw.get(name)!r}') from None
                row[label]=format(value,'f')
            row['端到端时间']=format(sum((Decimal(row[k]) for k in ('计算','未掩盖通信','Free')),Decimal(0)),'f')
            if selected_step is not None and step!=selected_step:
                continue
            if abs(Decimal(row['总通信'])-Decimal(row['未掩盖通信'])-Decimal(row['掩盖通信']))>Decimal('0.01'):
                warnings.append(f'Step {step} 卡号 {rank}：总通信与未掩盖通信+掩盖通信不一致')
            steps.setdefault(step,[]).append(row)
        if not seen:
            raise ValueError(f'{path} 没有数据行')
        sources.append({'rank':rank,'csv':str(path),'profiler_info':str(info),
                        'csv_bytes':path.stat().st_size,'csv_mtime_ns':path.stat().st_mtime_ns})
    if not steps:
        raise ValueError(f'没有找到 Step {selected_step!r} 的数据')
    for step,rows in steps.items():
        absent=sorted(set(rank_paths)-{r['卡号'] for r in rows})
        if absent:
            warnings.append(f'Step {step} 缺少卡 {absent} 的数据，没有填零或跨 Step 取平均')
    return {'schema_version':2,'feature_id':'rank-step-time','profiling_path':str(profiling),
            'generated_at':datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
            'time_unit':'us','csv_time_unit':'ms','columns':COLUMNS,'ranks':sorted(rank_paths),
            'steps':{s:sorted(steps[s],key=lambda r:r['卡号']) for s in sorted(steps,key=step_key)},
            'sources':sorted(sources,key=lambda s:s['rank']),'warnings':warnings}


def csv_text(rows):
    buf=io.StringIO(newline='');writer=csv.writer(buf);writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow([row['卡号']]+[format(Decimal(row[label])/1000,'f') for label in COLUMNS[1:]])
    return buf.getvalue()


SECTION_PATH=FEATURE_DIR/'script/global_overview.html'


def update_html(path,data):
    encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    section=START+'\n'+SECTION_PATH.read_text(encoding='utf-8').replace('__DATA__',encoded)+'\n'+END
    existing=path.read_text(encoding='utf-8') if path.exists() else ''
    if START in existing:
        if END not in existing:
            raise ValueError(f'{path} 功能区块不完整，未覆盖现有内容')
        begin=existing.index(START);end=existing.index(END,begin)+len(END)
        content=existing[:begin]+section+existing[end:]
    elif FEATURES_END in existing:
        content=existing.replace(FEATURES_END,section+'\n'+FEATURES_END,1)
    elif existing.strip():
        match=re.search(r'</body\s*>',existing,re.I)
        if not match:
            raise ValueError(f'{path} 没有 HTML body，未覆盖现有内容')
        content=existing[:match.start()]+section+'\n'+existing[match.start():]
    else:
        content='<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MSINSIGHT 功能演示</title><style>body{margin:0;background:#f5f7fa}</style></head><body><main id="features">\n'+section+'\n'+FEATURES_END+'\n</main></body></html>\n'
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content,encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiling',type=Path,help='省略时读取拆解文档顶部 profiling')
    parser.add_argument('--document',type=Path,default=BASE/'MSINSIGHT_核心功能拆解.md')
    parser.add_argument('--html',type=Path,default=BASE/'MSINSIGHT_功能演示.html')
    parser.add_argument('--output-dir',type=Path,default=FEATURE_DIR/'output')
    parser.add_argument('--step',help='仅输出指定 Step，默认分开保留所有 Step')
    parser.add_argument('--grad-ar-mode',choices=['gradient','pp_allreduce'],default='gradient',help='Grad AR统计梯度相关AllReduce，或显式采用PP域全部AllReduce约定')
    parser.add_argument('--communication-cache',type=Path,help='通信流式解析缓存目录，默认output/cache')
    parser.add_argument('--seed-communication-cache',type=Path,help='可选的已有通信泳道缓存，须通过原始文件fingerprint验证')
    args=parser.parse_args(argv);started=time.perf_counter()
    try:
        profiling=(args.profiling or profiling_from_document(args.document)).expanduser().resolve()
        data=collect(profiling,args.step)
        folders={source['rank']:Path(source['csv']).parent.parent for source in data['sources']}
        config=collect_config(profiling,folders)
        available={rank:folder for rank,folder in folders.items() if (folder/'profiler_metadata.json').is_file() and (folder/'ASCEND_PROFILER_OUTPUT/trace_view.json').is_file()}
        if available:
            communication=collect_communication(profiling,available,config,
                args.communication_cache or args.output_dir/'cache',args.seed_communication_cache,args.grad_ar_mode)
        else:
            communication={'per_step_rank':{},'notes':['未提供原始trace/通信组元数据，通信分类保持为空；原始Step耗时仍可查看。'],'sources':[],'warnings':[],'grad_ar_mode':args.grad_ar_mode}
        enrich_overview(data,config,communication)
        data['processing_seconds']=round(time.perf_counter()-started,6)
        data['source_csv_bytes']=sum(s['csv_bytes'] for s in data['sources'])
        args.output_dir.mkdir(parents=True,exist_ok=True)
        for step,rows in data['steps'].items():
            (args.output_dir/('step_'+quote(step,safe='')+'.csv')).write_text(csv_text(rows),encoding='utf-8-sig')
            for mode in ['time','percent']:
                (args.output_dir/('overview_step_'+quote(step,safe='')+'_'+mode+'.csv')).write_text(overview_csv(data,step,mode),encoding='utf-8-sig')
        (args.output_dir/'report.json').write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (args.output_dir/'configuration.json').write_text(json.dumps(config,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (args.output_dir/'communication_breakdown.json').write_text(json.dumps(communication,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        update_html(args.html,data)
    except (ValueError,OSError,csv.Error,RuntimeError) as exc:
        parser.exit(2,f'错误：{exc}\n')
    print(json.dumps({'html':str(args.html.absolute()),'ranks':data['ranks'],'steps':list(data['steps']),
                      'rows':sum(len(r) for r in data['steps'].values()),'warnings':data['warnings'],
                      'source_csv_bytes':data['source_csv_bytes'],
                      'elapsed_seconds':round(time.perf_counter()-started,6)},ensure_ascii=False))
    return 0


if __name__=='__main__':
    sys.exit(main())
