#!/usr/bin/env python3
"""Agent-free feature 2 workflow: input probing, bounded processing, task cache, validation and HTML."""
import argparse
import json,time
from pathlib import Path
import sys
from workflow_support import (InputProblem,Progress,normalize_input,input_states,default_output,load_json,write_json,digest,code_key,
    outputs_state,outputs_match,lock_file,write_delivery,capture_logs,finish_action,ensure_output_paths)
FEATURE=Path(__file__).resolve().parents[1]
CORE=['parallel_domain_split.py','parallel_domains.py','domain_trace.py','global_communication.py','pp_enrichment.py','pp_flow.py',
      'timeline_streams.py','timeline_streams_tlv.py','timeline_training.py','phase_markers.py','unified_timeline.py','timeline_colors.py','parallel_workers.py','fast_json.py','parallel_domain_registry.py','mindspeed_domains.py','hyperparallel_domains.py','refresh_domains.py','run_parallel.py',
      '../../../training_log.py']

def capabilities(out,probe,communication_only,validation):
    pp=load_json(out/'report.json');summary=load_json(out/'unified_summary.json');stats=summary['stats'];unknown=load_json(out/'domains/unresolved/report.json') if (out/'domains/unresolved/report.json').exists() else {'events':[]}
    c={'communication':{'status':'complete','reason':str(stats.get('by_domain',{}))},
       'domain_semantics':{'status':'partial' if unknown['events'] else 'complete','reason':f'{len(unknown["events"])}个事件保留未确认域','needs_agent':bool(unknown['events']),'action':'读取domains/unresolved/communication_events.csv与组证据；有依据时提供--domain-roles，不按组大小猜测。'},
       'pp_topology':{'status':'complete' if pp['groups'] else 'not_applicable','reason':'PP组按元数据确认；缺少PP不会阻止其他域与设备流。'},
       'micro_numbering':{'status':'complete' if pp.get('compute_regions') else 'partial','reason':str(pp.get('enrichment_warnings') or ['没有可验证的编号micro记录'])}}
    if not pp['groups']:
        dimensions={load_json(r['metadata']).get('distributed_args',{}).get('pipeline_model_parallel_size') for r in probe['ranks'] if Path(r['metadata']).is_file()}
        c['pp_topology']={'status':'not_applicable' if dimensions=={1} else 'partial','reason':'PP=1时不需要跨卡PP组；其他情况下无PP证据则保留未知，继续处理其他域。'}
    missing={g['group_id']:g.get('missing_ranks',[]) for g in pp['groups'] if g.get('missing_ranks')}
    if missing:c['pp_sampling']={'status':'partial','reason':str(missing),'action':'需要完整跨stage记录时补充缺少rank的采集。'}
    for kind in ('compute','copy','free'):
        c[kind]={'status':'not_requested' if communication_only else 'complete' if stats.get('by_kind',{}).get(kind,0)>0 else 'partial','reason':f'{stats.get("by_kind",{}).get(kind,0)}个已记录事件；0或缺失不补造。'}
    if communication_only:c['python_stacks']={'status':'not_requested','reason':'communication-only模式跳过调用链关联。'}
    else:
        counts=stats.get('stack_status_counts',{});c['python_stacks']={'status':'partial' if counts.get('unavailable',0) else 'complete','reason':str(counts)+'；缺少因果flow时保留部分链，不按最近时间补栈。'}
    c['optimizer']={'status':'not_requested' if communication_only else 'complete' if stats.get('phase_markers',{}).get('optimizer_count',0) else 'partial','reason':f'{stats.get("phase_markers",{}).get("optimizer_count",0)}个opt包络；按主计算流Adam设备算子的首尾定位，保留原始事件ID。'}
    c['recompute']={'status':'not_requested' if communication_only else 'complete' if stats.get('phase_markers',{}).get('recompute_micro_count',0) else 'partial','reason':f'{stats.get("phase_markers",{}).get("recompute_micro_count",0)}个已确认重计算micro；依赖checkpoint重执行证据及可确认的父b编号。'}
    if validation and validation.get('free_status')=='complete':
        crossings=[x for x in validation.get('free_checks',[]) if x['explanation']=='step_boundary_clipping']
        c['free_comparison']={'status':'complete','reason':f'{len(crossings)}个rank/Step存在跨边界Free；按本卡Step裁剪后与CSV一致，原始图中区间不改写。'}
    if validation and validation.get('free_status')=='partial':
        items=[x for x in validation.get('free_checks',[]) if x['explanation']=='unexplained_difference']
        c['free_comparison']={'status':'partial','reason':str(items or '缺少Free记录、CSV或Step边界，无法完成Free对照'),'needs_agent':bool(items),'action':'读取free_boundaries.json，核对未解释差异的原始记录。'}
    detail_meta=out/'operator_details.meta.json'
    if detail_meta.exists():
        detail=load_json(detail_meta).get('stats',{});total=detail.get('compute_total',0);recorded=detail.get('compute_with_io',0)
        c['compute_io']={'status':'not_requested' if communication_only else 'complete' if total and recorded==total else 'partial','reason':f'{detail.get("compute_matched",0)}/{total}个计算事件唯一关联CSV；{recorded}个至少记录一个输入/输出元字段，缺失字段不补造。'}
        c['communication_details']={'status':'complete' if detail.get('communication_matched',0)==detail.get('communication_total',0) else 'partial','reason':f'{detail.get("communication_matched",0)}个通信事件关联原始统计；{detail.get("communication_with_bandwidth",0)}个有非零传输数据；{detail.get("communication_with_matrix_links",0)}个有部分矩阵链路样本，集合通信不虚构唯一端点。'}
    if probe['issues']:c['input_coverage']={'status':'partial','reason':str(probe['issues'])}
    return c

