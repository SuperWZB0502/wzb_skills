"""Stream a bounded rank/time/domain/operator slice; do not send entire event JSON to an Agent."""
import argparse,json
from decimal import Decimal as D
from pathlib import Path
import ijson
from workflow_support import load_json,write_json

def query(out,ranks=None,step=None,kinds=None,domains=None,start=None,end=None,absolute=False,unresolved=False,contained=False,include_stack=False,limit=50,groups=None,name=None):
    out=Path(out);path=out/'domains/unresolved/report.json' if unresolved else out/'unified_report.json';rows=[];truncated=False
    with path.open('rb') as handle:
        for e in ijson.items(handle,'events.item'):
            kind=e.get('kind','communication');domain=e.get('domain','unresolved' if unresolved else None)
            if ranks and int(e['rank']) not in ranks:continue
            if step is not None and str(e['step'])!=str(step):continue
            if kinds and kind not in kinds:continue
            if domains and domain not in domains:continue
            if groups and e.get('group_id') not in groups:continue
            if name and name.casefold() not in e['name'].casefold():continue
            begin=D(e['absolute_start_us'] if absolute else e['start_us']);end_time=begin+D(e['duration_us'])
            if contained:
                if start is not None and begin<D(start):continue
                if end is not None and end_time>D(end):continue
            else:
                if start is not None and end_time<D(start):continue
                if end is not None and begin>D(end):continue
            if len(rows)>=limit:truncated=True;break
            rows.append(e)
    if include_stack and (out/'streams/stack_table.json').exists():
        stacks=load_json(out/'streams/stack_table.json')
        for e in rows:
            if e.get('stack_id'):e['python_stack']=stacks.get(e['stack_id'])
    return {'schema_version':1,'source':str(path),'time_basis':'absolute us' if absolute else 'common-origin relative us','filters':{'ranks':ranks,'step':step,'kinds':kinds,'domains':domains,'start_us':start,'end_us':end,'absolute':absolute,'unresolved':unresolved,'contained':contained,'groups':groups,'name':name,'limit':limit},'events':rows,'returned_events':len(rows),'truncated':truncated}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--rank',type=int,action='append');p.add_argument('--group',action='append');p.add_argument('--name');p.add_argument('--step');p.add_argument('--kind',action='append',choices=['communication','compute','copy','free']);p.add_argument('--domain',action='append');p.add_argument('--start-us');p.add_argument('--end-us');p.add_argument('--absolute',action='store_true');p.add_argument('--unresolved',action='store_true');p.add_argument('--contained',action='store_true');p.add_argument('--include-stack',action='store_true');p.add_argument('--limit',type=int,default=50);p.add_argument('--output',type=Path);a=p.parse_args()
    if not 1<=a.limit<=1000:p.error('limit应为1～1000')
    r=query(a.output_dir,a.rank,a.step,a.kind,a.domain,a.start_us,a.end_us,a.absolute,a.unresolved,a.contained,a.include_stack,a.limit,a.group,a.name)
    if a.output:write_json(a.output,r)
    else:print(json.dumps(r,ensure_ascii=False,indent=2,default=str))
