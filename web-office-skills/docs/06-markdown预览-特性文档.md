# 特性文档 · 06-markdown预览

- 项目：01-网页办公室
- 生成时间：2026-11-17T09:35:05.684Z

## 目录

1. [一、设计文档](#一设计文档)
2. [二、实现文档](#二实现文档)
3. [三、测试文档](#三测试文档)

# 一、设计文档

# 06-markdown预览 · 设计文档

## 目标

用户点开会话里的 `.md` 产出时看到"演示效果"，而不是源码或下载文件；与既有 HTML「在线打开」并列。

## 关键决策

| 议题 | 结论 | 理由 |
| --- | --- | --- |
| 打开方式 | 会话内弹窗（A 方案） | 复用站点 Markdown（remark-gfm）与样式，不引入服务端渲染依赖；HTML 走新标签，Markdown 走弹窗 |
| 取原文 | 同源 `fetch` `mode=download` | 复用既有鉴权 Cookie 与文件服务，不需要新接口 |
| 体积上限 | 硬上限 5MB、1MB 起提示 | 渲染在主线程，几百 KB~数 MB 的文档解析 + 排版会明显卡顿；工作区实际最大 md 为 3.4MB |
| 相对路径 | 以被预览文档所在目录为基准 | 文档里的 `img/a.png` 应解析到 `docs/img/a.png`，否则图片全裂 |
| 越界 | 解析出工作区之外时返回 `null`，保留原样 | 与 `isWorkspaceFileHref` 的既有安全边界一致 |

## 交互

- 点击 `.md` 链接 → 弹窗（标题=文件名，副标题=路径），顶部显示体积与「下载原文」。
- Ctrl/Cmd/Shift/Alt 点击或非左键 → 仍按下载链接行为，可直接另存。
- 预览中的 `.md` 链接 → 再开一层预览；Esc 关闭最上层弹窗（沿用 `Modal`）。

---

# 二、实现文档

# 06-markdown预览 · 实现文档

## 前端

- `file-links.ts`
  - 新增 `isMarkdownHref`（md/markdown/mdown/mkd）、`isRelativeHref`、`resolveRelativePath(basePath, href)`；
  - `resolveRelativePath` 先取 `basePath` 的目录部分（`pop()` 掉文件名），逐段解析 `.`/`..`，越界或含反斜杠/空字节返回 `null`，非相对地址直接返回 `null`。
- `MessageBody.tsx`
  - 新增 `basePath` 属性；`markdownComponents(agentId, basePath, onPreview)` 先按 `basePath` 重定相对链接，再走原有 `isWorkspaceFileHref` 判定；
  - `.md` 链接渲染为带「预览」标记的 `<a href=download>`，左键 `preventDefault()` 后打开弹窗，修饰键不拦截；
  - 组件内部持有 `previewPath` 状态并渲染 `MarkdownPreviewDialog`，因此预览内部还能再嵌套预览。
- `MarkdownPreviewDialog.tsx`
  - `fetch` 原文 → 先看 `Content-Length`，超限直接提示；否则读文本再按字节数复核；
  - `startTransition` 渲染，避免大文档解析时白屏；读取失败/超大均保留下载入口。

## 边界

- 预览只读，不写文件、不改会话。
- 未登录或跨工作区路径由既有文件服务拒绝，弹窗显示"暂时无法读取"。

---

# 三、测试文档

# 06-markdown预览 · 测试文档

## 自动化

| 用例 | 文件 | 结果 |
| --- | --- | --- |
| md 扩展名识别（含大写、markdown/mdown/mkd；mdx 不算） | `file-links.test.ts` | 通过 |
| 相对 vs 路由/锚点/URL 判定 | `file-links.test.ts` | 通过 |
| 以文档目录为基准解析，含 `..` 上升 | `file-links.test.ts` | 通过 |
| 越界（`../` 出头、反斜杠）返回 null | `file-links.test.ts` | 通过 |
| 点击 md 链接打开弹窗并渲染标题/表格 | `MessageBody.test.tsx` | 通过 |
| 修饰键点击不拦截（仍为下载链接） | `MessageBody.test.tsx` | 通过 |
| 预览文档内相对图片解析到文档目录 | `MessageBody.test.tsx` | 通过 |
| 预览文档内外链保持原样 | `MessageBody.test.tsx` | 通过 |
| 弹窗渲染 Markdown（非源码）+ 下载链接 | `MarkdownPreviewDialog.test.tsx` | 通过 |
| 超 5MB 拒绝渲染并给下载入口 | `MarkdownPreviewDialog.test.tsx` | 通过 |
| 读取失败降级提示 | `MarkdownPreviewDialog.test.tsx` | 通过 |

## 手工

| 场景 | 结果 |
| --- | --- |
| `tsc -b` | 通过（16.9s） |
| `vite build --config vite.office.config.ts` | 通过（9.4s，双入口） |
| `eslint` 变更文件 | 0 问题 |
| `prettier --check` 变更文件 | 通过 |
| `vitest src/office` | 18 文件 / 75 用例全通过 |
