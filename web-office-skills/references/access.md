# SSH 与网页访问

默认使用独立 SSH，不依赖 VS Code。服务保持服务器回环监听，通常为 18080；复用使用者自己的账号、SSH 端口和跳板配置，不自动创建公开隧道或改防火墙。

## 使用者本机

Windows 打开 PowerShell，macOS/Linux 打开终端；将通用占位符替换成自己的连接信息：

```bash
ssh -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:18080 -p SSH_PORT USER@SERVER
```

已有 SSH 主机别名可保留它配置的账号、端口和跳板：

```bash
ssh -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:18080 HOST_ALIAS
```

SSH 登录端口与网页端口不同。此连接同时打开远程终端和网页转发。

## 服务器终端与本机浏览器

在上一步打开的**服务器终端**，对已有工作根执行 [workflows 的启动、状态和配对命令](workflows.md#启动配对与日常维护)；没有安装时先按首次安装流程选择根目录与界面，不猜目录。

保持 SSH 窗口连接，在**本机浏览器**打开 `http://127.0.0.1:18080`。配对结果中只复制 `pairingCode` 字符串，不复制整段 JSON 或引号。当前配对码有效五分钟、成功使用一次后失效，新码替换旧的未使用码；未使用码也会在运行层重启时失效。不要保存实际码。

同一浏览器配对后固定登录 30 天，刷新不延长有效期；未过期的登录跨服务重启保留。退出登录、到期、清 Cookie、换浏览器/隐私窗口后重新配对。网页登录与模型授权独立：重启后仍需在网页补 Key 或重新完成官方账户认证。

SSH 断开只断开转发，后台与其运行期凭据继续保留；服务器重启后需要重新启动运行层。已有健康服务仅需转发时，在使用者本机运行：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18080:127.0.0.1:18080 -p SSH_PORT USER@SERVER
```

成功后无输出正常，保持窗口运行；Ctrl+C 关闭隧道。若本机 18080 被占用，可将 `-L` 的第一个端口改为 18082，浏览器也改用 18082；最后一个端口仍使用实际服务器网页端口。

## 可选 VS Code 转发

1. 用 Remote-SSH 连接服务器，在远程终端执行 workflows 中的启动/配对流程。
2. 底部 Ports（端口）面板选择 Forward a Port（转发端口），输入服务器网页端口。
3. 在本机用户设置中合并以下非秘密配置，保留已有内容：

```json
{
  "remote.restoreForwardedPorts": true,
  "remote.portsAttributes": {
    "18080": { "label": "同席 Agent 办公室" }
  }
}
```

用同一主机别名重连原工作区可恢复已保存转发。点击端口行的 Open in Browser（在浏览器中打开），实际地址以 Local Address（本地地址）为准。列表已有端口时直接使用，无需重复添加。找不到面板时用 Ctrl+J，或命令面板搜索 Forward a Port；macOS 的命令面板快捷键为 Cmd+Shift+P。

端口恢复不会启动服务器后台。转发期间保持远程连接，配对码/Key/OAuth 不写进 SSH 或 VS Code 设置。参见 [VS Code 官方端口转发说明](https://code.visualstudio.com/docs/remote/ssh#_temporarily-forwarding-a-port)。
