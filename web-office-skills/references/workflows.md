# 脚本工作流

本文件集中维护操作命令；功能介绍见 [README](../README.md)，访问方法见 [access](access.md)。优先使用随包脚本，记录实际输出；不要用临时 shell 拼装替代安装、构建、发布或打包流程。

下面的 `SKILL_DIR` 表示当前完整 skill 的绝对目录，`WORKSPACE` 表示**用户已明确选择的绝对工作根**，`OFFICE_MODE` 为 `office` 或 `chat`，`OFFICE_PORT` 为所选端口（默认 18080）。它们都是使用者自己的值，示例不能代替选择。其余 `STAGED_BUILD`、`LOCAL_RELEASE`、`DELIVERY_ZIP`、`PREVIOUS_RELEASE` 均取自本次实际产物或运行记录，不猜文件名。

## 首次安装

先确认工作根与界面，之后才执行环境检查。机器需要 Node.js 22+（含 `node:sqlite`）以及至少一个已安装的 Codex/Claude Code CLI；每个模型服务需要其对应 CLI。包不携带这些外部二进制或账户登录。

```bash
node "$SKILL_DIR/scripts/doctor.mjs" --target "$WORKSPACE" --port "$OFFICE_PORT"
node "$SKILL_DIR/scripts/install.mjs" --target "$WORKSPACE" --mode "$OFFICE_MODE" --port "$OFFICE_PORT"
```

安装器核对随包源码清单/哈希、在根内安装依赖并构建两个入口，非秘密偏好写入 `workspace.json`。遇到受管路径已存在时不覆盖、不删除整个工作根；已有实例走下面的生命周期流程。端口被无关服务占用时选择空闲端口。

CLI 路径来自本机配置和 PATH；Claude 也可用 `CLAUDE_CLI` 或本机 `workspace.json` 的 `cliPaths["claude-code"]`。这些路径不是模型凭据，不随分享包复制，不读取他人 auth/settings。

## 启动、配对与日常维护

这些命令在服务器执行，复用已有 `workspace.json`。`start` 对健康实例返回现有结果，不重复启动；配对码只即时展示，不保存到报告。

| 目的                       | 命令                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------ |
| 服务器重启后恢复工作台     | `bash "$WORKSPACE/restart-office.sh"`                                                       |
| 启动                       | `node "$WORKSPACE/scripts/office.mjs" start --root "$WORKSPACE"`                           |
| 查看运行状态               | `node "$WORKSPACE/scripts/office.mjs" status --root "$WORKSPACE"`                          |
| 生成网页配对码             | `node "$WORKSPACE/scripts/office.mjs" pair --root "$WORKSPACE"`                            |
| 清理指定天数之前的运行日志 | `node "$WORKSPACE/scripts/office.mjs" logs-prune --root "$WORKSPACE" --older-than-days 14` |
| 停止                       | `node "$WORKSPACE/scripts/office.mjs" stop --root "$WORKSPACE"`                            |

日志清理不用于删除项目。停止会结束受管任务并清除内存 Key/OAuth；历史、非秘密配置和未过期的浏览器登录保留。重启后在网页重新提供 Key 或完成账户授权。SSH/VS Code 转发和配对码用法见 [access](access.md)。

## 构建与仅前端发布

```bash
node "$SKILL_DIR/scripts/build.mjs" --target "$WORKSPACE"
```

可加 `--out "$WORKSPACE/artifacts/UNIQUE_BUILD_DIRECTORY"`，输出目录必须全新或空，不能放进 app、状态、项目、源码等受管树。默认输出为根内 `artifacts/frontend-builds/build-UUID`；以脚本 JSON 返回的实际目录为准。

build 执行类型检查、暂存 Vite 构建和入口隔离检查，生成 `.office-build-receipt.json`，记录产物哈希、后端源码指纹和可用的运行身份。它**只构建，不激活**。不要把普通 Vite 目录冒充带完整 receipt 的可发布产物。

用户已经要求发布且检查暂存结果后，使用本次返回的目录：

```bash
node "$SKILL_DIR/scripts/publish-frontend.mjs" --target "$WORKSPACE" --build "$STAGED_BUILD"
```

发布脚本验证 receipt/文件哈希、当前后端与运行实例，确认实际从内建 `app/dist-office` 提供前端；条件不匹配会拒绝，不能跳过检查手工复制。它先备份到根内 `artifacts/frontend-backups/`，先写新 assets、再原子替换入口 HTML，保留旧 hashed assets，核对 HTTP 与 PID/boot 身份；不重启服务或复制状态。失败恢复本次覆盖的前端文件。需要其他发布机制时按脚本拒绝原因转到受支持的 update 路径，不能将后端变更当作前端热更。

## 本地更新包与回滚

| 目的                 | 命令                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------------- |
| 应用可信本地 release | `node "$WORKSPACE/scripts/office.mjs" update --root "$WORKSPACE" --bundle "$LOCAL_RELEASE"`       |
| 切回已保留 release   | `node "$WORKSPACE/scripts/office.mjs" rollback --root "$WORKSPACE" --release "$PREVIOUS_RELEASE"` |

