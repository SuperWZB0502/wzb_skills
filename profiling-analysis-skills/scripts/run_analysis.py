#!/usr/bin/env python3
"""Generate the profiling overview and parallel timeline in one offline HTML.

Runs the two isolated feature CLIs in separate processes. No original research
repository, sample HTML, cached profiling or LLM service is required.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from assemble_report import assemble
from verify_package import verify
from worker_budget import choose as choose_workers

SCRIPTS=Path(__file__).resolve().parent
FEATURES={
    'overview':('全局总览',SCRIPTS/'features/overview/script/run_overview.py'),
    'parallel':('5D并行通信图',SCRIPTS/'features/parallel/script/run_parallel.py'),
}
if not FEATURES['overview'][1].is_file():
    FEATURES={'overview':('全局总览',SCRIPTS.parent/'功能01_每卡Step耗时统计/script/run_overview.py'),
              'parallel':('5D并行通信图',SCRIPTS.parent/'功能02_5D并行通信图/script/run_parallel.py')}


def read_json(path):
    with Path(path).open(encoding='utf-8') as handle:return json.load(handle)


def write_json(path,value):
    path=Path(path);temporary=path.with_name('.'+path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');temporary.replace(path)


def input_helper():
    spec=importlib.util.spec_from_file_location('_wzb_input_support',FEATURES['overview'][1].parent/'workflow_support.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def run_child(command,log_path):
    started=time.perf_counter()
    with Path(log_path).open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,env={**os.environ,'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1'})
        try:
            for line in process.stdout:
                log.write(line);log.flush();print(line,end='',flush=True)
            code=process.wait()
        except BaseException:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            raise
    return code,round(time.perf_counter()-started,3)


def summarize(out,report):
    write_json(out/'workflow.json',report)
    lines=['# Profiling分析结果','',f'状态：{report["status"]}',f'HTML：{report["html"]}',
           f'输入：{report["input"].get("profiling","未指定")}',f'总耗时：{report["wall_seconds"]:.3f}秒','',
           '| 功能 | 状态 | 模式 | 耗时（秒） |','| --- | --- | --- | ---: |']
    for feature in report['features']:
        lines.append(f'| {feature["title"]} | {feature["status"]} | {feature.get("mode","未知")} | {feature["wall_seconds"]:.3f} |')
    for feature in report['features']:
        lines+=['',f'## {feature["title"]}',f'执行摘要：{feature["summary"]}',f'详细状态：{feature["workflow"]}']
        for missing in feature.get('limitations',[]):lines.append('- '+missing)
    (out/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiling',type=Path,help='任务根目录、profiling目录、单卡采集目录或ASCEND_PROFILER_OUTPUT')
    parser.add_argument('--output-dir',type=Path,required=True,help='此profiling独立的分析输出目录')
    parser.add_argument('--html',type=Path,help='默认为output-dir/report.html')
    parser.add_argument('--features',choices=['all','overview','parallel'],default='all')
    parser.add_argument('--log-dir',type=Path);parser.add_argument('--step');parser.add_argument('--workers',type=int,help='手动指定逐卡并发；默认按CPU、内存、实采卡数自动选择，最多8')
    parser.add_argument('--domain-roles',type=Path);parser.add_argument('--grad-ar-mode',choices=['gradient','pp_allreduce'],default='gradient')
    parser.add_argument('--communication-only',action='store_true',help='功能2仅解析通信，跳过设备流、Python栈及opt/r')
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--render-only',action='store_true');mode.add_argument('--validate-only',action='store_true')
    parser.add_argument('--browser-check',action='store_true');parser.add_argument('--refresh',action='store_true');parser.add_argument('--reindex',action='store_true')
    args=parser.parse_args(argv)
    if args.workers is not None and args.workers<1:parser.error('workers必须大于0')
    if not args.profiling and not (args.render_only or args.validate_only):parser.error('首次分析需要--profiling')
    if args.validate_only and args.browser_check:parser.error('validate-only只核对数据；页面检查请使用正常运行或render-only配合browser-check')
    if (args.render_only or args.validate_only) and (args.refresh or args.reindex):parser.error('仅渲染/校验模式不应刷新解析数据')
    try:import ijson  # noqa: F401
    except ImportError:parser.error(f'缺少ijson；执行 {sys.executable} -m pip install -r {SCRIPTS / "requirements.txt"}')
    package_check=verify()
    if not package_check['passed']:parser.error('；'.join(package_check['errors']))
    selected=list(FEATURES) if args.features=='all' else [args.features]
    out=args.output_dir.expanduser().resolve();html=(args.html or out/'report.html').expanduser().resolve()
    utility=input_helper();probe=None
    if args.profiling:
        try:probe=utility.normalize_input(args.profiling,args.log_dir);utility.ensure_output_paths(probe,out,html)
        except ValueError as exc:parser.error(str(exc))
    elif html.suffix.lower() not in ('.html','.htm'):parser.error('HTML输出需要.html或.htm后缀')
    profiles=set()
    for key in selected:
        path=out/key/'report.json'
        if (args.render_only or args.validate_only) and not path.exists():parser.error(f'缺少{path}，请先正常分析')
        if path.exists() and (args.render_only or args.validate_only):profiles.add(str(Path(read_json(path)['profiling_path']).resolve()))
    if len(profiles)>1:parser.error('两个功能产物来自不同profiling，不能合并')
    if probe and profiles and probe['profiling'] not in profiles:parser.error('已有结果与--profiling不一致')
    canonical=probe['profiling'] if probe else next(iter(profiles),None)
    # Reuse the established raw-input path protection before writing any output.
    if not probe and canonical:
        try:utility.ensure_output_paths(utility.normalize_input(Path(canonical),args.log_dir),out,html)
        except ValueError as exc:parser.error(str(exc))
    budget=choose_workers(probe,args.workers)
    args.workers=budget['workers']
    print(json.dumps({'event':'worker_budget',**budget},ensure_ascii=False),flush=True)
    out.mkdir(parents=True,exist_ok=True)
    with (out/'.analysis.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=read_json(out/'workflow.json') if (out/'workflow.json').exists() else {}
        if previous.get('skill')=='wzb-profiling-analysis' and previous.get('input',{}).get('profiling') not in (None,canonical):
            parser.error('该输出目录已绑定另一个profiling；请为本次输入使用独立输出目录')
        started=time.perf_counter();rendered_pages={};report={'schema_version':1,'skill':'wzb-profiling-analysis','status':'running',
            'generated_at':datetime.now(timezone.utc).isoformat(),'input':{'profiling':canonical,'requested_path':str(args.profiling) if args.profiling else None},
            'html':str(html),'worker_budget':budget,'package_check':package_check,'features':[],'agent_required':False,'wall_seconds':0}
        write_json(out/'workflow.json',report)
        try:
            # First execution builds all feature input caches from one original pass.
            # Repeated executions retain each feature's existing cache decisions.
            fresh=not any((out/key/'analysis_manifest.json').exists() for key in selected)
            if probe and 'parallel' in selected and fresh and not (args.render_only or args.validate_only or args.refresh or args.reindex or args.communication_only):
                ingest=[sys.executable,str(SCRIPTS/'ingest_trace.py'),'--profiling',probe['profiling'],'--output-dir',str(out),'--workers',str(args.workers)]
                if 'overview' in selected:ingest.append('--overview')
                code,elapsed=run_child(ingest,out/'ingest.log')
                report['preprocessing']={'returncode':code,'wall_seconds':elapsed,'summary':str(out/'ingest.json')}
                if code:raise RuntimeError('原始输入索引失败，详见ingest.log')
            for key in selected:
                title,entry=FEATURES[key];feature_out=out/key
                feature_html=feature_out/'report.html'
                command=[sys.executable,str(entry),'--output-dir',str(feature_out),'--html',str(feature_html)]
                if args.render_only:command+=['--render-only']
                elif args.validate_only:command+=['--validate-only']
                else:
                    command+=['--profiling',str(args.profiling),'--workers',str(args.workers)]
                    if args.log_dir:command+=['--log-dir',str(args.log_dir)]
                    if args.step is not None:command+=['--step',args.step]
                    if args.domain_roles:command+=['--domain-roles',str(args.domain_roles)]
                    if args.refresh:command+=['--refresh']
                    if key=='overview':command+=['--grad-ar-mode',args.grad_ar_mode]
                    else:
                        if args.communication_only:command+=['--communication-only']
                        if args.reindex:command+=['--reindex']
                    if args.browser_check:command+=['--browser-check']
                print(json.dumps({'feature':key,'event':'start','output':str(feature_out)},ensure_ascii=False),flush=True)
                code,elapsed=run_child(command,out/f'{key}.log')
                if code==0 and args.render_only and args.browser_check:
                    check=[sys.executable,'-c','import sys; sys.path.insert(0,sys.argv[1]); from browser_check import check_html; print(check_html(sys.argv[2],sys.argv[3]))',str(entry.parent),str(feature_html),str(feature_out)]
                    extra_code,extra_time=run_child(check,out/f'{key}_browser.log');code=extra_code;elapsed+=extra_time
                workflow=read_json(feature_out/'workflow.json') if (feature_out/'workflow.json').exists() else {}
                item={'key':key,'title':title,'status':workflow.get('status','partial') if code==0 else 'error','returncode':code,
                    'mode':workflow.get('mode'),'wall_seconds':round(elapsed,3),'workflow':str(feature_out/'workflow.json'),'summary':str(feature_out/'summary.md'),
                    'statistics':workflow.get('statistics',{}) if code==0 else {},'limitations':[v.get('reason','')[:500] for v in workflow.get('capabilities',{}).values() if v.get('status') in ('partial','unsupported','error')],
                    'command':command}
                if code==0 and not args.validate_only:
                    if not feature_html.is_file():raise ValueError(f'{key}未生成预期功能页面：{feature_html}')
                    rendered_pages[key]=feature_html
                item['html']=str(feature_html)
                report['features'].append(item);report['wall_seconds']=round(time.perf_counter()-started,3);write_json(out/'workflow.json',report)
            if rendered_pages:
                report['assembly']=assemble(rendered_pages,html)
                print(json.dumps({'event':'assembled',**report['assembly']},ensure_ascii=False),flush=True)
            report['rendered']=bool(rendered_pages)
        except KeyboardInterrupt:
            report.update(status='interrupted',wall_seconds=round(time.perf_counter()-started,3));summarize(out,report);return 130
        except Exception as exc:
            report.update(status='error',error=str(exc),agent_required=True,wall_seconds=round(time.perf_counter()-started,3));summarize(out,report);print(str(exc),file=sys.stderr);return 2
        failed=any(item['returncode'] for item in report['features'])
        report.update(status='error' if all(item['returncode'] for item in report['features']) else 'partial' if failed or any(item['status']=='partial' for item in report['features']) else 'complete',
            agent_required=failed,wall_seconds=round(time.perf_counter()-started,3),html_exists=html.is_file(),rendered=bool(rendered_pages))
        summarize(out,report);print(json.dumps({'status':report['status'],'html':str(html),'workflow':str(out/'workflow.json'),'summary':str(out/'summary.md')},ensure_ascii=False))
        return 2 if failed else 0


if __name__=='__main__':raise SystemExit(main())
