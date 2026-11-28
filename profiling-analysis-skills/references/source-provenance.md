# 来源和维护

当前运行代码来自用户已完成的“msinsight调研”功能1/功能2拆解目录。打包的原脚本和HTML模板按功能保持隔离，逐文件来源及SHA256见[source_manifest.json](source_manifest.json)。脚本生成数据和页面，不需要原调研目录或msinsight服务参与执行。

原工具主要来源包括：

- msprof_analyze Step耗时统计字段与区间口径；msinsight MetaDataParser、EventParser、TextTraceDatabase的组/事件读取方式。
- msinsight AscendUnit、colorUtils、light主题的事件名称哈希及色板。
- msinsight KernelParse.cpp/h中ParseShapeInfoData、ParseStartTimeInfoData及Input/Output Shapes、Data Types、Formats字段规则。
- CommunicationRapidSaxHandler.cpp、SingleRankCommunicationJsonParser.cpp的通信时间/带宽读取；CommunicationMatrixRapidHandler.cpp的端点、传输类型、统计字段读取。
- MindSpore/CANN已知导出字段、flow与TLV schema；仅在已确认格式和证据边界内使用。

独立 Python 脚本封装上游字段、确定性算法与原始证据链。PP 载荷证明、micro/r/opt、级联筛选、二维框选的泳道重组及 HTML 编排由本项目适配。

原msinsight代码采用Mulan PSL v2；上游仓库为https://gitcode.com/Ascend/msinsight 。本技能没有打包整个上游仓库、开发样例profiling、原始trace或历史分析缓存。需要核对原工具时按上述文件/函数定位；相应源码快照和更长的研发审计保留在原调研工作区。

维护时修改技能内 `scripts/features` 各功能的 Python 和 HTML，再用统一入口生成和核对产物。纯样式或交互修改走 `--render-only`。各功能在独立进程加载。

功能3由本项目按 MindStudio Insight 算子页的可见行为和 `server/src/modules/operator` 的字段和分组语义实现。Python 直接读取 TEXT 或兼容的 Ascend DB，HTML 负责本地显示与 CSV 下载。

最新包包含扩展域注册表、MindSpeed通信组构建规则、轻量语义刷新、算子性能公式和统一详情JS。MindSpeed构建规则来源于megatron/core/parallel_state.py的RankGenerator及initialize_model_parallel，已转成不依赖模型仓库/torch的纯Python规则。source_manifest.json的source路径只说明开发来源，不是运行时加载路径。

技能本地的run_analysis.py、assemble_report.py、verify_package.py及query_evidence.py也列入文件指纹。维护后需要更新清单中对应SHA256；常规生成只检查文件完整性，显式--check-hashes可核对发布版本。首次只执行脚本即可生成页面；Agent推理用于用户后续改进、debug和新增适配。

性能优化工程包括一次原始扫描、分块JSON缓存、小型派生索引、局部GC控制和资源并发预算。它们封装既有提取规则，未改变时间精度、通信语义或字段来源。后端处理和模板与开发工作目录同步；性能实验及核对摘要见performance.md和performance-results.json，运行时不读取实验目录。

HTML列式压缩由本项目独立实现，位于compact_timeline.py/js与timeline_payload.py。它只改变HTML传输和浏览器内存布局，不改上游通信归域、算子关联、统计口径或canonical JSON/CSV。时间使用精确十进制字符串的可逆整数编码；绘图浮点值与旧版Number(原始时间文本)一致。validate_html_payload.py可逐条与本次canonical产物核对，benchmark_html.py用于可选浏览器实测。

## 时间轴交互源码

适配基于 MindStudio Insight 提交 `2cd7b16e2d77d7f2bf15ae7e1bee0f112f108e79`。

- `modules/timeline/src/components/TimelineMarker.tsx` 的 `FLAG_PATH`、`transformTimeToLeft`、`addNewFlag` 提供原始旗形、时间投影和点标记排序。默认色板来自 `modules/timeline/src/entity/timeMaker.tsx`。
- `modules/timeline/src/components/charts/ChartInteractor/actions.ts` 的双端点选区与按钮命中规则，以及 `modules/timeline/src/actions/actionZoomIntoSelection.ts`、`actionZoom.ts` 的选区放大、回退和重置，接入报告既有的二维泳道过滤。区间命名、单击和双击区分、按钮四角避让及 Esc 连按由本项目扩展。
- `server/src/modules/defs/DbSqlDefs.h` 的 `fwdbwd`、`async_npu` 分类及 `server/src/modules/timeline/core/dataEngine/PythonApiRepo.cpp` 的序列字段为逐层标注提供依据。TEXT 适配器结合模型显式层标记、原始 flow 和设备事件，保留无法归属的区间。

生成和验证固定版本的旗形资源。

```bash
python3 "$SKILL_DIR/scripts/features/parallel/script/sync_mindstudio_timeline.py" /path/to/msinsight
python3 "$SKILL_DIR/scripts/features/parallel/script/sync_mindstudio_timeline.py" /path/to/msinsight --check
```

运行报告生成时使用随包的资源，无需下载或构建 MindStudio。逐层归属缓存在 `parallel/layer_markers.json` 并嵌入离线 HTML。
