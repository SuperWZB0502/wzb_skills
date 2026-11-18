"""Render the existing report without rescanning profiling data."""
import argparse
import json
from pathlib import Path
from workflow_support import load_json, update_section

def render(output_dir,html):
    from cluster_step_time import START,END,SECTION_PATH
    data=load_json(Path(output_dir)/'report.json')
    # Keep existing reports and CSV presentation in sync without reclassifying events.
    from global_overview import columns,overview_csv
    from workflow_support import write_json
    if data.get('overview') and data['overview'].get('columns')!=columns():
        data['overview']['columns']=columns()
        write_json(Path(output_dir)/'report.json',data)
        for step in data['steps']:
            for mode in ('time','percent'):
                (Path(output_dir)/f'overview_step_{step}_{mode}.csv').write_text(overview_csv(data,step,mode),encoding='utf-8-sig')
    encoded=json.dumps(data,ensure_ascii=False,allow_nan=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    update_section(html,START,END,SECTION_PATH.read_text(encoding='utf-8').replace('__DATA__',encoded))
    return str(Path(html).resolve())
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--html',type=Path,required=True);a=p.parse_args();print(render(a.output_dir,a.html))
