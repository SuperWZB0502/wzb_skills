"""Column definitions and exact formulas for the profiling global overview."""
import csv
from decimal import Decimal, ROUND_HALF_UP
import io

D=Decimal
CATEGORIES=('PP','DP','EP','CP','TP','Grad AR')


def columns():
    result=[{'key':'stage','label':'stage','kind':'integer'},
            {'key':'层数','label':'层数','kind':'integer'},
            {'key':'重计算','label':'重计算','kind':'integer'},
            {'key':'显存','label':'显存（GiB）','kind':'memory','description':'采集结束时日志中的累计max allocated峰值，非该Step独立显存峰值'}]
    for key in ('计算','未掩盖通信','掩盖通信','总通信','Free','端到端时间',*CATEGORIES,'bubble'):
        result.append({'key':key,'label':key,'kind':'time'})
    result.extend([{'key':'有效耗时','label':'计算+未掩盖通信+Free-bubble','kind':'time','mode':'time'},
                   {'key':'理论bubble','label':'理论bubble (PP-1)/(MBN+PP-1)','kind':'ratio','mode':'percent'}])
    return result


def number(value):
    if value is None or value=='':return None
    value=D(str(value))
    return value if value.is_finite() else None


def enrich_overview(data,config,communication):
    summary=dict(config.get('summary',{}));summary['actual_ranks']=len(data['ranks'])
    pp=number(summary.get('PP'));mbn=number(summary.get('MBN'))
    theory=(pp-1)/(mbn+pp-1) if pp is not None and pp>=1 and mbn is not None and mbn>0 else None
    notes=list(config.get('notes',[]))+list(communication.get('notes',[]))
    step_times={};unlisted={}
    by_step=communication.get('per_step_rank',{})
    for step,rows in data['steps'].items():
        step_times[step]=format(max(D(row['端到端时间']) for row in rows),'f')
        for row in rows:
            rank=row['卡号'];meta=config.get('per_rank',{}).get(rank,{})
            for key in ('stage','层数','重计算','显存'):row[key]=meta.get(key)
            if 'pp_group' in meta:row['pp_group']=meta['pp_group']
            for key in ('显存_reserved','memory_iteration'):
                if key in meta:row[key]=meta[key]
            comm=by_step.get(step,{}).get(rank,{})
            for key in (*CATEGORIES,'其他通信'):row[key]=None if comm.get(key) is None else format(D(str(comm[key])),'f')
            pipeline=number(row['PP']);grad=number(row['Grad AR'])
            bubble=pipeline+grad if pipeline is not None and grad is not None else None
            row['bubble']=format(bubble,'f') if bubble is not None else None
            row['有效耗时']=format(D(row['端到端时间'])-bubble,'f') if bubble is not None else None
            row['理论bubble']=format(theory,'f') if theory is not None else None
            other=number(row['其他通信'])
            if other is not None and other!=0:unlisted.setdefault(step,{})[rank]=format(other,'f')
            values=[number(row[k]) for k in CATEGORIES]+[other]
            if all(v is not None for v in values) and abs(sum(values,D(0))-D(row['总通信']))>D('.01'):
                data['warnings'].append(f'Step {step} Rank {rank} 通信分类之和与原始总通信不同，未修改原始总通信')
    if unlisted:notes.append('总通信还包含未列入六类的日志/屏障等其他通信；各行完整分类与差额保存在JSON中，未强行计入Grad AR。')
    notes.extend(['单Step耗时色块取当前Step实际采集卡端到端耗时的最大值，固定以秒显示，不跨Stage累加。',
        'bubble按本表约定计算为PP+Grad AR，属于自定义估算，不等同于原始CSV的Bubble列或实测纯空闲时间。',
        '理论bubble=(PP-1)/(MBN+PP-1)，为理想流水线模型；显存、层数及重计算在百分比模式保持原单位。'])
    data['schema_version']=3
    data['overview']={'title':'全局总览','summary':summary,'columns':columns(),'step_time_us':step_times,
        'notes':notes,'sources':list(config.get('sources',[]))+list(communication.get('sources',[])),
        'unlisted_communication_us':unlisted,'grad_ar_mode':communication.get('grad_ar_mode','gradient')}
    data['warnings'].extend(config.get('warnings',[]));data['warnings'].extend(communication.get('warnings',[]))
    return data


def overview_csv(data,step,mode='time',unit='s'):
    cols=[c for c in data['overview']['columns'] if not c.get('mode') or c['mode']==mode]
    def cell(row,col):
        value=number(row.get(col['key']))
        if value is None:return '—'
        kind=col['kind']
        if kind=='integer':return str(int(value))
        if kind=='ratio':value*=100
        elif kind=='time':
            if mode=='percent':
                total=number(row['端到端时间'])
                if total is None or total<=0:return '—'
                value=value/total*100
            else:value/=D({'s':1000000,'ms':1000,'us':1}[unit])
        text=format(value.quantize(D('.01'),rounding=ROUND_HALF_UP),'f')
        return text+('%' if kind=='ratio' or kind=='time' and mode=='percent' else '')
    buf=io.StringIO(newline='');writer=csv.writer(buf)
    writer.writerow([c.get('percent_label',c['label']) if mode=='percent' else c['label'] for c in cols])
    for row in data['steps'][step]:writer.writerow([cell(row,c) for c in cols])
    return buf.getvalue()
