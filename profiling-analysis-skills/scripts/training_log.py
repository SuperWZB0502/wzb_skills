"""Read process counts, mesh degrees and batch parameters from training log headers."""
from collections import defaultdict
from pathlib import Path
import re

INIT = re.compile(r'^\[RANK \d+/(\d+)\].*Initializing parallel state\.\.\.\s+(.*)$')
DEGREE = re.compile(r'(?:^|,\s*)([a-z_]+_size)\s+(\d+)')
ARGUMENT = re.compile(r'^\s+(global_batch_size|micro_batch_size|expert_parallel_size|'
                      r'context_parallel_size|pipeline_parallel_size|tensor_parallel_size):\s*(\d+)\s*$')
INIT_KEYS = {'dp_size':'data_parallel_size', 'pp_size':'pipeline_model_parallel_size',
             'cp_size':'context_parallel_size', 'tp_size':'tensor_model_parallel_size'}
PARAMETER_KEYS = {'pipeline_parallel_size':'pipeline_model_parallel_size',
                  'tensor_parallel_size':'tensor_model_parallel_size',
                  'expert_parallel_size':'expert_model_parallel_size'}


def training_log_paths(log_dir):
    return sorted(Path(log_dir).glob('*.log'))


def mesh_training_arguments(log_dir):
    """Return values with line provenance only for logs with a mesh initialization record."""
    arguments=defaultdict(list)
    for file in training_log_paths(log_dir):
        initialization=[]
        parameters=[]
        with file.open(encoding='utf-8',errors='replace') as stream:
            for line_number,line in enumerate(stream,1):
                init=INIT.match(line)
                if init:
                    fields=dict(DEGREE.findall(init[2]))
                    if {'dp_size','dp_shard_size','dp_replicate_size'}<=fields.keys():
                        initialization.append(('world_size',int(init[1]),line_number))
                        initialization.extend((key,int(fields[field]),line_number)
                                              for field,key in INIT_KEYS.items() if field in fields)
                elif parameter:=ARGUMENT.match(line):
                    key=PARAMETER_KEYS.get(parameter[1],parameter[1])
                    parameters.append((key,int(parameter[2]),line_number))
        if initialization:
            for key,value,line_number in (*initialization,*parameters):
                if value>0:
                    arguments[key].append((value,{'file':str(file),'line':line_number,
                                                 'type':'mesh_training_log','key':key,'value':value}))
    return arguments
