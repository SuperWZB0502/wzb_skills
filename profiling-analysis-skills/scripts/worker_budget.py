"""Conservative per-rank concurrency, selected without an agent or GPU runtime."""
import os
from pathlib import Path

GIB=1024**3


def _text(path):
    try:return Path(path).read_text().strip()
    except OSError:return None


def resources():
    try:cores=len(os.sched_getaffinity(0))
    except (AttributeError,OSError):cores=os.cpu_count() or 1
    quota=_text('/sys/fs/cgroup/cpu.max')
    if quota:
        q,period=quota.split()
        if q!='max':cores=min(cores,max(1,int(q)//int(period)))
    else:
        for folder in ('cpu','cpu,cpuacct'):
            q=_text('/sys/fs/cgroup/'+folder+'/cpu.cfs_quota_us');period=_text('/sys/fs/cgroup/'+folder+'/cpu.cfs_period_us')
            if q and period:
                if int(q)>0:cores=min(cores,max(1,int(q)//int(period)))
                break
    available=None
    text=_text('/proc/meminfo') or ''
    for line in text.splitlines():
        if line.startswith('MemAvailable:'):available=int(line.split()[1])*1024
    for limit_path,used_path,stat_path,field in [('/sys/fs/cgroup/memory.max','/sys/fs/cgroup/memory.current','/sys/fs/cgroup/memory.stat','inactive_file'),
                                 ('/sys/fs/cgroup/memory/memory.limit_in_bytes','/sys/fs/cgroup/memory/memory.usage_in_bytes','/sys/fs/cgroup/memory/memory.stat','total_inactive_file')]:
        limit,used=_text(limit_path),_text(used_path)
        if limit and used and limit!='max':
            stats=dict(line.split() for line in (_text(stat_path) or '').splitlines() if len(line.split())==2)
            reclaimable=min(int(used),int(stats.get(field,0)))
            remaining=max(0,int(limit)-int(used)+reclaimable)
            available=remaining if available is None else min(available,remaining)
    return max(1,cores),available


def calculate(rank_count,max_trace_bytes,cores,available_bytes,requested=None):
    ranks=max(1,rank_count)
    estimate=GIB+6*max_trace_bytes
    memory_cap=2 if available_bytes is None else max(1,available_bytes//estimate)
    chosen=min(ranks,requested) if requested is not None else min(ranks,max(1,cores),8,memory_cap)
    if chosen<1:raise ValueError('workers必须大于0')
    return {'mode':'manual' if requested is not None else 'auto','requested':requested,'workers':int(chosen),
            'rank_count':rank_count,'cpu_budget':cores,'available_memory_bytes':available_bytes,
            'estimated_worker_bytes':estimate,'auto_worker_limit':8,'max_trace_bytes':max_trace_bytes,
            'note':'逐卡内存按1GiB+6倍最大trace估计；主进程汇总仍需额外内存，手动workers可覆盖自动预算。'}


def choose(probe,requested=None):
    rows=(probe or {}).get('ranks',[]);sizes=[]
    for row in rows:
        path=Path(row['trace'])
        if path.is_file():sizes.append(path.stat().st_size)
    cores,available=resources()
    return calculate(len(rows),max(sizes,default=0),cores,available,requested)
