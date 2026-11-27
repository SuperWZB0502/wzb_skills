"""Refresh domain semantics and layer names from existing parsed products only."""
from pathlib import Path
from workflow_support import load_json,write_json,file_state,code_key,digest
from parallel_domain_registry import DOMAINS
from mindspeed_domains import model_evidence
from hyperparallel_domains import mesh_log_files

CODE=['parallel_domain_registry.py','mindspeed_domains.py','parallel_domains.py','domain_trace.py',
      'global_communication.py','unified_timeline.py','timeline_colors.py','phase_markers.py','refresh_domains.py',
      'hyperparallel_domains.py']


def state_key(out):
    out=Path(out);report=load_json(out/'report.json');manifest=load_json(out/'analysis_manifest.json') if (out/'analysis_manifest.json').exists() else {}
    evidence=model_evidence(report['profiling_path'],report.get('model_log_dir') or manifest.get('input',{}).get('log_dir'))
    paths=[out/'report.json',Path(evidence['log_file']),*sorted((out/'domain_cache').glob('communication_rank_*.json'))]
    paths.extend(Path(source['metadata']) for source in report.get('sources',[]) if source.get('metadata'))
    paths.extend(Path(source['trace']).with_name('communication.json')
                 for source in report.get('sources',[]) if source.get('trace'))
    paths.extend(mesh_log_files(report['profiling_path'],Path(evidence['log_file']).parent))
    paths.extend(out/f'timeline_cache/raw_rank_{source["rank"]}.json.gz' for source in report.get('sources',[]))
    return digest({'code':code_key(Path(__file__).parent,CODE),'inputs':[file_state(p) for p in paths]}),evidence


def record_state(output_dir):
    out=Path(output_dir);key,_=state_key(out);write_json(out/'domain_semantics.meta.json',{'key':key,'report_state':file_state(out/'unified_report.json')})


def refresh(output_dir, force=False):
    from parallel_domains import build_domain_reports
    from unified_timeline import build_unified_report
    from phase_markers import annotate_layer_numbers
    out=Path(output_dir);key,evidence=state_key(out);meta=out/'domain_semantics.meta.json'
    if not force and meta.exists() and load_json(meta).get('key')==key:return {'cache_hit':True}
    pp=load_json(out/'report.json');old=load_json(out/'unified_report.json');rows=[]
    for source in pp['sources']:
        cache=out/f'domain_cache/communication_rank_{source["rank"]}.json'
        if not cache.exists():cache=out/f'cache/rank_{source["rank"]}.json'
        if not cache.exists():raise ValueError('缺少通信解析缓存，请运行正常分析入口：'+str(cache))
        row=load_json(cache)
        for item in row['fingerprint']:
            state=file_state(Path(item['path']))
            if state.get('size')!=item['size'] or state.get('mtime_ns')!=item['mtime_ns']:
                raise ValueError('原始profiling发生变化，请运行正常分析入口')
        rows.append(row)
    overrides=load_json(out/'domains/audit.json').get('overrides',{}) if (out/'domains/audit.json').exists() else {}
    domains=build_domain_reports(Path(pp['profiling_path']),rows,pp,out,overrides=overrides,log_dir=Path(evidence['log_file']).parent)
    markers=load_json(out/'phase_markers.json') if (out/'phase_markers.json').exists() else {}
    annotate_layer_numbers(markers,pp,evidence);write_json(out/'phase_markers.json',markers)
    streams={'events':[e for e in old['events'] if e['kind']!='communication'],
        'stack_table':old.get('stack_table',{}),'communication_stacks':{e['event_id']:e for e in old['events'] if e['kind']=='communication'},
        'micro_stacks':{r['region_id']:r for r in old.get('compute_regions',[])},
        'rank_info':old.get('rank_info',{}),'sources':old.get('sources',[]),
        'stats':old.get('stats',{}).get('stream_extraction',{}),
        'stack_requested':not old.get('analysis_options',{}).get('communication_only',False)}
    report=build_unified_report(pp,domains,streams,markers);report['analysis_options']=old.get('analysis_options',{})
    write_json(out/'unified_report.json',report)
    write_json(out/'unified_summary.json',{name:report[name] for name in ('title','stats','time_origin_us','rank_info','notes','sources')})
    write_json(out/'domains/index.json',{'order':list(report['domains']),'time_unit':'us','time_origin_us':report['time_origin_us'],
        'modules':[{'domain':name,'report':name+'/report.json','events':len((pp if name=='PP' else domains[name])['events']),
                    'groups':len((pp if name=='PP' else domains[name])['groups'])} for name in report['domains']]})
    record_state(out)
    return {'cache_hit':False,'by_domain':report['stats']['by_domain'],'events':report['stats']['event_count']}


if __name__=='__main__':
    import argparse,json
    from workflow_support import lock_file
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True);args=parser.parse_args()
    with lock_file(args.output_dir/'.workflow.lock'):print(json.dumps(refresh(args.output_dir,True),ensure_ascii=False))
