---
name: wzb-profiling-analysis
description: 分析已导出的 Ascend、MindSpore、PyTorch Profiling，生成全局总览、并行通信图和中文算子详情的离线 HTML。可单独分析 TEXT 算子 CSV 或兼容的 Ascend Profiling DB，也可查询已有报告的时间区间、算子和通信证据。
---

# Profiling分析

统一 Python 入口完成输入识别、解析、校验和离线 HTML 拼装。Agent 执行入口并读取结果状态。页面样式、交互和默认项由技能内的模板决定。

## 生成报告

`SKILL_DIR` 为本 SKILL.md 所在目录，入口可在任意工作目录执行。需要 Python 3.10+ 和 ijson，支持 Linux 和 Windows。

```bash
python3 -m pip install -r "$SKILL_DIR/scripts/requirements.txt"
```

获得用户profiling路径后直接执行：

```bash
python3 "$SKILL_DIR/scripts/run_analysis.py" \
  --profiling "/path/to/profiling-or-job" \
  --output-dir "/path/to/analysis-result"
```

默认将全局总览、训练并行时间轴和算子详情合并为 `analysis-result/report.html`。支持任务目录、逐卡采集目录和含单个 Device_id 的平铺 `ASCEND_PROFILER_OUTPUT`。平铺导出所需的输入副本由脚本自动准备，卡号依据写入结果状态。每份输入使用独立输出目录，重复执行沿用该目录。

用 `--features overview|parallel|operator` 运行单个功能。`--step 5` 限制 Step，缺省使用全部 Step。用 `--html /path/to/report.html` 指定页面位置。平铺导出旁的 `logs/` 可自动发现，另存时使用 `--log-dir /path/to/logs`。`--workers N` 可为前两个功能覆盖自动并发数。

首次完整分析原始 trace 扫描一次，供全局总览和并行通信图共用。只需算子详情或输入是算子 CSV、Ascend Profiling DB 时，使用 `--features operator`。算子详情读取 kernel 明细或 DB，页面按卡提供四种分组和全量 CSV。统一入口校验数据，并按固定顺序拼装本次成功的功能页面。

## 交付

读取输出根目录的 `workflow.json` 和 `summary.md`。交付根目录的 `report.html` 和实际限制。拼装入口会清理本机输入、日志和输出路径。保留工作流和分析文件用于复现。

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

- [技能介绍和功能预览](README.md)：向用户展示用途和实际界面时读取。

- [功能和固定交互](references/features.md)：当前页面、配色及交互规则。
- [数据口径](references/data-semantics.md)：字段来源、未知项及性能统计边界。
- [命令和产物](references/commands.md)：缓存、查询、单功能维护及可选浏览器检查。
- [性能说明和实测](references/performance.md)：一次扫描、并发预算、内存开销与独立目录实验。
- [来源](references/source-provenance.md)与[文件指纹](references/source_manifest.json)：脚本和模板维护依据。
