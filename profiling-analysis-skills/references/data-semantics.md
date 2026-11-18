# 数据口径和证据边界

## 输入

支持已导出的Ascend TEXT文件树：每卡目录带profiler_info、profiler_metadata及ASCEND_PROFILER_OUTPUT。功能1以step_trace_time.csv为基础，通信分类和功能2需要trace_view.json与组元数据。算子详情读取kernel_details.csv、communication.json、communication_matrix.json。

原始PROF_、单独DB、非支持的二进制输入不直接伪装成TEXT；需要真实导出或新增适配。缺少某份导出时，相应字段保留未知。输入路径、采集rank及任务规模不能硬编码为开发样例。

配置优先取真实元数据及Megatron训练日志。MBN=GBS/(DP×MBS)，非整数、配置冲突或batch rampup不强行推断。EP不作为world_size的额外乘数；实际采集卡数与总配置卡数分别显示。

## 时间与总览

内部JSON以微秒保存原始精度。TEXT step_trace_time.csv按其已确认格式读取微秒；旧兼容统计CSV可能使用毫秒，不能混用。页面显示默认秒、两位小数，CSV随相应模式/单位导出。

- 端到端=计算+未掩盖通信+Free。
- 总通信=未掩盖通信+掩盖通信；掩盖通信与计算重叠，不能再把全部时间项相加。
- 单Step信息块为实采卡端到端时间最大值，固定秒；不是跨卡求和。
- 图中视窗为实测Step公共时间范围，可能包含Preparing；它与单卡端到端最大值不必相等。
- 百分比的分母为当前行端到端时间；层数、重算、显存不转百分比。
- bubble=PP+Grad AR为用户自定义估算，不等同原工具实测空闲Bubble。有效耗时=端到端−bubble；理论bubble=(PP−1)/(MBN+PP−1)，适用于理想模型。

显存来自采集结束附近日志的max allocated累计峰值，非本Step独立峰值；日志MB口径按已确认的MiB换为GiB。缺少对应日志时保持未知，不用后续迭代峰值顶替。

## 通信域、端点和性能

组ID是通信组主键。DP/EP可成员相同但组ID和语义不同；只凭组大小不能分类。使用元数据语义、明确域映射及有证据的runtime上下文。GLOBAL要求真实world语义与完整成员。用途与通信域分开：只把梯度上下文确认的AllReduce计入Grad AR，不把所有AllReduce或PP日志通信当成梯度同步。

communication.json按rank、Step、完整算子名、持续时间及导出起点唯一关联。Start Timestamp的epoch浮点导出允许最多0.5微秒量化误差；不使用最近时间猜测。原始MB/GB/s标注及原值保留，协议payload_bytes单独作为字节证据。分传输类型显示，不把包含等待的算子时长当作链路传输时间或据此反推带宽。

communication_matrix.json可能只有top/middle/bottom/total汇总。仅在op_name明确、Step相同、组后缀相同的情况下，附加属于该次算子的部分链路；不能把total应用到每次调用。全局/组内rank解释不唯一时保留原始端点及未知全局rank。集合通信不强造一个发送/接收对；PP载荷端点继续使用已确认的协议证据。

“没有非零带宽记录”与文件缺失不同；接收端可能仅记录等待，而载荷证据来自对端。明细同时提供原始链路通信量和载荷字节，避免混为一个数字。

## 设备流、调度和调用栈

计算流选择本卡真实Ascend Hardware主计算stream；Copy含主stream和独立stream上的实际内存操作。Free来自Overlap Analysis原始区间，可能与拷贝重叠；不由图中白色背景推断。多流分行是为显示重叠，不能把各流时长相加解释成端到端。

f/b灰框引用已验证的框架与设备交集，用于调度示意，其显示跨度不等于纯设备计算量。micro编号要求有足够的调用顺序/显式证据及MBN一致性；不完整、交错或未支持的调度保持未知。

逐层r使用同一backward中的checkpoint backward→custom_forward记录，按独立层重执行分段。优先用flow确认的设备事件首尾；只有框架证据的段明确注明，不把不同层合成一个大重计算区间。opt取主计算流中真实Adam算子的首尾包络，包含内部间隙，不使用异步CPU optimizer.step定位，也不把包络当纯Adam时长。

Python栈沿原始因果flow、可靠connection_id及线程包含关系建立，到进入msadapter前一层为止。缺少flow时可显示部分链，不能按时间接近补造调用栈。MindSpore TLV诊断仅覆盖明确支持的2.10.0 schema；其他版本不会据此断言flow无效。

## 计算输入输出

字段规则参考msinsight的KernelParse::ParseShapeInfoData：Input/Output Shapes、Data Types、Formats。按同rank精确起点、名称及可用Stream/Task ID唯一关联CSV；持续时间仅允许CSV末位量化误差，最多0.001微秒（1ns）。保存来源CSV行号，I/O描述去重。

导出只包含张量元数据，不包含真实Tensor数值。某个计算事件可以关联到CSV却仍没有shape/dtype字段；这属于采集缺失，不通过Agent恢复。

## 扩展域与共享组

扩展域的构建规则封装在`mindspeed_domains.py`，来源为MindSpeed-MM-XY `megatron/core/parallel_state.py`的RankGenerator及initialize_model_parallel。运行时不导入torch、不读取模型源码。成员、顺序和多组初始化证据一致时才采用构建身份；证据不充分仍保留unresolved，不按编号或组大小单独猜测。TP-Extend-EP与EP是不同语义，不能仅因成员重合合并。

CP=1等情况下多个域可能复用同一物理通信组。`domain_aliases`保留等价域；多域同时选择时只显示/统计一次原始事件。未知通信也纳入统一图，不因无法分类而丢失。

## 带宽显示状态

以原始导出GB/s数值为准，不以算子耗时或协议载荷重算。小于0.01的非零数用三位有效数字，避免显示成0.00；原始零值标注「采集值为0」，该值可能受导出精度或统计口径影响，不代表物理链路带宽为零。已有统计字段但无有效传输及可展示链路样本时标注「未记录有效传输」；这与字段缺失`-`不同，也不证明对端未发送。详情、明细和CSV一致。

## FLOPs与MFU

计算量优先读取有效导出FLOPs，否则仅对有明确shape/dtype和已实现公式的算子推导，记录依据；不支持的算子保持未知。硬件型号/峰值无法确认时不显示推测MFU。

单算子MFU=已知FLOPs/(算子执行秒数×本卡已确认BF16/FP16峰值)。区间MFU=区间内完整计算算子的已知FLOPs之和/(区间秒数×涉及实采卡峰值之和)，不相加算子百分比，不外推未采集卡。区间有未知项或跨边界事件时显示部分下界，不把未知按零宣称完整统计。
