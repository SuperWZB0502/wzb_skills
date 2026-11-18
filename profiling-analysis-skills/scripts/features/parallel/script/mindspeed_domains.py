"""MindSpeed-MM-XY group construction, guarded by a complete observed fingerprint.

Source: megatron/core/parallel_state.py RankGenerator / initialize_model_parallel.
No torch import, no dependency on the model checkout at runtime. Numeric aliases
are used ONLY after all observed pre-extension memberships match construction
order and >=4 independent initialization families corroborate that order.
"""
import ast
from collections import defaultdict
import json
from pathlib import Path
import re

FIELDS = {'TP':'tensor_model_parallel_size','CP':'context_parallel_size',
          'EP':'expert_model_parallel_size','DP':'data_parallel_size','PP':'pipeline_model_parallel_size'}
ARGUMENT = re.compile(r'^\s+(\w+)\s+\.{2,}\s+(.*?)\s*$')


def model_evidence(profiling, log_dir=None):
    path = (Path(log_dir) if log_dir else Path(profiling).parent/'run_log')/'worker_0.log'
    wanted = set(FIELDS.values()) | {'use_tp_pp_dp_mapping','moe_tp_extend_ep','moe_hierarchical_alltoallv',
              'num_layer_list','num_layers','recompute_num_layers','recompute_method',
              'virtual_pipeline_model_parallel_size','pipeline_model_parallel_split_rank', 'reduce_recompute_for_last_chunk'}
    values, sources, logged = {}, {}, {}
    if path.is_file():
        for number, line in enumerate(path.read_text(errors='replace').splitlines(), 1):
            match = ARGUMENT.match(line)
            if match and match[1] in wanted:
                key, raw = match.groups()
                try: value = ast.literal_eval(raw)
                except (ValueError, SyntaxError): value = raw
                if key in values and values[key] != value:
                    values[key] = None
                else: values[key] = value
                sources[key] = {'file':str(path),'line':number,'raw':raw}
            match = re.match(r'^.*?\ball_(\w+)_groups\s+(\[.*\])\s*$',line)
            if match:
                try: logged[match[1]] = ast.literal_eval(match[2])
                except (ValueError, SyntaxError): pass
    return {'values':values,'sources':sources,'logged_groups':logged,'log_file':str(path)}


def rank_groups(config, token, expert=False, independent=False):
    tp, cp, dp, pp, ep = (int(config[k]) for k in ('TP','CP','DP','PP','EP'))
    order = config.get('order','tp-cp-ep-dp-pp').lower()
    bypass = order == 'tp-cp-ep-dp-pp'
    if expert and bypass: dp, cp = dp*cp, 1
    if not expert and bypass: ep = 1
    sizes = {'tp':tp,'pp':pp,'dp':dp,'ep':ep,'cp':cp}
    names = order.split('-')
    for name in sizes:
        if name not in names:
            if sizes[name] != 1: raise ValueError('non-unit dimension omitted from rank order')
            names.append(name)
    if independent:
        if dp % ep: raise ValueError('expert degree does not divide expert data dimension')
        sizes['dp'] //= ep
    else: names.remove('ep')
    shape = [sizes[name] for name in names]
    world = tp*cp*dp*pp
    groups = defaultdict(list)
    selected = token.split('-')
    if any(name not in names for name in selected): raise ValueError('invalid selected dimensions')
    for rank in range(world):
        stride = 1; coordinates = {}
        for name, size in zip(names, shape):
            coordinates[name] = rank//stride % size; stride *= size
        fixed = tuple(coordinates[name] for name in reversed(names) if name not in selected)
        groups[fixed].append(rank)
    return [groups[key] for key in sorted(groups)]


