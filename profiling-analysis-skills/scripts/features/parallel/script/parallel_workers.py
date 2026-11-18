"""Bounded, production per-rank scheduling; workers publish caches atomically."""
from concurrent.futures import ProcessPoolExecutor
from decimal import Decimal
import hashlib,json,time
from pathlib import Path

def _rank_input(task):
    from parallel_domain_split import read_rank
    rank,source,out,refresh=task
    row,hit=read_rank(rank,source,Path(out)/'cache'/f'rank_{rank}.json',refresh)
    return {'rank':rank,'cache_hit':hit,'events':len(row['events'])}

def prepare_rank_inputs(sources,out,workers,refresh=False):
    with ProcessPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_rank_input,[(rank,src,str(out),refresh) for rank,src in sorted(sources.items())]))

def _stream_input(task):
    started=time.perf_counter()
    print(json.dumps({'event':'stream_rank_start','rank':task[0]}),flush=True)
    from timeline_streams import _rank_report,_signature,_write_gzip,_read_gzip_json,VERSION,ADAPTER_ALIASES
    from workflow_support import load_json
    rank,out=task;out=Path(out);row=load_json(out/f'cache/rank_{rank}.json');pp=load_json(out/'report.json')
    source=next(s for s in pp['sources'] if int(s['rank'])==rank);trace=Path(source['trace']);signature=_signature([trace,Path(source['metadata'])]);origin=Decimal(pp['time_origin_us'])
    stage=pp.get('rank_stages',{}).get(str(rank))
    if stage is None:
        stage=next((g['members'].index(rank) for g in pp['groups'] if g['kind']=='pp' and rank in g['members']),None)
    if row.get('metadata',{}).get('distributed_args',{}).get('pipeline_model_parallel_size')==1:stage=0
    key=hashlib.sha256(json.dumps({'stage':stage,'communication_ordinals':sorted(int(e['ordinal']) for e in row['events']),
        'micro_sources':[(r['region_id'],r.get('source_ordinal'),r.get('source_name')) for r in pp['compute_regions'] if int(r['rank'])==rank]},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    path=out/f'timeline_cache/rank_{rank}.json.gz'
    if path.exists():
        import gzip
        cached=_read_gzip_json(path)
        if cached.get('version')==VERSION and cached.get('fingerprint')==signature and cached.get('input_key')==key and cached.get('time_origin_us')==str(origin) and cached.get('adapter_aliases')==list(ADAPTER_ALIASES):return {'rank':rank,'cache_hit':True}
    data=_rank_report(row,trace,origin,stage,pp,scan_cache=out/f'timeline_cache/raw_rank_{rank}.json.gz')
    if _signature([trace,Path(source['metadata'])])!=signature:raise ValueError('Source changed during worker extraction')
    _write_gzip(path,{'version':VERSION,'fingerprint':signature,'time_origin_us':str(origin),'adapter_aliases':list(ADAPTER_ALIASES),'input_key':key,'data':data})
    result={'rank':rank,'cache_hit':False,'events':len(data['events']),'elapsed_seconds':round(time.perf_counter()-started,3)}
    print(json.dumps({'event':'stream_rank_complete',**result}),flush=True)
    return result

def prepare_stream_inputs(ranks,pp,out,workers):
    from parallel_domain_split import dump_json
    dump_json(Path(out)/'report.json',pp)
    with ProcessPoolExecutor(max_workers=workers) as pool:return list(pool.map(_stream_input,[(int(r['rank']),str(out)) for r in ranks]))
