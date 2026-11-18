"""Data-driven overview validation, with no fixed dataset/rank/GBS/Step assertions."""
import argparse
import csv
from decimal import Decimal as D
from pathlib import Path
from workflow_support import load_json, write_json

def validate(output_dir):
    out=Path(output_dir);report=load_json(out/'report.json');errors=[];checks=0
    def check(value,message):
        nonlocal checks
        checks+=1
        if not value:errors.append(message)
    source_rows={}
    for src in report['sources']:
        with Path(src['csv']).open(encoding='utf-8-sig') as handle:
            for row in csv.DictReader(handle):source_rows[(int(src['rank']),row['Step'])]=row
    seen=set();mapping={'计算':'Computing','未掩盖通信':'Communication(Not Overlapped)','掩盖通信':'Overlapped','总通信':'Communication','Free':'Free'}
    comm=load_json(out/'communication_breakdown.json') if (out/'communication_breakdown.json').exists() else {}
    summary=report['overview']['summary'];pp=summary.get('PP');mbn=summary.get('MBN')
    for step,rows in report['steps'].items():
        for row in rows:
            identity=(int(row['卡号']),str(step));check(identity not in seen,f'重复rank/Step：{identity}');seen.add(identity)
            raw=source_rows.get(identity);check(raw is not None,f'找不到原始CSV：{identity}')
            if raw:
                for label,column in mapping.items():check(D(row[label])==D(raw[column]),f'{identity} {label}与源CSV不同')
            check(D(row['端到端时间'])==sum((D(row[k]) for k in ('计算','未掩盖通信','Free')),D(0)),f'{identity}端到端公式错误')
            values=comm.get('per_step_rank',{}).get(step,{}).get(str(identity[0]),{})
            if values:
                check(abs(sum((D(v) for v in values.values()),D(0))-D(row['总通信']))<=D('.01'),f'{identity}通信分类未闭合')
            if row.get('bubble') is not None:
                check(D(row['bubble'])==D(row['PP'])+D(row['Grad AR']),f'{identity}bubble公式错误')
                check(D(row['有效耗时'])==D(row['端到端时间'])-D(row['bubble']),f'{identity}有效耗时公式错误')
            if pp and mbn and row.get('理论bubble') is not None:check(abs(D(row['理论bubble'])-(D(pp)-1)/(D(mbn)+D(pp)-1))<D('1e-24'),f'{identity}理论bubble错误')
        check(D(report['overview']['step_time_us'][step])==max(D(r['端到端时间']) for r in rows),f'{step}单Step最大值错误')
        from global_overview import overview_csv
        for mode in ('time','percent'):
            path=out/f'overview_step_{step}_{mode}.csv'
            if path.exists():check(path.read_text(encoding='utf-8-sig').replace('\r\n','\n')==overview_csv(report,step,mode).replace('\r\n','\n'),f'{path.name}与报告不同')
    result={'passed':not errors,'checks':checks,'ranks':report['ranks'],'steps':list(report['steps']),'rows':len(seen),'errors':errors}
    write_json(out/'validation.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args();result=validate(a.output_dir);print(result);return 0 if result['passed'] else 2
if __name__=='__main__':raise SystemExit(main())
