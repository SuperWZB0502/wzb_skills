"""Local workflow utilities. Each feature ships its own copy; no cross-feature imports."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import gc
import sys
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import time

class InputProblem(ValueError):
    def __init__(self, message, status='unsupported'):
        super().__init__(message); self.status=status

def load_json(path):
    enabled=gc.isenabled()
    try:
        if enabled:gc.disable()
        with Path(path).open(encoding='utf-8') as handle:return json.load(handle)
    finally:
        if enabled:gc.enable()

def write_json(path, data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8');temporary.replace(path)

def file_state(path):
    path=Path(path).expanduser().resolve()
    if not path.is_file():return {'path':str(path),'exists':False}
    st=path.stat();return {'path':str(path),'size':st.st_size,'mtime_ns':st.st_mtime_ns}

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()

def code_key(folder, names):
    return digest([{ 'file':name,'sha256':hashlib.sha256((Path(folder)/name).read_bytes()).hexdigest()} for name in names])

def normalize_input(value, logs=None):
    original=Path(value).expanduser().resolve()
    if original.is_file():raise InputProblem(f'当前输入是文件({original.suffix or "无后缀"})；此入口需要Ascend TEXT目录，请提供导出目录或新增格式适配器。')
    if not original.is_dir():raise InputProblem(f'输入目录不存在：{original}','error')
    profiling=original.parent if original.name=='ASCEND_PROFILER_OUTPUT' else original
    if (profiling/'profiling').is_dir():profiling=profiling/'profiling'
    folders=[]
    for parent,dirs,_ in os.walk(profiling):
        p=Path(parent)
        if p.name.endswith(('_ascend_ms','_ascend_pt')):folders.append(p);dirs[:]=[]
        else:dirs[:]=sorted(d for d in dirs if d not in {'FRAMEWORK','ASCEND_PROFILER_OUTPUT','logs','run_log','cluster_analysis_output'} and not d.startswith(('PROF_','.')))
    if not folders:raise InputProblem('未找到已导出的Ascend TEXT采集目录；DB/原始PROF_输入需要先导出TEXT或添加格式适配器。')
    log_candidates=[profiling.parent/'run_log']
    if profiling.name.endswith(('_ascend_ms','_ascend_pt')):log_candidates.insert(0,profiling.parent.parent/'run_log')
    log_dir=Path(logs).expanduser().resolve() if logs else next((p for p in log_candidates if p.is_dir()),log_candidates[0])
    rows=[];issues=[];seen=set()
    for folder in sorted(folders):
        infos=sorted(folder.glob('profiler_info*.json'))
        if len(infos)!=1:issues.append({'folder':str(folder),'reason':'profiler_info不唯一或缺失'});continue
        info=load_json(infos[0]);rank=info.get('ms_profiler_info',{}).get('rank_id',info.get('rank_id'))
        match=re.fullmatch(r'profiler_info_(\d+)\.json',infos[0].name)
        if rank is None and match:rank=match[1]
        if not str(rank).isdigit():issues.append({'folder':str(folder),'reason':'缺少可验证全局rank'});continue
        rank=int(rank)
        if match and int(match[1])!=rank:raise InputProblem(f'卡号与profiler_info文件名冲突：{folder}','error')
        if rank in seen:raise InputProblem(f'重复全局rank {rank}；请指定一次采集目录。','error')
        seen.add(rank)
        rows.append({'rank':rank,'folder':str(folder),'info':str(infos[0]),'metadata':str(folder/'profiler_metadata.json'),
                     'trace':str(folder/'ASCEND_PROFILER_OUTPUT/trace_view.json'),'csv':str(folder/'ASCEND_PROFILER_OUTPUT/step_trace_time.csv')})
    if not rows:raise InputProblem('没有可验证rank的TEXT采集数据。')
    return {'requested_path':str(original),'profiling':str(profiling),'log_dir':str(log_dir),'ranks':rows,'issues':issues}

def input_states(probe, extra=()):
    paths=set(extra)
    for row in probe['ranks']:
        paths.update(row[k] for k in ('info','metadata','trace','csv'))
        paths.add(str(Path(row['folder'])/'FRAMEWORK/mindspore.op_range'))
    for rank in {0,*[row['rank'] for row in probe['ranks']]}:paths.add(str(Path(probe['log_dir'])/f'worker_{rank}.log'))
    p=Path(probe['profiling'])
    paths.update(str(x) for x in (p/'communication_group.json',p/'cluster_analysis_output/communication_group.json',p.parent/'cluster_analysis_output/communication_group.json'))
    return [file_state(p) for p in sorted(paths)]

def default_output(feature, probe):
    path=Path(probe['profiling']);name=path.parent.name if path.name=='profiling' else path.name
    name=re.sub(r'[^\w.-]+','_',name)[:64]+'_'+digest(str(path))[:8]
    return Path(feature)/'output/runs'/name

@contextmanager
def lock_file(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)

def update_section(path, start, end, section):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with lock_file(path.with_name('.'+path.name+'.lock')):
        current=path.read_text(encoding='utf-8') if path.exists() else ''
        block=start+'\n'+section+'\n'+end
        if start in current:
            if end not in current:raise ValueError('已有HTML功能标记不完整，未覆盖。')
            a=current.index(start);b=current.index(end,a)+len(end);result=current[:a]+block+current[b:]
        elif '</body>' in current:result=current.replace('</body>',block+'\n</body>',1)
        elif current.strip():raise ValueError('已有HTML缺少body，未覆盖。')
        else:result='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MSINSIGHT 功能演示</title><style>body{margin:0;background:#f5f7fa}</style></head><body>'+block+'</body></html>'
        temp=path.with_name('.'+path.name+f'.{os.getpid()}.tmp');temp.write_text(result,encoding='utf-8');temp.replace(path)

def outputs_state(folder):
    folder=Path(folder)
    owned={'report.json','configuration.json','communication_breakdown.json','group_catalog.json','compute_regions.json','micro_flows.json','transfer_directions.json','unified_report.json','unified_summary.json','phase_markers.json'}
    paths=[p for p in folder.iterdir() if p.is_file() and (p.name in owned or p.suffix=='.csv')]
    for name in ('domains','streams'):
        paths.extend(p for p in (folder/name).rglob('*') if p.is_file() and not p.name.startswith('.') and not p.name.endswith('.tmp'))
    return [file_state(p) for p in sorted(paths)]

def outputs_match(states):return bool(states) and all(file_state(p['path'])==p for p in states)

class Progress:
    def __init__(self,folder):self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.timings={}
    def emit(self,stage,event,**extra):
        data={'time':datetime.now(timezone.utc).isoformat(timespec='seconds'),'stage':stage,'event':event,**extra}
        with (self.folder/'progress.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(data,ensure_ascii=False)+'\n')
        print(json.dumps(data,ensure_ascii=False),flush=True)
    @contextmanager
    def stage(self,name):
        self.emit(name,'start');t=time.monotonic()
        try:yield
        except BaseException as exc:self.emit(name,'error',message=str(exc));raise
        finally:self.timings[name]=round(time.monotonic()-t,4)
        self.emit(name,'complete',elapsed_seconds=self.timings[name])

def write_delivery(folder, feature, status, capabilities, artifacts, timings, **extra):
    folder=Path(folder)
    tasks=[{'capability':k,'status':v['status'],'reason':v.get('reason',''),'action':v.get('action','核对来源后决定是否扩展规则。')}
           for k,v in capabilities.items() if v.get('needs_agent')]
    data={'schema_version':1,'feature':feature,'status':status,'capabilities':capabilities,'artifacts':artifacts,'timings_seconds':timings,
          'agent_required':status in ('unsupported','error') and bool(tasks),'agent_review_available':bool(tasks),**extra}
    public={k:v for k,v in data.items() if k not in ('input_states','output_states','analysis_key','analysis_code','validation_code')}
    public['analysis_manifest']=str(folder/'analysis_manifest.json')
    write_json(folder/'workflow.json',public)
    if extra.get('analysis_key'):write_json(folder/'analysis_manifest.json',data)
    write_json(folder/'agent_tasks.json',{'schema_version':1,'tasks':tasks,'policy':'只处理未覆盖语义或适配；不得编造未采集数据。'})
    lines=[f'# {feature} 执行结果','',f'状态：{status}',f'运行方式：{extra.get("mode","generated")}','', '| 能力 | 状态 | 说明 |','| --- | --- | --- |']
    for name,v in capabilities.items():lines.append('| '+name+' | '+v['status']+' | '+v.get('reason','').replace('|','/').replace('\n',' ')+' |')
    if extra.get('statistics') is not None:lines+=['','## 统计','', '```json',json.dumps(extra['statistics'],ensure_ascii=False,indent=2,default=str),'```']
    lines+=['','## 产物','']+[f'- {name}：`{value}`' for name,value in artifacts.items()]
    lines+=['','## 可选Agent后续工作','']+([f'- {t["capability"]}：{t["reason"]}；{t["action"]}' for t in tasks] or ['当前没有必须交给Agent分析的任务；缺失采集记录仍以未知显示。'])
    (folder/'summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');return data

@contextmanager
def capture_logs(folder):
    class Tee:
        def __init__(self, console, log):self.console=console;self.log=log
        def write(self,text):self.console.write(text);self.log.write(text);self.log.flush();return len(text)
        def flush(self):self.console.flush();self.log.flush()
    with (Path(folder)/'analysis.log').open('a',encoding='utf-8') as log:
        with contextlib.redirect_stdout(Tee(sys.stdout,log)),contextlib.redirect_stderr(Tee(sys.stderr,log)):yield

def finish_action(folder,previous,feature,artifacts,timings,mode,passed=True):
    caps=dict(previous.get('capabilities',{})) or {'source_coverage':{'status':'partial','reason':'独立渲染/校验入口未声明完整的数据语义覆盖。'}}
    caps[mode]={'status':'complete' if passed else 'error','reason':'只执行指定的渲染或校验动作。'}
    status=previous.get('status','partial') if passed else 'error'
    extras={k:v for k,v in previous.items() if k not in ('schema_version','feature','status','capabilities','artifacts','timings_seconds','agent_required','agent_review_available','mode')}
    return write_delivery(folder,feature,status,caps,{**previous.get('artifacts',{}),**artifacts},timings,mode=mode,**extras)

def ensure_output_paths(probe,folder,html):
    source=Path(probe['profiling']);folder=Path(folder).resolve();html=Path(html).resolve()
    protected=[source,*[Path(row['folder']) for row in probe['ranks']]]
    unsafe=folder==Path(probe['requested_path']) or any(folder==p or p in folder.parents or html==p or p in html.parents for p in protected)
    unsafe=unsafe or any(part.endswith(('_ascend_ms','_ascend_pt')) for part in folder.parts)
    if unsafe or html.suffix.lower() not in ('.html','.htm'):
        exc=InputProblem('输出目录/HTML不得覆盖原始profiling；HTML文件应使用.html或.htm后缀。','error');exc.output_unsafe=True;raise exc
