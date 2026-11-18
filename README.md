# wzb_skills

存放蒸馏王振邦得到的各种skills，欢迎各位大佬们下载，使用，二次开发。

好用的话，麻烦大佬们帮忙点个star吧，万分感谢，您的支持是我持续更新（不是，蒸馏自己）的动力。

## Skills 目录

| Skill | 说明 | 目录 |
| ----- | ---- | ---- |
| profiling-analysis-skills | Ascend/MindSpore/PyTorch profiling 离线分析，生成全局总览和并行通信图 HTML | [profiling-analysis-skills/](profiling-analysis-skills/) |
| web-office-skills | 把 Codex 和 Claude Code 放进私有网页工作台的多智能体办公室 | [web-office-skills/](web-office-skills/) |

## Skills 介绍

以下按上库先后顺序排列。

### 1. profiling-analysis-skills

把已导出的 Ascend / MindSpore / PyTorch TEXT profiling 生成一份离线交互 HTML，包含「全局总览」和「5D 并行通信图」两个功能。无需把 profiling 下载到本地、也无需启动 msinsight 服务；在数据所在机器运行脚本，打开生成的 HTML 即可查看。数据识别、解析、统计、校验、页面渲染和最终拼装全部由随附 Python 脚本完成，HTML 默认使用无损列式压缩与共享字典以降低浏览器内存。

- **全局总览**：查看实际采集卡数、GBS/MBS/MBN、PP/DP/EP/CP/TP 等配置与规模，按 Stage / 实采卡展示计算、未掩盖通信、掩盖通信、Free 和端到端时间分解，支持表头排序、搜索、行列悬停高亮和 CSV 导出。
- **5D 并行通信图**：在统一时间轴上查看每个 Stage / Rank 的执行过程，计算、通信、拷贝、Free 分行展示；支持通信域筛选、级联复选、调度过程（f/b/r/opt）和键盘交互缩放平移，悬停或单击查看算子详情（输入输出 shape、FLOPs/MFU、收发 Rank、带宽等）。

样例：

![profiling-analysis-skills 样例](profiling-analysis-skills/image.png)

详见 [profiling-analysis-skills/README.md](profiling-analysis-skills/README.md)。

### 2. web-office-skills

把 Codex 和 Claude Code 放进一个私有网页工作台：为伙伴分配职能、服务和模型，在独立会话中协作，并把历史会话整理成公司知识库。基于随包固定版本的 Claw-Empire 源码，部署到使用者自己选择的工作目录。

- **两种界面**：任务动画（office）可见 Boss 办公室、会议室、工作区、休息区、知识库和小黑屋；极简对话（chat）省去动画画布。两种界面互斥加载，后台已提交的任务继续运行。
- **伙伴、任务与会议**：Boss 为固定个人入口，其他伙伴可设为 SE / PL / 开发 / 测试，分别选择名称、外观、模型服务和思考档位；可停止任务，也可组织空闲伙伴召开会议并查看记录。
- **模型服务**：在「我的模型服务」统一管理，明确选择 Codex 或 Claude Code，支持 API Key 与官方账户两种认证；Claude Code 单独提供 1M 长上下文手动勾选项。
- **独立会话与公司知识库**：每位伙伴有独立会话列表，历史和线程映射保存在工作根内、重启保留；知识库按伙伴分组，支持搜索、分类筛选和分段读取。
- **阅读、输入和图片**：回复支持 Markdown 渲染与代码块复制，Enter 发送 / Alt+Enter 换行，支持图片上传、粘贴和拖入（PNG/JPEG/WebP，每张 ≤5 MiB）。

样例：

![web-office-skills 样例](web-office-skills/image.png)

详见 [web-office-skills/README.md](web-office-skills/README.md)。
