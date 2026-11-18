"""Deterministic, source-backed logical FLOPs and per-rank model peak capacity.

References: msprof_analyze operator_mfu/{operator_flops,chip_peak_flops,
mfu_calculator}.py, called by msinsight ClusterFileParser::MsprofAnalyzeOperatorMfu.
FMA=2. Shape-derived FLOPs are analytical values, not PMU measurements.
Unknown sparse masks, fused internals and ambiguous transpose shapes stay unknown.
"""
import json
import math
from pathlib import Path
import re


def tensor_shapes(raw):
    text = str(raw or '').strip().strip('"')
    if text in ('', 'N/A', 'None'):
        return []
    return [tuple(map(int, slot.split(','))) if re.fullmatch(r'\d+(?:\s*,\s*\d+)*', slot.strip()) else None
            for slot in text.split(';')]


def flops_for_record(record):
    inputs = tensor_shapes(record.get('Input Shapes'))
    outputs = tensor_shapes(record.get('Output Shapes'))
    kind = record['type']
    unknown = {'flops': None, 'status': 'unknown', 'basis': '该算子没有已确认的计算量规则或缺少必要shape/执行语义'}
    if record.get('exported_flops') is not None:
        return {'flops': record['exported_flops'], 'status': 'exported', 'basis': 'kernel_details.csv显式FLOPs字段'}
    def known(value, basis):
        return {'flops': value, 'status': 'shape_formula', 'basis': basis}
    nd = all(v.strip() == 'ND' for v in str(record.get('Input Formats', '')).split(';')[:2]) and str(record.get('Output Formats', '')).split(';')[0].strip() == 'ND'
    if kind in ('MatMul', 'MatMulV2', 'MatMulV3', 'BatchMatMul', 'BatchMatMulV2') and nd and len(inputs) >= 2 and outputs:
        a, b, c = inputs[0], inputs[1], outputs[0]
        if a and b and c and min(len(a), len(b), len(c)) >= 2:
            # Accept only a unique contraction dimension consistent with both inputs
            # and the actual output, without guessing transpose attributes.
            contractions = set()
            for ta in (False, True):
                for tb in (False, True):
                    m, k = a[-2:][::-1] if ta else a[-2:]
                    kb, n = b[-2:][::-1] if tb else b[-2:]
                    try:
                        batch = broadcast_batch(a[:-2], b[:-2])
                    except ValueError:
                        continue
                    if k == kb and c == batch+(m, n):
                        contractions.add(k)
            if len(contractions) == 1:
                return known(2*math.prod(c)*next(iter(contractions)), 'GEMM：2×output_elements×K；输入/输出shape唯一确认K及batch，FMA=2')
    if kind in ('SparseFlashMla', 'SparseFlashMlaGrad', 'FlashAttentionScore', 'FlashAttentionScoreGrad'):
        return {**unknown, 'basis': 'Attention FLOPs还依赖稀疏索引/有效序列长度/mask及重算语义；仅凭当前shape不能唯一确认'}
    if kind == 'GroupedMatmul' and len(inputs) >= 2 and outputs:
        a, b, c = inputs[0], inputs[1], outputs[0]
        if a and b and c and len(a) == 2 and len(b) == 3 and len(c) == 2:
            contractions = set()
            for ta in (False, True):
                for tb in (False, True):
                    m, k = a[::-1] if ta else a
                    kb, n = b[-2:][::-1] if tb else b[-2:]
                    if k == kb and c == (m, n):
                        contractions.add(k)
            if len(contractions) == 1:
                return known(2*math.prod(c)*next(iter(contractions)), 'GroupedGEMM：各组共享K/N，sum(M_group)=实际tokens；2×output_elements×K，不假定组间负载均衡')
    zero = {'ZerosLike', 'OnesLike', 'Fill', 'Assign', 'Transpose', 'TransData', 'Cast', 'ViewCopy',
            'Slice', 'StridedSlice', 'StridedSliceV3', 'ConcatD', 'TensorMove', 'MemSet', 'BroadcastTo',
            'GatherV2', 'GatherElements', 'GatherElementsV2', 'Equal', 'NotEqual', 'GreaterEqual',
            'Less', 'LogicalOr', 'ReduceAll', 'NonZero', 'ArgMaxWithValue', 'Index', 'IndexCheck'}
    if kind in zero:
        return known(0, '浮点数学操作口径：搬运/填充/转换/比较/索引不计浮点算术')
    output = outputs[0] if outputs else None
    types = str(record.get('Output Data Types', '')).upper()
    floating = any(token in types for token in ('FLOAT', 'BF16', 'HALF', 'DOUBLE'))
    if output is not None and floating:
        elements = math.prod(output)
        basic = {'Add', 'Sub', 'Mul', 'Muls', 'AddV2', 'Neg', 'Exp', 'Log', 'Sqrt', 'Rsqrt',
                 'Sin', 'Cos', 'Pow', 'Reciprocal', 'RealDiv', 'Div', 'Square', 'Maximum', 'Minimum'}
        if kind in basic:
            if kind in ('Maximum', 'Minimum'):
                return known(0, '浮点比较不计浮点算术')
            return known(elements, '逐元素数学操作：output_elements；exp/log/div/sqrt等各按1次逻辑操作计')
        if kind in ('ReduceSum', 'ReduceMean') and inputs and inputs[0] is not None:
            return known(max(0, math.prod(inputs[0])-elements)+(elements if kind == 'ReduceMean' else 0), '归约加法input_elements-output_elements；ReduceMean额外output_elements次除法')
    return unknown


def broadcast_batch(a, b):
    width = max(len(a), len(b))
    aa, bb = (1,)*(width-len(a))+a, (1,)*(width-len(b))+b
    if any(x != y and x != 1 and y != 1 for x, y in zip(aa, bb)):
        raise ValueError('incompatible batch')
    return tuple(max(x, y) for x, y in zip(aa, bb))


def hardware_files(report):
    return sorted({p for source in report.get('sources', [])
                   for p in Path(source['trace']).parent.parent.glob('PROF_*/device_*/info.json.*')
                   if re.fullmatch(r'info\.json\.\d+', p.name)})


def collect_hardware(report):
    result = {}
    for source in report.get('sources', []):
        candidates = []
        for path in sorted(Path(source['trace']).parent.parent.glob('PROF_*/device_*/info.json.*')):
            if not re.fullmatch(r'info\.json\.\d+', path.name):
                continue
            info = json.loads(path.read_text())
            for device in info.get('DeviceInfo', []):
                try:
                    cores, mhz = int(device['ai_core_num']), float(device['aic_frequency'])
                except (KeyError, ValueError, TypeError):
                    continue
                if cores > 0 and math.isfinite(mhz) and mhz > 0:
                    candidates.append({'aicore_count': cores, 'frequency_mhz': mhz,
                                       'peak_flops': cores*mhz*1e6*8192,
                                       'source': str(path), 'basis': 'msprof_analyze BF16/FP16 Cube：AICore×MHz×10^6×8192；模型归一化峰值'})
        unique = {(v['aicore_count'], v['frequency_mhz']) for v in candidates}
        result[str(source['rank'])] = candidates[0] if len(unique) == 1 else {
            'peak_flops': None, 'basis': '缺少有效硬件核心数/频率，或同rank多份硬件信息冲突'}
    return result