def construction_schedule(config):
    schedule = {}; count = 0
    def add(family, domain, groups, gloo=False):
        nonlocal count
        for members in groups:
            schedule[str(count)] = {'family':family,'domain':domain,'members':list(members)}
            count += 2 if gloo else 1
    base=lambda token:rank_groups(config,token)
    expert=lambda token:rank_groups(config,token,True,True)
    add('dp','DP',base('dp'),True)
    add('dp_cp','DP-With-CP',base('dp-cp'),True)
    add('cp','CP',base('cp'))
    add('mp',None,base('tp-pp'))
    add('tp','TP',base('tp'))
    split=config.get('split_rank')
    for members in base('pp'):
        add('pp','PP',[members])
        embedding=list(dict.fromkeys([members[0], *([members[split]] if split is not None else []), members[-1]]))
        position=list(dict.fromkeys([members[0], *([members[split]] if split is not None else [])]))
        add('embedding',None,[embedding]);add('position_embedding',None,[position])
    add('tp_dp_cp',None,base('tp-dp-cp'))
    add('tp_dp',None,base('tp-dp'))
    add('tp_cp',None,base('tp-cp'))
    add('mp_exp',None,expert('tp-ep-pp'))
    add('tp_exp','TP-Extend-EP',expert('tp-ep'))
    add('ep','EP',expert('ep'))
    add('dp_modulo_exp','DP-Modulo-EP',expert('dp'),True)
    if config['CP'] > 1:
        add('dp_modulo_exp_cp','DP-With-CP-Modulo-EP',expert('dp-cp'),True)
    else:
        for entry in schedule.values():
            if entry['domain']=='DP-Modulo-EP':entry['domain_aliases']=['DP-With-CP-Modulo-EP']
    world=config['TP']*config['CP']*config['DP']*config['PP'];stride=world//config['PP']
    # CP overlap/ring and hierarchical MoE may create additional groups here.
    # They must not invalidate the already corroborated core initialization.
    if config['CP']==1 and not config.get('post_groups_possible',False):
        add('pp_new_stream','PP',[list(range(i,world,stride)) for i in range(stride)])
    return schedule, count


def initialization_roles(rows, profiling, evidence):
    mapping, audit = {}, {'status':'unavailable','reason':'missing metadata/log layout evidence'}
    configurations = {tuple(row['metadata'].get('distributed_args',{}).get(FIELDS[k]) for k in FIELDS) for row in rows}
    if len(configurations)!=1:return mapping,audit
    values=next(iter(configurations))
    if any(not isinstance(v,int) or v<=0 for v in values):return mapping,audit
    config=dict(zip(FIELDS,values));logs=evidence['values']
    world=config['TP']*config['CP']*config['DP']*config['PP']
    if any(row['metadata'].get('distributed_args',{}).get('world_size',world)!=world for row in rows):
        return {},dict(audit,reason='world size conflicts with rank coordinate dimensions')
    if logs.get('use_tp_pp_dp_mapping') not in (True,False):return mapping,audit
    config['order']='tp-pp-dp' if logs['use_tp_pp_dp_mapping'] else 'tp-cp-ep-dp-pp'
    config['split_rank']=logs.get('pipeline_model_parallel_split_rank')
    config['post_groups_possible']=config['CP']>1 or logs.get('moe_hierarchical_alltoallv') is not False
    if any(field in logs and logs[field]!=config[key] for key,field in FIELDS.items()):
        return {},dict(audit,reason='metadata and logged parallel degrees conflict')
    try:schedule,limit=construction_schedule(config)
    except (ValueError,KeyError,IndexError):return mapping,audit
    families=set();checked={};conflicts=[]
    for row in rows:
        for identity,group in row['metadata'].get('parallel_group_info',{}).items():
            alias=str(group.get('group_name',''))
            if not alias.isdigit() or int(alias)>=limit:continue
            entry=schedule.get(alias)
            if entry is None or list(map(int,group.get('global_ranks',[])))!=entry['members']:
                conflicts.append({'rank':row['rank'],'group_id':identity,'alias':alias});continue
            families.add(entry['family']);checked[identity]=(alias,entry)
    if conflicts or len(families)<4 or not {'tp','pp'}<=families:
        return {},dict(audit,reason='initialization fingerprint not uniquely corroborated',conflicts=conflicts,families=sorted(families))
    # Printed membership lists corroborate the coordinate generator independently.
    for key,family in [('tp','tp'),('pp','pp'),('dp_with_cp','dp_cp'),('tp_extend_ep','tp_exp'),('dp_modulo_ep','dp_modulo_exp')]:
        actual=evidence['logged_groups'].get(key)
        expected=[entry['members'] for entry in schedule.values() if entry['family']==family]
        if actual is not None and actual!=expected:
            return {},dict(audit,reason='printed group layout conflicts with initialization schedule',family=family)
    for identity,(alias,entry) in checked.items():
        if entry['domain']:
            mapping[identity]={**entry,'alias':alias,'reason':'MindSpeed initialize_model_parallel full observed alias/member/order fingerprint; '+entry['family']}
    return mapping,{'status':'verified','config':config,'matched_groups':len(checked),'families':sorted(families),'log_file':evidence['log_file'],'roles':mapping}
