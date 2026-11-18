#!/usr/bin/env python3
"""Single original-trace pass for the independently maintained feature consumers.

Each rank writes only into this run's output tree. Existing source fingerprints
are checked by each normal feature entrypoint; no global/development cache exists.
"""
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
import argparse,gc,importlib.util,json,sys,time
from pathlib import Path

SCRIPTS=Path(__file__).resolve().parent
PARALLEL=SCRIPTS/'features/parallel/script'
OVERVIEW=SCRIPTS/'features/overview/script'
if not PARALLEL.is_dir():
    PARALLEL=SCRIPTS.parent/'功能02_5D并行通信图/script'
    OVERVIEW=SCRIPTS.parent/'功能01_每卡Step耗时统计/script'


def prepare_rank(task):
    enabled=gc.isenabled()
    try:
        if enabled:gc.disable()
        return _prepare_rank(task)
    finally:
        if enabled:gc.enable()


def _prepare_rank(task):
    rank,source,out,overview=task;out=Path(out)
    sys.path.insert(0,str(PARALLEL))
    from parallel_domain_split import read_rank,fingerprint,dump_json
    from timeline_streams import ScanCollector,write_scan_cache,_signature
    sources={key:Path(value) for key,value in source.items()};trace=sources['trace']
    signature=fingerprint([sources[key] for key in ('trace','metadata','info')])
    index=ScanCollector(Decimal(0))
    first=None
    if overview:
        spec=importlib.util.spec_from_file_location('_overview_ingest',OVERVIEW/'overview_communication.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        first=module.TraceCollector(trace,json.loads(sources['metadata'].read_text()),rank)
    def feed(event,ordinal):
        index.feed(event,ordinal)
        if first is not None:first.feed(event,ordinal)
        if ordinal and ordinal%250000==0:print(json.dumps({'event':'ingest_progress','rank':rank,'events_seen':ordinal+1},ensure_ascii=False),flush=True)
    start=time.perf_counter()
    row,_=read_rank(rank,sources,out/'parallel/cache'/f'rank_{rank}.json',refresh=True,observer=feed)
    scan_seconds=time.perf_counter()-start
    targets={int(e['ordinal']) for e in row['events']}
    result=index.finish(targets)
    index.pending.clear()
    write_scan_cache(out/'parallel/timeline_cache'/f'raw_rank_{rank}.json.gz',result,_signature([trace]),Decimal(0),targets)
    if first is not None:
        data=first.finish();data.update(version=module.VERSION,fingerprint=signature)
        dump_json(out/'overview/cache'/f'communication_rank_{rank}.json',data)
    if fingerprint([sources[key] for key in ('trace','metadata','info')])!=signature:
        raise ValueError(f'rank {rank} source changed during ingest')
    stats={'rank':rank,'trace_bytes':trace.stat().st_size,'trace_passes':1,'source_cache_hit':False,
           'events_seen':row['trace_event_count'],'scan_seconds':round(scan_seconds,3),'total_seconds':round(time.perf_counter()-start,3)}
    dump_json(out/'ingest_ranks'/f'rank_{rank}.json',stats)
    return stats


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profiling',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True)
    p.add_argument('--workers',type=int,default=2);p.add_argument('--overview',action='store_true')
    args=p.parse_args();sys.path.insert(0,str(PARALLEL))
    from parallel_domain_split import discover,dump_json
    sources=discover(args.profiling,True);start=time.perf_counter()
    tasks=[(rank,{key:str(value) for key,value in source.items()},str(args.output_dir.resolve()),args.overview) for rank,source in sorted(sources.items())]
    print(json.dumps({'event':'ingest_start','ranks':len(tasks),'workers':args.workers},ensure_ascii=False),flush=True)
    rows=[]
    if args.workers>1:
        with ProcessPoolExecutor(max_workers=min(args.workers,max(1,len(tasks)))) as pool:
            for row in pool.map(prepare_rank,tasks):
                rows.append(row);print(json.dumps({'event':'rank_ingested',**row},ensure_ascii=False),flush=True)
    else:
        for task in tasks:
            row=prepare_rank(task);rows.append(row);print(json.dumps({'event':'rank_ingested',**row},ensure_ascii=False),flush=True)
    report={'mode':'single_pass','workers':args.workers,'wall_seconds':round(time.perf_counter()-start,3),
            'trace_bytes':sum(row['trace_bytes'] for row in rows),'trace_passes_per_rank':1,'ranks':rows}
    dump_json(args.output_dir/'ingest.json',report)
    print(json.dumps({'event':'ingest_complete',**{k:v for k,v in report.items() if k!='ranks'}},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