def adopt(out,probe,args):
    if args.refresh or args.reindex or args.step is not None or args.domain_roles or args.communication_only:raise ValueError('adopt-existing仅用于默认全Step、全流的已审核报告。')
    pp=load_json(out/'report.json')
    if Path(pp['profiling_path']).resolve()!=Path(probe['profiling']):raise ValueError('已有报告与当前输入路径不同。')
    from parallel_domain_split import fingerprint
    cached_rows=[]
    for row in probe['ranks']:
        path=out/f'cache/rank_{row["rank"]}.json'
        if not path.exists():raise ValueError(f'已有产物缺少rank {row["rank"]}的来源缓存。')
        cached=load_json(path);cached_rows.append(cached)
        if cached['fingerprint']!=fingerprint([Path(row[k]) for k in ('trace','metadata','info')]):raise ValueError('来源文件指纹改变，不能收编已有产物。')
    from timeline_training import micro_configuration,ARGUMENT
    current=micro_configuration(Path(probe['profiling']),cached_rows,probe['log_dir'])
    numbered={(str(r['rank']),str(r['step'])) for r in pp.get('compute_regions',[]) if r.get('basis','').endswith('非交错流水线F/B调用顺序')}
    if current['expected_mbn'] is not None:
        for scope,count in pp.get('enrichment_stats',{}).get('phase_counts',{}).items():
            if tuple(scope.split(':')) in numbered and (count['f']!=current['expected_mbn'] or count['b']!=current['expected_mbn']):raise ValueError('当前日志MBN与已有推断micro编号不符，必须重新处理。')
    for item in pp.get('micro_configuration',{}).get('sources',[]):
        path=Path(item['file'])
        lines=path.read_text(errors='replace').splitlines() if path.exists() else []
        match=ARGUMENT.match(lines[item['line']-1]) if len(lines)>=item['line'] else None
        if not match or match[1]!=item['key'] or match[2]!=item['value']:raise ValueError('micro配置日志改变，不能收编已有产物。')

