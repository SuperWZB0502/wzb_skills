#!/usr/bin/env python3
"""Agent-free feature 1 workflow: normalize, analyze/cache, validate, render, report."""
import argparse
import json,time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import sys,time
from workflow_support import (InputProblem,Progress,normalize_input,input_states,default_output,load_json,write_json,
    digest,code_key,outputs_state,outputs_match,lock_file,write_delivery,capture_logs,finish_action,ensure_output_paths)
FEATURE=Path(__file__).resolve().parents[1]
CORE=['cluster_step_time.py','overview_config.py','overview_communication.py','global_overview.py','run_overview.py']

def capabilities(report,probe):
    summary=report['overview']['summary'];rows=[r for rows in report['steps'].values() for r in rows]
    c={'step_metrics':{'status':'complete','reason':f'{len(report["ranks"])}卡，{len(report["steps"])}个Step'},
       'training_configuration':{'status':'complete' if all(summary.get(k) is not None for k in ('GBS','MBS','MBN','TP','CP','EP','DP','PP')) else 'partial','reason':'读取元数据和训练日志；缺失字段保持未知'},
       'memory':{'status':'complete' if all(r.get('显存') is not None for r in rows) else 'partial','reason':'缺少采集结束附近的显存记录时不使用后续迭代峰值','action':'需要完整显存时补充采集或对应日志。'}}
    unknown=[e for e in load_json(Path(probe['output_dir'])/'communication_breakdown.json').get('classifications',[]) if 'not established' in e.get('reason','') or 'conflicting' in e.get('reason','')]
    missing=any(r.get('PP') is None for r in rows)
    c['communication_roles']={'status':'partial' if unknown or missing else 'complete','reason':f'{len(unknown)}个事件语义未确认；未确认值保留其他通信/未知','needs_agent':bool(unknown),'action':'先查看communication_breakdown.json的小规模分类证据；确认后提供--domain-roles映射。'}
    if probe['issues']:c['input_coverage']={'status':'partial','reason':str(probe['issues'])}
    return c

