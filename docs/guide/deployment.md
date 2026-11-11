# 服务器部署

本页说明怎样把 Zymor 部署到一台服务器，让多台机器、多个 AI 工具共用同一个用户库。

## 先确认适用范围

一个 `zymor mcp` 进程在启动时绑定一个库。服务器部署适合放**用户库**（偏好、跨项目知识、来自各处的对话）。

以下功能依赖本地仓库文件，放到服务器后不可用，应当留在各台开发机的项目库里：

- 文件、依赖、API 版本锚点（`zymor anchor` / `zymor stale`）；
- 规则检查（`memory_rule_check`）；
- 以 `路径:行号` 形式记录的证据核对。

推荐的组合：开发机上的项目库走 stdio；服务器上的用户库走 HTTPS。两者可以同时接入同一个宿主。

## 1. 准备库与配置

```bash
pip install zymor
zymor init --global                      # 创建 ~/.zymor/vault
python -c "import secrets; print(secrets.token_urlsafe(32))"   # 生成 token
```

`~/.zymor/config.toml`：

```toml
[llm]
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[llm.models]
default = "<模型名>"                     # 所有做梦角色共用；可再按角色覆盖

[llm.pricing."<模型名>"]                 # 不配置单价时 max_usd 不生效，只有 max_tokens 限流
input_per_1m = 0.0
output_per_1m = 0.0

[mcp]
public_url = "https://memory.example.com"   # 客户端访问的地址
```

## 2. 选择 TLS 方式

对外监听时，token 至少 24 个字符。监听非回环地址且没有 TLS 时，`zymor mcp` 拒绝启动。

**方式 A：反向代理终结 TLS（推荐）。** Zymor 只监听本机，证书由代理自动续期。

```bash
export ZYMOR_MCP_HTTP_TOKEN=<token>
zymor mcp --transport http --host 127.0.0.1 --port 8765
```

Caddy 配置：

```
memory.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

代理把外部域名原样放进 `Host` 头。Zymor 只接受 `Host` 在允许名单内的请求，名单由三部分组成：本机地址、`[mcp].public_url` 的域名、`[mcp].allowed_hosts` 里的条目。所以方式 A 必须配置 `public_url`；有多个域名时把其余的写进 `allowed_hosts`。名单外的请求返回 421。

**方式 B：Zymor 自己终结 TLS。**

```bash
zymor mcp --transport http --host 0.0.0.0 --port 8765 \
  --tls-cert /etc/letsencrypt/live/memory.example.com/fullchain.pem \
  --tls-key  /etc/letsencrypt/live/memory.example.com/privkey.pem
```

证书续期后需要重启进程。也可以把路径写进 `[mcp].tls_cert` 与 `[mcp].tls_key`。

**方式 C：私有网络。** 链路已由 WireGuard、Tailscale 等加密时，可以设置 `[mcp].allow_insecure_http = true` 后监听内网地址。

## 3. 常驻运行

`/etc/systemd/system/zymor.service`：

```ini
[Unit]
Description=Zymor MCP
After=network-online.target

[Service]
User=zymor
EnvironmentFile=/etc/zymor/env
ExecStart=/usr/local/bin/zymor mcp --transport http
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

`/etc/zymor/env`（权限 600）放 `ZYMOR_MCP_HTTP_TOKEN` 与 LLM 的 API key。

验证：

```bash
curl https://memory.example.com/healthz          # {"status":"ok"}，不需要 token
```

## 4. 定时做梦

没有常驻的做梦进程。用 cron 触发，或者打开按量自动做梦（`dream.auto_after_ingest_events`，见 [`cloud.md`](cloud.md)）：

```cron
0 4 * * * . /etc/zymor/env && /usr/local/bin/zymor dream >> /var/log/zymor-dream.log 2>&1
```

做梦与写入共用库锁。做梦运行期间，写入请求返回「写入锁被占用」，调用方稍后重试即可。

## 5. 接入客户端

想让每台电脑自动上传对话、自动取回记忆，用一条命令接入，见 [`cloud.md`](cloud.md)：

```bash
zymor connect https://memory.example.com --claude-code
```

只需要让 Agent 通过 MCP 工具读写记忆时，按下面的方式接入。

Claude Code：

```bash
claude mcp add --transport http zymor https://memory.example.com/mcp \
  --header "Authorization: Bearer <token>"
```

其他支持 Streamable HTTP 的 MCP 客户端：地址填 `https://memory.example.com/mcp`，请求头加 `Authorization: Bearer <token>`。

对话推送见 [`capture.md`](capture.md) 的「HTTP 推送」。

只想把本地的知识备份到服务器、并在多台电脑之间同步，不需要让服务器参与检索和提炼时，见 [`sync.md`](sync.md)。这种用法下服务器不需要配置 LLM，也不需要定时做梦。

## 6. 备份

库是纯文件。备份 `~/.zymor/vault` 目录即可；`index/` 是可重建的缓存，可以不备份。把节点目录纳入 git 能得到完整的变更历史（`L0/`、`audit/`、`capture/` 默认被忽略）。

## 安全边界

- 鉴权是单个静态 Bearer token：没有过期时间，没有按用户或按工具的权限，泄露后只能更换。
- 持有 token 的一方可以读写整个库。不要把同一个 token 发给不受信任的服务。
- 只需要读取的客户端，另起一个 `--read-only` 进程并使用不同的 token。
- `/healthz` 不需要鉴权，只返回固定内容。