def run(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--profiling',type=Path);p.add_argument('--log-dir',type=Path);p.add_argument('--output-dir',type=Path);p.add_argument('--html',type=Path)
    p.add_argument('--step');p.add_argument('--workers',type=int,default=1);p.add_argument('--domain-roles',type=Path);p.add_argument('--communication-only',action='store_true')
    p.add_argument('--refresh',action='store_true');p.add_argument('--reindex',action='store_true');p.add_argument('--render-only',action='store_true');p.add_argument('--validate-only',action='store_true')
    p.add_argument('--adopt-existing',action='store_true');p.add_argument('--payload-from-html',type=Path);p.add_argument('--validation',choices=['standard','deep','off'],default='standard');p.add_argument('--browser-check',action='store_true')
    a=p.parse_args(argv)
    if a.render_only and a.validate_only:p.error('render-only与validate-only不能同时使用')
    if a.workers<1:p.error('--workers必须大于0')
    if not a.profiling and not (a.output_dir and (a.render_only or a.validate_only)):p.error('需要--profiling，或output-dir配合render-only/validate-only')
    started=time.perf_counter()
    try:
        probe=normalize_input(a.profiling,a.log_dir) if a.profiling else None
        out=(a.output_dir or default_output(FEATURE,probe)).resolve();html=(a.html or out/'report.html').resolve()
        if probe:ensure_output_paths(probe,out,html)
        out.mkdir(parents=True,exist_ok=True);progress=Progress(out)
        from render_parallel import render
        from validate_parallel import validate
        with lock_file(out/'.workflow.lock'):
            manifest=out/'analysis_manifest.json'
            previous=load_json(manifest) if manifest.exists() else load_json(out/'workflow.json') if (out/'workflow.json').exists() else {}
            if a.render_only or a.validate_only:
                with progress.stage('validate' if a.validate_only else 'render'):
                    result=validate(out,a.validation=='deep',a.communication_only or previous.get('options',{}).get('communication_only',False)) if a.validate_only else render(out,html,a.payload_from_html)
                # Lightweight semantic refresh can change counts during render-only;
                # delivery summaries must describe the actual current report.
                current=load_json(out/'unified_summary.json')['stats']
                previous=dict(previous)
                previous['statistics']={key:current.get(key) for key in ('event_count','by_kind','by_domain','rank_count','phase_markers')}
                if (out/'operator_details.meta.json').exists():previous['statistics']['operator_details']=load_json(out/'operator_details.meta.json')['stats']
                if (out/'validation.json').exists():
                    checked=load_json(out/'validation.json')
                    previous['statistics'].update(stack_available_percent=checked.get('stack_available_percent'),stack_ratio_scope=checked.get('stack_ratio_scope'))
                previous['capabilities']=dict(previous.get('capabilities',{}))
                previous['capabilities']['communication']={'status':'complete','reason':str(current.get('by_domain',{}))}
                unresolved=load_json(out/'domains/unresolved/report.json') if (out/'domains/unresolved/report.json').exists() else {'events':[]}
                previous['capabilities']['domain_semantics']={'status':'partial' if unresolved['events'] else 'complete','reason':f'{len(unresolved["events"])}个事件保留未确认域；已保留在统一图中','needs_agent':bool(unresolved['events']),'action':'按需核对未确认域的来源，不按组大小猜测。'}
                finish_action(out,previous,'功能2：5D并行通信图',{'html':str(html)} if a.render_only else {'validation':str(out/'validation.json')},progress.timings,'render_only' if a.render_only else 'validate_only',result.get('passed') is not False)
                print(json.dumps(result,ensure_ascii=False,default=str));return 2 if result.get('passed') is False else 0
            good=[r for r in probe['ranks'] if Path(r['metadata']).is_file() and Path(r['trace']).is_file()]
            probe['issues'] += [{'rank':r['rank'],'reason':'缺少组元数据或TEXT trace，该卡未补造数据'} for r in probe['ranks'] if r not in good]
            if not good:raise InputProblem('没有具备trace_view.json、profiler_info和元数据的采集卡；请提供TEXT导出。')
            states=input_states(probe,[str(a.domain_roles)] if a.domain_roles else [])
            options={'step':a.step,'domain_roles':load_json(a.domain_roles) if a.domain_roles else {},'communication_only':a.communication_only}
            analysis_code=code_key(FEATURE/'script',CORE);key=digest({'inputs':states,'options':options,'code':analysis_code})
            hit=not (a.refresh or a.reindex) and previous.get('analysis_key')==key and outputs_match(previous.get('output_states',[]));mode='cache_hit' if hit else 'generated'
            if a.adopt_existing:adopt(out,probe,a);hit=True;mode='adopted'
            if hit:progress.emit('analyze','cache_hit',mode=mode)
            if not hit:
                # Derived caches must follow rule changes. Raw indices remain reusable unless reindex is explicit.
                if a.refresh or a.reindex or (previous.get('analysis_code') and previous['analysis_code']!=analysis_code):
                    for folder,pattern in [('cache','rank_*.json'),('domain_cache','communication_rank_*.json'),('timeline_cache','rank_*.json.gz')]:
                        for file in (out/folder).glob(pattern):file.unlink()
                if a.reindex:
                    for file in (out/'timeline_cache').glob('raw_rank_*.json.gz'):file.unlink()
                from parallel_domain_split import main
                command=['--profiling',probe['profiling'],'--output-dir',str(out),'--html',str(out/'.analysis.html'),'--allow-partial','--no-html','--workers',str(a.workers),'--log-dir',probe['log_dir']]
                if a.step:command+=['--step',a.step]
                if a.domain_roles:command+=['--domain-roles',str(a.domain_roles)]
                if a.communication_only:command+=['--communication-only']
                with progress.stage('analyze'),capture_logs(out):
                    try:main(command)
                    except SystemExit as exc:raise RuntimeError(f'核心解析器退出({exc.code})，详见analysis.log。') from exc
            validation_key=code_key(FEATURE/'script',['validate_parallel.py','free_boundaries.py'])+':'+a.validation+':'+str(a.communication_only)
            validation=None
            if a.validation!='off':
                if mode in ('cache_hit','adopted') and previous.get('input_states')==states and outputs_match(previous.get('output_states',[])) and previous.get('validation_code')==validation_key and (out/'validation.json').exists():validation=load_json(out/'validation.json')
                else:
                    with progress.stage('validate'):validation=validate(out,a.validation=='deep',a.communication_only)
                if not validation['passed']:raise ValueError('通用数据校验失败：'+str(validation['errors'][:5]))
            with progress.stage('render'):render(out,html,a.payload_from_html)
            if a.browser_check:
                from browser_check import check_html
                with progress.stage('browser_check'):check_html(html,out)
            caps=capabilities(out,probe,a.communication_only,validation)
            caps['data_validation']={'status':'complete' if validation is not None else 'not_requested','reason':'通用校验通过或复用同输入、同校验代码的已通过结果。' if validation is not None else '本次未请求校验。'}
            caps['browser_check']={'status':'complete' if a.browser_check else 'not_requested','reason':'浏览器验证通过。' if a.browser_check else '未请求浏览器验证。'}
            status='partial' if any(v['status'] not in ('complete','not_requested','not_applicable') for v in caps.values()) else 'complete'
            stats=load_json(out/'unified_summary.json')['stats']
            statistics={k:stats.get(k) for k in ('event_count','by_kind','by_domain','rank_count','phase_markers')}
            if validation:statistics.update(stack_available_percent=validation.get('stack_available_percent'),stack_ratio_scope=validation.get('stack_ratio_scope'))
            if (out/'operator_details.meta.json').exists():statistics['operator_details']=load_json(out/'operator_details.meta.json')['stats']
            progress.timings['total']=round(time.perf_counter()-started,4)
            result=write_delivery(out,'功能2：5D并行通信图',status,caps,{'html':str(html),'report':str(out/'unified_report.json'),'validation':str(out/'validation.json'),'summary_json':str(out/'unified_summary.json'),'free_boundaries':str(out/'free_boundaries.json'),'operator_details':str(out/'operator_details.json'),'operator_details_validation':str(out/'operator_details_validation.json')},progress.timings,
                mode=mode,statistics=statistics,input={'requested_path':probe['requested_path'],'profiling':probe['profiling'],'log_dir':probe['log_dir']},analysis_key=key,analysis_code=analysis_code,input_states=states,options=options,output_states=outputs_state(out),validation_code=validation_key)
            print(json.dumps({'status':result['status'],'mode':mode,'workflow':str(out/'workflow.json'),'html':str(html)},ensure_ascii=False));return 0
    except Exception as exc:
        out=(FEATURE/'output/last_failure' if getattr(exc,'output_unsafe',False) else a.output_dir or FEATURE/'output/last_failure').resolve();out.mkdir(parents=True,exist_ok=True);status=getattr(exc,'status','error')
        write_delivery(out,'功能2：5D并行通信图',status,{'input_or_execution':{'status':status,'reason':str(exc),'needs_agent':status=='unsupported','action':'补充TEXT导出或扩展格式适配器；不要伪造事件。'}},{},{},mode='failed')
        print(str(exc),file=sys.stderr);return 3 if status=='unsupported' else 2
if __name__=='__main__':raise SystemExit(run())
