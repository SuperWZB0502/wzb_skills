"""Extract a bounded overview/communication evidence slice for the user's Agent."""
import argparse,json
from decimal import Decimal as D
from pathlib import Path
from workflow_support import load_json,write_json

def query(out,ranks=None,step=None,category=None,unresolved=False,start=None,end=None,limit=50,groups=None,name=None):
    out=Path(out);report=load_json(out/'report.json');comm=load_json(out/'communication_breakdown.json');result=[];truncated=False
    for e in comm.get('classifications',[]):
        if ranks and int(e['rank']) not in ranks:continue
        if step is not None and str(e['step'])!=str(step):continue
        if category and e['category'] not in category:continue
        if unresolved and not ('not established' in e.get('reason','') or 'conflicting' in e.get('reason','')):continue
        if groups and e.get('group_id') not in groups:continue
        if name and name.casefold() not in e['name'].casefold():continue
        begin=D(e['absolute_start_us']);finish=begin+D(e['duration_us'])
        if start is not None and finish<D(start):continue
        if end is not None and begin>D(end):continue
        if len(result)>=limit:truncated=True;break
        result.append(e)
    rows=[dict(row,step=s) for s,values in report['steps'].items() for row in values if (step is None or str(s)==str(step)) and (not ranks or int(row['卡号']) in ranks)]
    return {'schema_version':1,'source':str(out),'time_basis':'absolute microseconds','filters':{'ranks':ranks,'step':step,'category':category,'unresolved':unresolved,'absolute_start_us':start,'absolute_end_us':end,'groups':groups,'name':name,'limit':limit},'rows':rows[:limit],'row_count':len(rows),'rows_truncated':len(rows)>limit,'communication_evidence':result,'returned_events':len(result),'truncated':truncated}
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--rank',type=int,action='append');p.add_argument('--group',action='append');p.add_argument('--name');p.add_argument('--step');p.add_argument('--category',action='append');p.add_argument('--unresolved',action='store_true');p.add_argument('--absolute-start-us');p.add_argument('--absolute-end-us');p.add_argument('--limit',type=int,default=50);p.add_argument('--output',type=Path);a=p.parse_args()
    if not 1<=a.limit<=1000:p.error('limit应为1～1000')
    r=query(a.output_dir,a.rank,a.step,a.category,a.unresolved,a.absolute_start_us,a.absolute_end_us,a.limit,a.group,a.name)
    if a.output:write_json(a.output,r)
    else:print(json.dumps(r,ensure_ascii=False,indent=2))
