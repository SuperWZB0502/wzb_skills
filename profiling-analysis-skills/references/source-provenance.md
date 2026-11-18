# 来源和维护

当前运行代码来自用户已完成的“msinsight调研”功能1/功能2拆解目录。打包的原脚本和HTML模板按功能保持隔离，逐文件来源及SHA256见[source_manifest.json](source_manifest.json)。脚本生成数据和页面，不需要原调研目录或msinsight服务参与执行。

原工具主要来源包括：

- msprof_analyze Step耗时统计字段与区间口径；msinsight MetaDataParser、EventParser、TextTraceDatabase的组/事件读取方式。
- msinsight AscendUnit、colorUtils、light主题的事件名称哈希及色板。
- msinsight KernelParse.cpp/h中ParseShapeInfoData、ParseStartTimeInfoData及Input/Output Shapes、Data Types、Formats字段规则。
- CommunicationRapidSaxHandler.cpp、SingleRankCommunicationJsonParser.cpp的通信时间/带宽读取；CommunicationMatrixRapidHandler.cpp的端点、传输类型、统计字段读取。
- MindSpore/CANN已知导出字段、flow与TLV schema；仅在已确认格式和证据边界内使用。

独立Python脚本并非原仓库同名脚本的直接运行；它们是对字段、确定性算法与真实证据链的封装。PP载荷证明、micro/r/opt、级联筛选、Ctrl框选重组泳道与时间窗、HTML编排等包含独立扩展。原数据缺失不会由这些扩展补造。

原msinsight代码采用Mulan PSL v2；上游仓库为https://gitcode.com/Ascend/msinsight 。本技能没有打包整个上游仓库、开发样例profiling、原始trace或历史分析缓存。需要核对原工具时按上述文件/函数定位；相应源码快照和更长的研发审计保留在原调研工作区。

维护时修改技能内scripts/features各功能的Python/HTML源文件，再用统一入口生成和核对产物。纯样式/交互修改走render-only；数据规则变化按来源和代码指纹重建。保持两功能的独立进程加载方式，避免workflow_support等同名模块串用。

最新包包含扩展域注册表、MindSpeed通信组构建规则、轻量语义刷新、算子性能公式和统一详情JS。MindSpeed构建规则来源于megatron/core/parallel_state.py的RankGenerator及initialize_model_parallel，已转成不依赖模型仓库/torch的纯Python规则。source_manifest.json的source路径只说明开发来源，不是运行时加载路径。

技能本地的run_analysis.py、assemble_report.py、verify_package.py及query_evidence.py也列入文件指纹。维护后需要更新清单中对应SHA256；常规生成只检查文件完整性，显式--check-hashes可核对发布版本。首次只执行脚本即可生成页面；Agent推理用于用户后续改进、debug和新增适配。

性能优化工程包括一次原始扫描、分块JSON缓存、小型派生索引、局部GC控制和资源并发预算。它们封装既有提取规则，未改变时间精度、通信语义或字段来源。后端处理和模板与开发工作目录同步；性能实验及核对摘要见performance.md和performance-results.json，运行时不读取实验目录。

HTML列式压缩由本项目独立实现，位于compact_timeline.py/js与timeline_payload.py。它只改变HTML传输和浏览器内存布局，不改上游通信归域、算子关联、统计口径或canonical JSON/CSV。时间使用精确十进制字符串的可逆整数编码；绘图浮点值与旧版Number(原始时间文本)一致。validate_html_payload.py可逐条与本次canonical产物核对，benchmark_html.py用于可选浏览器实测。