def run(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profiling',type=Path);p.add_argument('--log-dir',type=Path);p.add_argument('--output-dir',type=Path);p.add_argument('--html',type=Path)
    p.add_argument('--step');p.add_argument('--workers',type=int,default=1);p.add_argument('--domain-roles',type=Path);p.add_argument('--grad-ar-mode',choices=['gradient','pp_allreduce'],default='gradient')
    p.add_argument('--refresh',action='store_true');p.add_argument('--render-only',action='store_true');p.add_argument('--validate-only',action='store_true');p.add_argument('--adopt-existing',action='store_true');p.add_argument('--skip-validation',action='store_true')
    p.add_argument('--browser-check',action='store_true');a=p.parse_args(argv)
    if a.render_only and a.validate_only:p.error('render-only与validate-only不能同时使用')
    if a.workers<1:p.error('--workers必须大于0')
    if not a.profiling and not (a.output_dir and (a.render_only or a.validate_only)):p.error('需要--profiling，或--output-dir配合render-only/validate-only')
    started=time.perf_counter()
    probe=None
    try:
        if a.profiling:probe=normalize_input(a.profiling,a.log_dir)
        out=(a.output_dir or default_output(FEATURE,probe)).resolve();html=(a.html or out/'report.html').resolve()
        if probe:ensure_output_paths(probe,out,html)
        out.mkdir(parents=True,exist_ok=True);progress=Progress(out)
        from render_overview import render
        from validate_overview import validate
        with lock_file(out/'.workflow.lock'):
            manifest=out/'analysis_manifest.json'
            previous=load_json(manifest) if manifest.exists() else load_json(out/'workflow.json') if (out/'workflow.json').exists() else {}
            if a.render_only or a.validate_only:
                with progress.stage('validate' if a.validate_only else 'render'):
                    result=validate(out) if a.validate_only else {'html':render(out,html)}
                finish_action(out,previous,'功能1：全局总览',{'html':str(html)} if a.render_only else {'validation':str(out/'validation.json')},progress.timings,'render_only' if a.render_only else 'validate_only',result.get('passed') is not False)
                print(json.dumps(result,ensure_ascii=False,default=str));return 2 if result.get('passed') is False else 0
            probe['output_dir']=str(out)
            good=[r for r in probe['ranks'] if Path(r['csv']).is_file()]
            probe['issues'] += [{'rank':r['rank'],'reason':'缺少step_trace_time.csv，该卡未伪造统计'} for r in probe['ranks'] if r not in good]
            if not good:raise InputProblem('没有可用step_trace_time.csv，先导出TEXT统计表。')
            options={'step':a.step,'grad_ar_mode':a.grad_ar_mode,'domain_roles':load_json(a.domain_roles) if a.domain_roles else {}}
            states=input_states(probe,[str(a.domain_roles)] if a.domain_roles else []);key=digest({'inputs':states,'options':options,'code':code_key(FEATURE/'script',CORE)})
            hit=not a.refresh and previous.get('analysis_key')==key and outputs_match(previous.get('output_states',[]))
            mode='cache_hit' if hit else 'generated'
            if a.adopt_existing:
                if a.refresh or a.domain_roles or a.step is not None or a.grad_ar_mode!='gradient':raise ValueError('adopt-existing只接受默认全Step、gradient口径；其他参数应正常生成。')
                old=load_json(out/'report.json')
                if Path(old['profiling_path']).resolve()!=Path(probe['profiling']):raise ValueError('已有报告不属于当前输入。')
                from overview_config import collect_config
                current=collect_config(Path(probe['profiling']),{r['rank']:Path(r['folder']) for r in good},probe['log_dir'])
                existing=load_json(out/'configuration.json')
                if current['summary']!=existing['summary'] or current['per_rank']!={int(k):v for k,v in existing['per_rank'].items()}:raise ValueError('配置或日志已改变，不能收编旧报告。')
                for r in good:
                    cache=load_json(out/f'cache/communication_rank_{r["rank"]}.json') if Path(r['trace']).is_file() and Path(r['metadata']).is_file() else None
                    if cache:
                        from overview_communication import _fingerprint
                        if cache['fingerprint']!=_fingerprint([Path(r[k]) for k in ('trace','metadata','info')]):raise ValueError('旧通信缓存输入指纹不匹配。')
                hit=True;mode='adopted'
            if hit:progress.emit('analyze','cache_hit',mode=mode)
            if not hit:
                if a.refresh or (previous.get('analysis_code') and previous['analysis_code']!=code_key(FEATURE/'script',CORE)):
                    for path in (out/'cache').glob('communication_rank_*.json'):path.unlink()
                with progress.stage('collect_and_analyze'),capture_logs(out):
                    from cluster_step_time import collect,csv_text
                    from overview_config import collect_config
                    from overview_communication import collect_communication
                    from global_overview import enrich_overview,overview_csv
                    data=collect(Path(probe['profiling']),a.step,[Path(r['folder']) for r in good]);folders={r['rank']:Path(r['folder']) for r in good}
                    config=collect_config(Path(probe['profiling']),folders,probe['log_dir'])
                    if not isinstance(options['domain_roles'],dict):raise ValueError('domain-roles必须是JSON对象')
                    roles=options['domain_roles'].get('communication_group_roles',options['domain_roles'])
                    if not isinstance(roles,dict) or any(v not in ('TP','CP','EP','DP','PP','GLOBAL','Grad AR','其他通信','unresolved') for v in roles.values()):raise ValueError('domain-roles需为组ID到合法通信域的JSON对象')
                    config['communication_group_roles']={k:('其他通信' if v in ('GLOBAL','unresolved') else v) for k,v in roles.items()}
                    available={r['rank']:Path(r['folder']) for r in good if Path(r['trace']).is_file() and Path(r['metadata']).is_file()}
                    comm=collect_communication(Path(probe['profiling']),available,config,out/'cache',grad_ar_mode=a.grad_ar_mode,workers=a.workers) if available else {'per_step_rank':{},'notes':['缺少原始trace或组元数据，通信分类未知。'],'sources':[],'warnings':[],'grad_ar_mode':a.grad_ar_mode}
                    enrich_overview(data,config,comm)
                    data['source_csv_bytes']=sum(src['csv_bytes'] for src in data['sources'])
                    for pattern in ('step_*.csv','overview_step_*_time.csv','overview_step_*_percent.csv'):
                        for old_csv in out.glob(pattern):old_csv.unlink()
                    for step,rows in data['steps'].items():
                        (out/f'step_{step}.csv').write_text(csv_text(rows),encoding='utf-8-sig')
                        for view in ('time','percent'):(out/f'overview_step_{step}_{view}.csv').write_text(overview_csv(data,step,view),encoding='utf-8-sig')
                    for name,value in [('report',data),('configuration',config),('communication_breakdown',comm)]:write_json(out/(name+'.json'),value)
            validation=None
            if not a.skip_validation:
                if mode in ('cache_hit','adopted') and previous.get('input_states')==states and outputs_match(previous.get('output_states',[])) and previous.get('validation_code')==code_key(FEATURE/'script',['validate_overview.py']) and (out/'validation.json').exists():validation=load_json(out/'validation.json')
                else:
                    with progress.stage('validate'):validation=validate(out)
                if not validation['passed']:raise ValueError('通用数据校验未通过：'+str(validation['errors'][:5]))
            with progress.stage('render'):render(out,html)
            if a.browser_check:
                from browser_check import check_html
                with progress.stage('browser_check'):check_html(html,out)
            data=load_json(out/'report.json');caps=capabilities(data,probe)
            caps['data_validation']={'status':'complete' if validation is not None else 'not_requested','reason':'源数据校验通过或复用同输入、同校验代码的已通过结果。' if validation is not None else '本次显式跳过数据校验。'}
            caps['browser_check']={'status':'complete' if a.browser_check else 'not_requested','reason':'浏览器验证通过。' if a.browser_check else '未请求浏览器验证，不影响数据处理。'}
            status='partial' if any(v['status'] not in ('complete','not_requested','not_applicable') for v in caps.values()) else 'complete'
            from decimal import Decimal
            statistics={**data['overview']['summary'],'steps':list(data['steps']),'step_time_seconds':{step:format(Decimal(v)/Decimal(1000000),'.2f') for step,v in data['overview']['step_time_us'].items()}}
            progress.timings['total']=round(time.perf_counter()-started,4)
            result=write_delivery(out,'功能1：全局总览',status,caps,{'html':str(html),'report':str(out/'report.json'),'validation':str(out/'validation.json')},progress.timings,
                mode=mode,statistics=statistics,input={'requested_path':probe['requested_path'],'profiling':probe['profiling'],'log_dir':probe['log_dir']},analysis_key=key,analysis_code=code_key(FEATURE/'script',CORE),input_states=states,options=options,output_states=outputs_state(out),validation_code=code_key(FEATURE/'script',['validate_overview.py']))
            print(json.dumps({'status':result['status'],'mode':mode,'workflow':str(out/'workflow.json'),'html':str(html)},ensure_ascii=False));return 0
    except Exception as exc:
        out=(FEATURE/'output/last_failure' if getattr(exc,'output_unsafe',False) else a.output_dir or FEATURE/'output/last_failure').resolve();out.mkdir(parents=True,exist_ok=True)
        status=getattr(exc,'status','error');write_delivery(out,'功能1：全局总览',status,{'input_or_execution':{'status':status,'reason':str(exc),'needs_agent':status=='unsupported','action':'补充支持的TEXT导出，或为该格式开发适配器。'}},{},{},mode='failed')
        print(str(exc),file=sys.stderr);return 3 if status=='unsupported' else 2
if __name__=='__main__':raise SystemExit(run())
