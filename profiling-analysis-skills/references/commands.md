# 命令和产物

以下`SKILL_DIR`为本技能根目录，不是运行命令的当前目录。为每份profiling使用独立输出目录，避免不同任务页面或缓存混用。

## 统一入口

```bash
python3 "$SKILL_DIR/scripts/run_analysis.py" --profiling /data/job/profiling --output-dir /data/reports/job
python3 "$SKILL_DIR/scripts/run_analysis.py" --profiling /data/job --output-dir /data/reports/job --log-dir /data/job/run_log
python3 "$SKILL_DIR/scripts/run_analysis.py" --output-dir /data/reports/job --render-only
python3 "$SKILL_DIR/scripts/run_analysis.py" --output-dir /data/reports/job --validate-only
```

`--features overview|parallel|all`控制需要的功能。`--communication-only`仅影响功能2。`--step`限制采集Step；未提供时不硬编码。`--browser-check`对正常运行/重渲染增加可选浏览器验收，需要Playwright/Chromium：

```bash
python3 -m pip install playwright
python3 -m playwright install chromium
```

`--refresh`重建派生数据；`--reindex`额外重建功能2原始索引。通常使用正常入口让它判断缓存有效性，修改模板用render-only即可。

默认Grad AR模式为gradient；`--grad-ar-mode pp_allreduce`是保留的旧兼容口径，仅在用户明确要求时使用。

## 输出

```text
analysis-result/
  report.html                  两功能统一离线HTML
  workflow.json                Agent优先读取的聚合状态/统计/路径
  summary.md                   聚合摘要
  overview.log / parallel.log  各入口控制台日志
  overview/
    report.html                  本功能独立页面
    workflow.json / summary.md / agent_tasks.json
    report.json / configuration.json / communication_breakdown.json
    overview_step_<step>_time.csv / overview_step_<step>_percent.csv
    validation.json / analysis_manifest.json / cache/
  parallel/
    report.html                  本功能独立页面
    workflow.json / summary.md / agent_tasks.json
    report.json / unified_report.json / unified_summary.json
    domains/{PP,DP,EP,CP,TP,GLOBAL,TP-Extend-EP,DP-Modulo-EP,DP-With-CP,DP-With-CP-Modulo-EP,unresolved}/
    streams/ / timeline_cache/
    phase_markers.json / free_boundaries.json
    operator_details.json / operator_details_validation.json
    presentation_summary.json / validation.json / analysis_manifest.json
```

缓存与载荷文件也在各功能输出中，不放进技能源目录。详细manifest保存输入/代码/产物指纹，正常无需读入Agent上下文。语义不完整的partial可以返回码0；统一入口非零返回表示至少一个所选功能执行失败。

仅渲染需要已有解析结果及其记录的元数据/日志来源可访问；已生成的HTML本身则独立离线。压缩载荷缓存与模板分离，命中解析缓存也会读取最新模板。

## 小范围证据查询

```bash
python3 "$SKILL_DIR/scripts/query_evidence.py" --output-dir /data/reports/job --rank 16 --step 5 --start-us 1640000 --end-us 3620000 --include-details --include-stack --limit 10
python3 "$SKILL_DIR/scripts/query_evidence.py" --output-dir /data/reports/job --unresolved --limit 10
python3 "$SKILL_DIR/scripts/query_evidence.py" --output-dir /data/reports/job --feature overview --rank 16 --step 5 --limit 10
```

默认feature=parallel，时间为公共原点相对us；absolute选项使用绝对us。contained只返回完整落入时间区间的事件。组ID使用完整字符串，name为名称子串。truncated表示结果超过上限，不等于源数据没有其他事件。overview时间查询要求absolute，不能把图中相对区间直接用于它。

## 确认通信语义后的映射

正常不提供映射；只有元数据/调用证据明确且现有规则未覆盖时使用`--domain-roles /path/to/roles.json`。示例：

```json
{
  "communication_group_roles": {"完整组ID": "DP"},
  "evidence": {"完整组ID": {"reason": "确认域身份的依据", "source_file": "/path/to/evidence.json"}}
}
```

两功能共用的基础映射角色为PP、DP、EP、CP、TP、GLOBAL、unresolved；功能2底层另支持四个扩展域。跨功能统一映射只使用共同角色，扩展域优先由内置规则识别。仅做功能1时其底层另支持Grad AR/其他通信。来源及映射文件进入分析指纹；不要因为组人数相同给出映射。

## 单功能底层工具

只在统一入口不覆盖的维护/定位任务中使用。各功能路径下的script可独立运行，帮助信息由`--help`提供。

