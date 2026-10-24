# 代码质量审查（2026-10）

范围：2026-10-25 的工作区，方法是逐文件阅读加 grep 与 AST 扫描。本文是带日期的审查记录。

## 结论

基础做得不错：
- 文件服务路由都做了路径穿越防护（`resolve` 后用 `is_relative_to` 校验）；
- 子进程全部以参数列表调用，YAML 全部用 `safe_load`；
- SQLite 连接不跨线程共享；
- 没有可变默认参数；
- 前端只有一处 `innerHTML`，内容先经 DOMPurify 清洗。

主要问题有五类：
1. 异步端点里有阻塞调用；
2. 原子写的实现有缺陷；
3. 后台异常丢失 traceback；
4. 鉴权有两处小漏洞；
5. 前端轮询与重连没有退避。

ruff 没有启用能发现这些问题的规则。

## 发现

### 高

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| Q1 | `settings/settings_api.py` `clone_voice` | 端点是 `async def`，却直接调用同步的网络请求（`qwentts_register`、`minimax_clone`、`aliyun_enroll`，超时 600 秒）和 ffmpeg，期间整个事件循环被冻结，SSE 和助手都会卡住 | 阻塞调用改用 `run_in_threadpool` |
| Q2 | 同上 | `sample.write_bytes(await file.read())` 把任意大小的上传整个读进内存 | 改用 `save_upload` 并设上限 |
| Q3 | `studio/studio.py` `save`/`save_bytes`、`shared/jsonio.py` | 临时文件名固定为 `path.with_suffix(".partial")`：并发写同一目标会互相覆盖临时文件，`a.json` 和 `a.yaml` 也会共用同一个临时名；没有 fsync；写文件不指定编码，非 UTF-8 locale 下中文会出错 | 同目录唯一临时名，加 fsync 和 UTF-8，只保留一份实现 |
| Q4 | `studio/studio_api.py` 构建 worker 的 except 分支 | 用 `next(b for b ...)` 找构建记录，记录不存在时抛出 StopIteration，原始异常被掩盖；也没有记录 traceback | 先 `log.exception`，用 `next(..., None)`，终态写入单独加保护 |

### 中

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| Q5 | `shared/web_auth.py` `login` | `next` 只检查了以 `/` 开头、不以 `//` 开头，`/\evil.com` 能绕过检查，构成 open redirect | 拒绝 netloc、反斜杠和控制字符 |
| Q6 | `shared/web_auth.py` | `/logout` 接受 GET，存在登出 CSRF | 只接受 POST |
| Q7 | `cli.py` `serve` | 监听非回环地址且没有令牌时只警告，任何人都能访问设置和删除接口 | 拒绝启动，除非显式传 `--allow-insecure`（已与用户确认） |
| Q8 | `settings_api.py` 克隆失败分支 | `pending-*` 样本留在磁盘上；DashScope 失败记录的 voice_id 固定为 `mindex-{slug}-failed`，重复失败会冲突 | 失败时清理样本，voice_id 加随机后缀 |
| Q9 | `agent/api.py` 附件上传 | `async def` 里同步计算整文件 sha256 并 move | 放进线程池 |
| Q10 | 全仓库 | 有 52 处 `except Exception`，但只有 7 处记录了 traceback；后台失败只存 `str(exc)` | 后台线程顶层统一 `log.exception` |
| Q11 | `studio_api.py` 标题生成 | `except Exception: title = None` 没有日志，模型配置错误不会被发现 | 记 warning |
| Q12 | `studio_api.py` executor | `executor["thread"]` 由两个启动器共用，`cancels` 字典在线程间读写，没有统一加 guard | 由 `StudioExecutor` 统一管理 |
| Q13 | `frontend/src/persons/persons.js` 末尾 | `setInterval(..., 5000)` 永不停止，没有退避，页面在后台时也照常运行，慢请求会叠加 | 链式 setTimeout，页面隐藏时暂停，失败后退避 |
| Q14 | `frontend/src/shared/agent-panel.js` 两处 SSE | `onerror` 后固定 1.5 秒无限重连，服务关闭或返回 401 后每个标签页每 1.5 秒发一次请求 | 指数退避加上限 |
| Q15 | `pyproject.toml` `[tool.ruff.lint]` | 没有启用 `ASYNC`、`BLE`；`RUF100` 被忽略，所以现有的 `# noqa: BLE001` 实际不起作用 | 启用这些规则并整理 noqa |

### 低

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| Q16 | `shared/uploads.py` `save_upload` | 返回 413 或 422 时把半成品文件留给调用方处理 | 在函数内删除半成品 |
| Q17 | `maintenance/backup_files.py` | 备份向量的子进程没有超时 | 加超时 |
| Q18 | `processing/shot_repair.py` | `re.search(...).groups()` 匹配不到时抛 AttributeError | 判空后抛 MediaError |
| Q19 | `settings_api.py` 测试连接 | 把 `str(exc)[:500]` 原样返回给页面，可能带出带查询串的 URL | 脱敏后返回，完整错误写日志 |
| Q20 | `frontend/src/settings/settings.js` | 两处 `.catch(() => {})` 静默吞掉接口失败 | 改为提示 |
| Q21 | `admin/source_tasks.py` 等 | 线程全部是 daemon，服务关闭时会在写入中途被杀；依赖启动时的恢复逻辑兜底 | 由统一 runner 记录，本期不加关闭钩子 |
| Q22 | 多处 | 魔法数字：1500ms、5000ms、`timeout=600`、截断长度 | 修改相关代码时顺手提为常量 |

## 长函数

前十（AST 统计，单位为行）：

| 函数 | 行数 |
|---|---|
| `studio_router` | 1,005 |
| `person_router` | 478 |
| `settings_router` | 391 |
| `PersonTasks.run` | 297 |
| `agent_router` | 262 |
| `workspace.load` | 255 |
| `build_channels` | 243 |
| `render_timeline` | 214 |
| `dialogue._source` | 212 |
| `admin_router` | 211 |

路由工厂过长的问题，随 executor 抽离和工作台拆包一起处理。其余函数不单独拆分。

## 当前可做

| 任务 | 对应发现 |
|---|---|
| 4 安全与鉴权 | Q5、Q6、Q7 |
| 5 IO 与资源 | Q1、Q2、Q3、Q8、Q9、Q16、Q17、Q18 |
| 6 日志与前端轮询 | Q10、Q11、Q13、Q14、Q19、Q20 |
| 7.1 Lint | Q15 |
| 12 StudioExecutor | Q4、Q12 |
