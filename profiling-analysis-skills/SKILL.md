---
name: wzb-profiling-analysis
description: 用于分析已导出的Ascend、MindSpore、PyTorch TEXT profiling，生成全局总览和并行通信图的离线HTML，或查询已有报告中的算子、时间区间、I/O、通信域和调用栈证据。
---

# Profiling分析

使用随附Python入口完成输入识别、数据解析、校验、模板注入、功能拼装和HTML交付。首次使用不需要Agent阅读源码、识别通信域、生成前端或手工拼接HTML，脚本不调用模型服务。页面样式、颜色、交互、默认项和水印由打包的源模板决定。

## 生成报告

`SKILL_DIR`为本SKILL.md所在目录，入口可在任意工作目录执行。需要Linux、Python 3.10+、ijson；缺依赖时执行：

```bash
python3 -m pip install -r "$SKILL_DIR/scripts/requirements.txt"
```

获得用户profiling路径后直接执行：

```bash
python3 "$SKILL_DIR/scripts/run_analysis.py" \
  --profiling "/path/to/profiling-or-job" \
  --output-dir "/path/to/analysis-result"
```

默认生成全局总览、并行通信图和`analysis-result/report.html`。支持任务目录、profiling目录、单卡采集目录及`ASCEND_PROFILER_OUTPUT`。每份输入使用独立输出目录；重复执行沿用该目录。输入路径缺失时询问用户，不使用开发样例代替。

按用户已提供的信息添加参数：`--step 5`限制Step；`--log-dir /path/to/run_log`补充日志；`--workers N`手动控制逐卡并发（省略则自动选择）；`--html /path/to/report.html`指定报告位置；`--features overview|parallel`只执行一个功能。默认全Step、两个功能；并发按CPU配额、可用内存和实采卡数自动选择，最多8进程，`--workers N`可手动指定；配置从元数据和日志读取，不根据目录名猜测。

首次完整分析先用一次原始trace扫描供给两个功能各自的缓存，再自动执行必要数据校验，并用Python把本次成功的功能页面按固定顺序拼装为新HTML，不读取旧目标HTML作为素材。Agent只执行命令、读取状态、返回链接；不用另写页面或合并代码。

## 交付

读取输出根目录的`workflow.json`、`summary.md`，交付其中的HTML路径及实际限制。无需把大体积JSON或原始trace读入上下文。

- `complete`：正常交付。
- `partial`且返回码为0：数据有缺失或语义未知，报告已经自动生成，可直接交付并说明限制；首次生成不需要Agent先解决未知项。
- 返回码非0：读取`overview.log`、`parallel.log`和workflow中的错误。若部分功能成功，只交付本次生成的部分报告并说明失败范围；`rendered=false`时不能把旧HTML当成本次新结果。

纯页面更新或重用已有解析结果：

```bash
python3 "$SKILL_DIR/scripts/run_analysis.py" \
  --output-dir "/path/to/analysis-result" --render-only
```

新生成的HTML默认使用无损列式压缩与共享字典，保留全部事件及原始时间文本；不需要Agent选择压缩算法或改写页面。已有结果执行上面的`--render-only`即可迁移，首次迁移会重建HTML载荷缓存。详见[HTML体积和浏览器内存](references/html-performance.md)。

不要默认使用`--refresh`或`--reindex`，也不要重复启动尚未结束的解析。首次解析与缓存重渲染耗时分开说明；运行进度见各功能的`progress.jsonl`。HTML离线可用，无CDN；浏览器需支持DecompressionStream。正常生成不需要Playwright。

## 后续查询、改进和排错

用户需要定位某段时间、解释异常或查看证据时，再运行有数量上限的查询：

```bash
python3 "$SKILL_DIR/scripts/query_evidence.py" \
  --output-dir "/path/to/analysis-result" --feature parallel \
  --rank 16 --step 5 --start-us 1640000 --end-us 3620000 \
  --include-details --include-stack --limit 10
```

时间默认是图中公共原点的相对微秒，`--absolute`改用绝对微秒。仅查未知域用`--unresolved`。Agent可结合这些小范围证据分析脚本处理不了的新格式、语义冲突或用户提出的优化；确定规则后修改随附源脚本或模板，再重新运行入口。不能只改某一次生成的HTML。

缺少数据保持未知，不造shape、带宽、调用栈或通信域。通信组不能只按卡数判断；未确认事件仍保留在图中。模板/脚本缺失时报具体文件，按发布包修复；正常执行不要求Agent重绘界面。包完整性检查：`python3 "$SKILL_DIR/scripts/verify_package.py" --check-hashes`。

需要时读取：

- [技能介绍和两个功能预览](README.md)：向用户展示用途和实际界面时读取。

- [功能和固定交互](references/features.md)：当前页面、配色及交互规则。
- [数据口径](references/data-semantics.md)：字段来源、未知项及性能统计边界。
- [命令和产物](references/commands.md)：缓存、查询、单功能维护及可选浏览器检查。
- [性能说明和实测](references/performance.md)：一次扫描、并发预算、内存开销与独立目录实验。
- [来源](references/source-provenance.md)与[文件指纹](references/source_manifest.json)：脚本和模板维护依据。