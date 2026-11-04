# Web/API 访问鉴权

Mindex 默认**不启用鉴权**（本机使用）。设置环境变量 `MINDEX_WEB_TOKEN` 后启用令牌认证：浏览器在 `/login` 提交令牌换取会话 Cookie；脚本与命令行客户端使用 `Authorization: Bearer <令牌>`。实现位于 `backend/src/shared/web_auth.py`。

## 令牌与会话

- Cookie 名 `mindex_session`，HttpOnly；**存的是令牌的 HMAC-SHA256 摘要**（域分隔串 `mindex-web-session-v1`），不存令牌本身。
- 一切令牌比较使用常量时间比较（`hmac.compare_digest`）。
- `/login`：表单提交令牌，成功设置 Cookie；`next` 参数**只接受站内相对路径**（单个 `/` 开头、无反斜杠与控制字符、无 scheme/netloc，否则回落 `/`），防开放重定向。
- `/logout`：仅接受 POST。
- 公开路径只有：`/login`、`/logout`、`/mcp` 与 `/assets/shared/` 静态资源、`/api/agent/attachments`（该端点自行鉴权，同时接受 `MINDEX_MCP_TOKEN`）。
- 未设置令牌时不安装任何认证，行为与本机使用时一致。

## 各运行形态的鉴权

| 形态 | 令牌来源 |
|---|---|
| `mindex serve`（Web） | 环境变量 `MINDEX_WEB_TOKEN`，由部署者设定 |
| 桌面版 | 外壳**每次启动生成随机令牌**，对本 origin 自动注入 Bearer 与会话 Cookie，用户无感知（见 [桌面版](desktop.md)） |
| 桌面远程 API 模式 | 使用用户提供的远程访问令牌；文件凭据经系统安全存储加密，环境变量优先。非法配置明确报错，不自动进入本地库 |
| 智能助手工具（进程内回环） | 后端启动时生成进程内随机 `agent_internal_token`，工具请求经 `x-mindex-internal` 头携带；外部无法得知，见 [智能助手](agent.md) |
| MCP 服务（`mindex mcp` / `/mcp`） | 独立的 `MINDEX_MCP_TOKEN`（未设置时不挂载 `/mcp`），不复用网页登录；stdio 模式与 `--policy` 见 CLI 参考 |

## 与 API 的关系

启用后所有 `/api/*` 与页面都需要凭证（Bearer 或 Cookie）。智能助手工具经 `loopback.py` 的进程内 ASGI 回环调用既有 REST 接口，内部凭证由 `web_auth` 放行，不复制业务逻辑、不绕过校验。

集群业务 API 的助手请求转发至 `agent.gateway_url`，保留用户认证，剥离进程内
`x-mindex-internal` 凭证，并以 hop 标记拒绝循环路由；网关不可用时返回 503。
这是一个逻辑库、可信协作空间的访问模型，不等同于多租户或细粒度 RBAC。
完整网关入口、断流和重启恢复验收状态见 [增强验证记录](scaling-enhancement-verification.md)。
