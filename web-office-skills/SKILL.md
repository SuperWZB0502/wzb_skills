---
name: web-office-skills
description: Use when installing, operating, building, publishing, packaging or troubleshooting Codex Web Office, including SSH access, runtime model authorization, conversations, company knowledge and office/chat interactions. Not for general website generation.
license: See LICENSE in the distributed package
metadata:
  version: 1.14.2
  owner: HTML大神
---

# Codex Web Office

使用随包固定源码和脚本管理私有多智能体工作台。功能总览见 [中文介绍](README.md)；所有操作命令集中在[脚本工作流](references/workflows.md)，按任务读取相应小节，优先执行脚本，不手工拼装依赖安装、前端复制或 ZIP。

## 首次安装与已有实例

首次安装必须先取得用户明确选择的**工作根目录**，再确认默认界面 `office`（任务动画）或 `chat`（极简对话）。已有指令已选择时直接沿用。当前 shell 目录和 skill 所在目录不能替代用户选择；确认根目录前不创建目录、探测环境、下载或安装。

工作根同时是部署根和所有任务根，状态、项目、缓存、下载、临时文件和产物均留在其中。首次按 workflows 执行 doctor、install、start、pair。遇到已有无关文件或受管路径冲突先检查，不能删除整个目录；端口冲突换空闲端口，不停止无关服务。

已有安装先读取 `workspace.json`，沿用其根与偏好，不重新选择或覆盖安装。启动、状态、配对、停止、日志清理、构建、前端发布、更新/回滚、打包和核验都走对应脚本。仅构建或核验的请求不等于发布授权；在既有授权范围内完成具体工作，不重复索要已给过的选择。

## 环境、访问与凭据

机器需 Node.js 22+（`node:sqlite`）及所选执行器对应的 Codex/Claude Code CLI，doctor 检查至少一个受支持 CLI；包不携带外部 CLI 二进制或登录信息。Claude 使用本机 `CLAUDE_CLI`、PATH 或 `workspace.json` 的 `cliPaths["claude-code"]` 非秘密路径选择；不复制其他机器的路径、启动器、auth/settings 或全局登录。原生会话文件清理目前要求 Linux。

主机保持回环监听，默认提供[独立 SSH 访问](references/access.md)，明确本机与服务器各执行什么；VS Code 转发只是可选方式，不会启动后台。配对码仅按用户要求实时取得和展示，不写报告；API Key 和账户授权码只在专门网页表单输入，不发送到 agent 聊天、命令行、环境模板或持久文件。

Key/OAuth 只在运行期内存，停止/重启需重新提供或授权；浏览器固定 30 天登录独立保留。官方账户模式不接受任意中转 URL，不能把打开网站当授权成功。服务、模型与手动 1M 说明见[模型凭据](references/credentials.md)。

新安装默认 `sandboxed`。仅在真实终端工具遇到沙箱限制时解释原因，由该用户明确选择后才使用 `account`；它使用当前系统账号权限，不能对任意终端程序提供 OS 文件边界。其他实例的授权不能随包继承，不修改全局工具配置或自动建立公开隧道。

## 功能和操作边界

- office/chat 互斥加载，切换或关闭网页不停止后台任务；空闲动画不调用模型。每身份排队，无固定全局任务并发 cap。
- 保留会话隔离、原生线程映射、主动只读上下文及实际读取追踪。删除身份先移入小黑屋且保留配置；最终开除清配置并退出名单，会话留公司知识库。永久删除会话、待清理及显式重试见[服务与会话](references/services-conversations.md)。
- 聊天保持紧凑两行、Enter/Alt+Enter/IME、图片预览、Markdown 代码复制及阅读全屏 Esc 返回；不展示内部推理原文。日常用法见[交互说明](references/interaction.md)。
- 前端发布与运行层升级分开。核心/依赖/数据库不兼容变化需要计划重启；只使用用户提供的可信本地包，不自动拉取任意上游。见[更新说明](references/updates.md)。

## 长期记忆、技能与项目目录

- 安装后工作根会有 `company/rules.md`（全员规章）与 `docs/dev-work-layout.md`（项目详细规范）。规章随员工长期记忆注入；详细规范在创建目录、设置开关、定稿、提交或打包前按需读取。
- 技能库 `skills/` 按 Anthropic 规范组织（方向目录 + SKILL.md）；agent 用 `office_list_skills` / `office_read_skill` 读取，改完跑 `node app/scripts/skills-index.mjs`。
- 开发岗用 `node app/scripts/work-layout.mjs` 生成 `work/<编号-项目>/{code,test,feature}` 骨架；提交以主特性为最小粒度（代码 + 拼接的《特性文档.md》）。
- 备份与迁移：`office.mjs backup`、`office.mjs migrate-layout`；Codex 旧线程路径修复用 `app/scripts/office-repair-codex-paths.mjs`。

阶段产出（同步源码 → 提交 → 打包）：可分发的 skill 由项目代码仓库维护，流程见 [workflows](references/workflows.md) 的「阶段产出：同步源码 → 提交 → 打包」；改完 skill 脚本或文档必须重新 `pack-source` 再 `package`。

## 记录与复用

在工作根 `docs/WORKLOG.md` 记录实际操作、失败、修复、结果和限制，保留历史证据；不写 Key、token、Cookie、授权码或配对码。匹配故障时读[排障参考](references/troubleshooting.md)。

分享前运行 workflows 的隐私扫描、打包与核验；保留许可证和第三方声明，不夹带状态、项目、工作根配置、登录资料、报告中的秘密或机器专属授权。记录实际包哈希与核验范围。脚本结构核验、编译/构建、短模型连通、完整浏览器流程、实际 1M 输入和 clean install 分别报告，不能互相替代；最终结果以本次实际报告为准，不把旧包安装证据套到新包。