- `features/overview/script/run_overview.py`：全局总览完整流程。
- `features/parallel/script/run_parallel.py`：并行图完整流程。
- `render_overview.py` / `render_parallel.py`：读取已有JSON和模板生成HTML。
- `operator_details.py`：从导出CSV/JSON重建详情侧文件；`validate_operator_details.py`核对全部引用。
- `phase_markers.py`：从有效设备/框架缓存重建逐层r和Adam opt。
- `script_catalog.py --public-only`：静态脚本能力目录。

修改数据派生规则后正常入口会重新判断代码/输入指纹。adopt-existing仅用于已审核旧产物的迁移，不能用它绕过来源变化检查或宣称未经核对的缓存有效。

## 生成流程与完整性

统一入口自动执行：输入规范化→按CPU/内存/实采卡数决定并发→首次一次原始扫描生成各功能缓存→功能1解析/缓存/校验/模板注入→功能2解析/缓存/校验/模板注入→assemble_report.py拼装本次成功功能→workflow.json和summary.md。普通使用不要求Agent拼接任何HTML。各功能进程使用独立模块路径，避免同名workflow_support串用。

最终HTML只包含本次成功功能；没有成功功能时不发布新HTML，以workflow.rendered为准。部分成功时返回码非0，并明确成功范围；元数据不全的正常partial返回码为0，不阻塞首次交付。

核对包完整性（无需profiling）：

```bash
python3 "$SKILL_DIR/scripts/verify_package.py" --check-hashes
```

仅有已生成的功能页面时也可以用纯Python重拼装，无需原始profiling或模板推理：

```bash
python3 "$SKILL_DIR/scripts/assemble_report.py" \
  --overview /data/reports/job/overview/report.html \
  --parallel /data/reports/job/parallel/report.html \
  --html /data/reports/job/report.html
```

首次无需此额外命令，run_analysis.py会自动调用。手工重拼装前应确认两份功能页面来自同一profiling；正常入口已检查已有产物的一致性。

## 首次解析性能

默认不再固定为2进程：入口结合CPU亲和性、cgroup CPU/内存预算、可回收文件缓存及实采Rank数自动决定并发，最多8个。逐卡工作内存按1GiB+6倍最大trace文件大小粗估；这不是整个任务的内存保证，主进程汇总仍有额外开销。手动`--workers N`可覆盖预算，有效并发以实采卡数为上限。

首次完整分析（包含parallel、非communication-only、未强制refresh/reindex，且所选功能均没有analysis_manifest.json）启用`ingest_trace.py`。它用同一事件流调用两个功能原有提取规则，写入本输出目录下各功能的缓存和`ingest.json`、`ingest_ranks/`；各功能随后读本次刚生成的缓存。没有跨任务或跨输出目录的全局缓存。

单Rank仍顺序扫描，多个Rank并行。GC优化只作用于无环JSON/Node索引构建与加载，退出时恢复；调用链关联阶段不全程关闭GC。轻量Python/DistComm索引避免重复解压全量CPU索引，大缓存分块编码，大型派生JSON紧凑输出。字段、时间精度和调用栈证据不变。

仅重渲染不重新扫描。指定Step通常仍要顺序读取原始JSON，不能把`--step`理解为可以物理跳过其他Step。详细实验及限制见[性能说明](performance.md)。

## HTML体积与内存核对

正常生成自动启用列式压缩，不需要新增参数。已有数据执行统一入口`--render-only`，不重新扫描trace；第一次迁移重新编码载荷，后续命中载荷缓存仍读取最新模板。浏览器支持gzip DecompressionStream即可，正常生成不需要Playwright、psutil或Node。

可选全量无损核对（只读已有产物）：

```bash
python3 "$SKILL_DIR/scripts/features/parallel/script/validate_html_payload.py" \
  --html "/path/to/analysis-result/report.html" \
  --output-dir "/path/to/analysis-result/parallel"
```

可选浏览器性能对比需要额外安装Playwright/Chromium和psutil；这些不是正常解析依赖。用一个新的输出目录执行：

```bash
python3 "$SKILL_DIR/scripts/benchmark_html.py" \
  --case baseline=/path/to/old-report.html \
  --case optimized=/path/to/new-report.html \
  --runs 3 --output-dir /path/to/new-benchmark-directory
```

脚本交替顺序，每次启动独立浏览器；保存HTML大小、首次可操作耗时、JS堆、渲染进程RSS采样、流切换与缩放耗时、截图及默认筛选CSV。实验数据与限制见[HTML性能说明](html-performance.md)。