包由用户明确提供并保存在所选根内，不自动拉取未知上游。以真实控制接口返回的激活记录为准，生成命令或离线 fixture 不算发布成功。核心、依赖、Node 或不兼容 schema/ABI 变更需要计划停止、更新、再启动；回滚前端不能恢复已清空的内存凭据。支持范围见 [updates](updates.md)。

## 打包分享与离线核验

在发布者自己的工作根运行，包内容来自当前受管源码，不复制运行状态、项目、凭据、浏览器登录、CLI 二进制、个人路径或 account 执行授权。

```bash
node "$SKILL_DIR/scripts/package.mjs" --target "$WORKSPACE" --skill "$SKILL_DIR"
```

可加 `--out "$WORKSPACE/artifacts/RELEASE_NAME.zip"`，输出必须是该根 `artifacts/` 的直接子文件且以 `.zip` 结尾，不接受子目录。默认 `artifacts/web-office-skills-v1.zip`；脚本生成源码 tar/manifest、ZIP/哈希和 skill-package JSON，保留旧包。可向 package 传 `--personal-markers-file "$WORKSPACE/artifacts/personal-markers.json"`，采用下节相同的根内 JSON 标识格式。

package 先在根内暂存候选源码与 ZIP，自动做离线核验和隐私扫描，再更新正式包及 metadata；旧包保留。`blocked` 会中止，`needsReview` 仍可能生成包，但状态和数量留在结果/metadata 中，可按脱敏报告逐项复核。分享前完成人工判断，不能将 `ok:true` 当作隐私已全通过。检查实际返回的清单和路径，不因文件名仍含 v1 就复用历史验收结论。

```bash
node "$SKILL_DIR/scripts/verify.mjs" --skill "$SKILL_DIR" --root "$WORKSPACE" --report "$WORKSPACE/artifacts/skill-verification.json"
```

`--root`、`--report` 可省略；报告写文件时需要显式 root，建议放在 `artifacts/` 的 JSON 路径。verify 离线核对结构、哈希、关键资源、外层 skill 与内嵌源码一致性；提供 root 时比较当前清单内源码，且存在 `artifacts/skill-package.json` 时默认核对 metadata 指向的 ZIP。也可加 `--bundle "$DELIVERY_ZIP"` 显式指定候选包；有 root 时该文件必须在根内。未核 ZIP 的结果不等于已验证分享包。

verify 不安装、不运行模型、不启动浏览器，也不是实际构建证明。新的隔离环境安装/构建/启动应分别执行本文件相应脚本并保留各自结果。

## 阶段产出：同步源码 → 提交 → 打包

可分发 skill 由项目代码仓库管理（`<工作根>/agents/<开发者ID>/work/01-网页办公室/code`），不再放在工作根 `skills/` 下。每个阶段产出按下面四步执行。

```bash
SKILL_REPO="$WORKSPACE/agents/<开发者ID>/work/01-网页办公室/code"

# 1) 按白名单从工作根收集源码，生成 skill 的源码归档（记录 officeVersion 与来源 commit）
node "$SKILL_REPO/scripts/pack-source.mjs" --root "$WORKSPACE" --skill "$SKILL_REPO" --out "$SKILL_REPO/assets"

# 2) 在 skill 仓库提交，提交信息标出来源
git -C "$SKILL_REPO" add -A
git -C "$SKILL_REPO" commit -m "chore(skill): sync source @$(git -C "$WORKSPACE" rev-parse --short HEAD)"

# 3) 打包（必须显式给 --skill；默认输出 artifacts/web-office-skills-v1.zip）
node "$SKILL_REPO/scripts/package.mjs" --target "$WORKSPACE" --skill "$SKILL_REPO" \
  --out "$WORKSPACE/artifacts/web-office-skills-v1.zip"

# 4) 离线核验 + 隐私（blocked 必须为 0）
node "$SKILL_REPO/scripts/verify.mjs" --skill "$SKILL_REPO" --root "$WORKSPACE" \
  --report "$WORKSPACE/artifacts/skill-verification.json"
```

- ZIP 内目录前缀是 `web-office-skills/`；源码归档随 skill 仓库提交，保证克隆即自包含。
- 分发包只含复现运行环境所需的文件：`app/{src,server,shared,public}` 与构建配置、`app/scripts` 中运行期需要的脚本、`company/`、`scripts/office.mjs`、`restart-office.sh`、`VERSION`、skill 自身。**不含**测试（`*.test.ts(x)`）、上游开发/QA 脚本、仓库内 `docs/`（特性文档只留在 git）。
- 改了 skill 脚本或文档后必须重新 `pack-source` 再 `package`，否则 `verify` 会报 `outer_embedded_skill_mismatch`。
- 安装命令不变：`node "$SKILL_REPO/scripts/install.mjs" --target <新工作根> --mode office [--port <端口>]`。

## 隐私扫描

