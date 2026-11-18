"""Explain every rank/Step Free difference using the original per-rank Step boundaries."""
import argparse,csv
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path
import ijson
from workflow_support import load_json,write_json

def analyze(output_dir):
    out=Path(output_dir);pp=load_json(out/'report.json');origin=D(pp['time_origin_us']);bounds={};expected={};observed=defaultdict(D);clipped=defaultdict(D);examples=defaultdict(list)
    for path in sorted((out/'cache').glob('rank_*.json')):
        row=load_json(path);rank=int(row['rank'])
        for step in row['steps']:
            left=D(step['absolute_start_us'])-origin;bounds[(rank,str(step['step']))]=(left,left+D(step['duration_us']))
    for src in pp['sources']:
        path=Path(src['trace']).parent/'step_trace_time.csv'
        if path.exists():
            with path.open(encoding='utf-8-sig') as handle:
                for row in csv.DictReader(handle):expected[(int(src['rank']),row['Step'])]=D(row['Free'])
    stream_path=out/'streams/report.json'
    if not stream_path.exists():
        result={'status':'not_requested','checks':[],'reason':'本次没有设备流报告。'};write_json(out/'free_boundaries.json',result);return result
    with stream_path.open('rb') as handle:
        for event in ijson.items(handle,'events.item'):
            if event['kind']!='free':continue
            scope=(int(event['rank']),str(event['step']));start,end=D(event['start_us']),D(event['end_us']);observed[scope]+=end-start
            if scope not in bounds:continue
            left,right=bounds[scope];duration=max(D(0),min(end,right)-max(start,left));clipped[scope]+=duration
            if duration!=end-start:examples[scope].append({'event_id':event['event_id'],'source_ordinal':event['source_ordinal'],'start_us':str(start),'end_us':str(end),'step_start_us':str(left),'step_end_us':str(right),'outside_step_us':str(end-start-duration)})
    checks=[]
    for rank,step in sorted(set(expected)|set(observed)):
        scope=(rank,step);raw=observed[scope];target=expected.get(scope);cut=clipped[scope] if scope in bounds else None
        reason='missing_source_csv' if target is None else 'missing_step_bounds' if cut is None else 'exact' if raw==target else 'step_boundary_clipping' if cut==target else 'unexplained_difference'
        if scope not in observed and target not in (None,D(0)):reason='missing_overlap_free'
        checks.append({'rank':rank,'step':step,'overlap_free_us':str(raw),'step_clipped_free_us':str(cut) if cut is not None else None,'csv_free_us':str(target) if target is not None else None,
            'difference_us':str(raw-target) if target is not None else None,'explanation':reason,'passed':target is not None and (raw==target or cut==target),'crossing_events':examples[scope]})
    result={'status':'complete' if checks and all(c['passed'] for c in checks if c['step']!='unassigned') else 'partial','checks':checks,'policy':'图表保留原始完整Overlap色块；仅对比CSV时按各卡Step裁剪，不改写原始事件。'}
    write_json(out/'free_boundaries.json',result);return result
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();print(analyze(a.output_dir))