```bash
node "$SKILL_DIR/scripts/privacy-scan.mjs" --skill "$SKILL_DIR" --root "$WORKSPACE" --report "$WORKSPACE/artifacts/skill-privacy.json"
```

不加 report 时只输出脱敏 JSON，不自动写文件；report 需显式 root，且限于 root 下 `artifacts/` 的 JSON，拒绝不安全链接。

| 可选参数                                                               | 用途                                                           |
| ---------------------------------------------------------------------- | -------------------------------------------------------------- |
| `--personal-marker VALUE`，可重复                                      | 扫描发布者指定的个人标识，不在输出中回显值                     |
| `--personal-markers-file "$WORKSPACE/artifacts/personal-markers.json"` | 从根内 JSON 数组或 `{ "markers": [] }` 读取标识；需要显式 root |
| `--zip "$WORKSPACE/artifacts/RELEASE_NAME.zip"`                        | 额外扫描根内分享 ZIP；需要显式 root                            |

标识用于个人路径片段、内部账号名等自查；不要把真实 Key、OAuth token、配对码当作命令行 marker。敏感标识优先用仅本机保存的文件输入，marker 文件不加入分享包。扫描只读取显式 skill、内嵌源码/ZIP 和 manifest 列出的 root 实时源码，不遍历根内运行数据或其他用户目录；报告不回显匹配原文。

退出码 `0` 表示未发现候选，`2` 表示仅有 `needsReview`，`1` 表示阻断项或扫描错误。示例/fixture、通用 home 路径和版权归属中的候选不会被静默忽略；按脱敏位置人工核对，保留许可证，不能将 needsReview 当作已经人工放行。静态扫描不能证明候选凭据有效，也不能保证找出所有私密资料。

## 记录证据

分别记录源码清单/哈希、privacy/verify 结果、实际安装、构建、启动/状态、前端发布、浏览器与真实模型操作。目录可见、上传成功、短连通或 1M 后缀请求成功，都不能替代视觉、OAuth、删除/重试或百万 token 输入验证。只声明本次实际完成的范围，未完成项写清楚，旧包结果保留为历史记录。

## 备份与恢复

```bash
node scripts/office.mjs backup --root "$PWD"            # 默认保留 7 份
node scripts/office.mjs backup --root "$PWD" --keep 3   # 调整保留份数
```

产物在 `artifacts/backups/office-backup-<时间>-<原因>-<id>.tar.gz`，同目录有 `.sha256` 与 `latest.json`；服务内每 24 小时自动跑一次（启动时若已超过 24 小时会补跑）。恢复：停服务 → 解开归档，用 `sqlite/*.sqlite` 覆盖 `state/`，把 `agents/`、`projects/`、`skills/` 覆盖回工作根（原生会话按 manifest 里的 `restorePath` 放回）→ 启动。

## 目录迁移（旧布局 → agents/<id>/…）

```bash
node scripts/office.mjs stop --root "$PWD"
node scripts/office.mjs migrate-layout --root "$PWD" --mode plan
node scripts/office.mjs migrate-layout --root "$PWD" --mode apply
node scripts/office.mjs start --root "$PWD"
```

迁移会重排 `agents/<id>/{profile,sessions,work,cache}`，并自动修复 Codex SQLite 里的绝对 rollout 路径；`--mode rollback` 可回退，启动守卫会拒绝半迁移状态。

## 技能库索引与技能工具

```bash
node app/scripts/skills-index.mjs "$PWD"    # 更新 skills/INDEX.md 与各方向 README 的技能表
```

Agent 侧只读工具：`office_list_skills`（方向 + 名称 + 描述 + 校验）、`office_read_skill`（`技能名` 或 `方向/技能名`，可读 `references/` 内文件）。

## 开发岗项目脚手架

```bash
node app/scripts/work-layout.mjs new-project    --name "01-项目名"
node app/scripts/work-layout.mjs new-feature    --project 01-项目名 --name "01-主特性"
node app/scripts/work-layout.mjs new-subfeature --project 01-项目名 --feature 01-主特性 --name "01-子特性"
node app/scripts/work-layout.mjs new-experiment --project 01-项目名 --feature 01-主特性 --name "01-实验"
node app/scripts/work-layout.mjs check          # 校验编号/层级/README/四份文档/状态
node app/scripts/work-layout.mjs finalize-feature --project 01-项目名 --feature 01-主特性   # 生成特性文档但不提交
node app/scripts/work-layout.mjs finalize-feature ... --toggle-reviewed                      # 确认开关形式后提交
```

`finalize-feature` 会拼接设计/实现/测试为《特性文档.md》、复制到 `code/docs/`、跑结构检查；未带 `--toggle-reviewed` 时不会提交，用来先和用户确认特性开关形式（默认环境变量）。

## Codex 可执行文件修复

```bash
node app/scripts/office-repair-codex-paths.mjs "$PWD" --dry-run   # 预览 SQLite 中的旧绝对路径
node app/scripts/office-repair-codex-paths.mjs "$PWD"             # 迁移后修复旧线程 resume
```


