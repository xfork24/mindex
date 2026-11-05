# 双路径上传与浏览器 S3 配置

上传会话默认使用现有 API 分片中转。`POST /api/uploads` 可增加 `transport: s3`，
管理页“添加新素材”可选择直传对象存储。会话凭据仅授权该会话；服务器决定 pending key，
客户端不能提交正式对象 key。两条路径共用配额和服务端完整 SHA256 校验，ETag 只用于分片回执。

最小部署片段（在现有 PostgreSQL/S3 部署配置内合并）：

```yaml
uploads:
  default_transport: api
  part_bytes: 8388608
storage:
  backend: s3
  s3:
    endpoint_url: http://minio.internal:9000
    browser_endpoint_url: https://objects.example.com
    browser_origins:
      - https://mindex.example.com
    bucket: mindex
    region: us-east-1
    access_key_id_env: MINDEX_S3_ACCESS_KEY
    secret_access_key_env: MINDEX_S3_SECRET_KEY
```

内部 endpoint 用于服务端读写；所有浏览器预签名 URL 使用浏览器 endpoint。
反向代理必须保留原始 Host、路径与查询参数；不得改写签名。
直传的非末尾分片至少 5 MiB；API 分片保留原有可配置大小。

S3 bucket 的最小 CORS 规则如下。先导出已有规则再合并，避免覆盖其他应用的 CORS；
Mindex 不会自动写入或覆盖 bucket CORS。MinIO 等实现可通过其原生部署配置提供同等能力。

```json
{"CORSRules":[{"AllowedOrigins":["https://mindex.example.com"],
"AllowedMethods":["GET","HEAD","PUT"],"AllowedHeaders":["*"],
"ExposeHeaders":["ETag","Content-Length","Content-Range","Accept-Ranges"],"MaxAgeSeconds":300}]}
```

执行 `mindex --config /path/to/cluster.yaml doctor` 检查签名、endpoint、实际 CORS 预检及 ETag 暴露。
探针只操作随机 pending key 并清理原生 multipart，不修改 bucket 配置。
服务端可达性检查不能代替浏览器实测；`scripts/verify-scaling.py` 包含真实浏览器跨 endpoint 上传。

续传协议：

1. 创建会话，保存 `id` 和 `upload_token`；所有会话操作使用 `X-Upload-Token`。
2. `POST /api/uploads/{id}/parts/{number}/sign` 获取短期 PUT URL。
3. 浏览器直接 PUT Blob；不得带应用凭据。用返回的 ETag 调用同路径 `/receipt`。
4. `GET /api/uploads/{id}` 从 S3 分页读取真实分片，恢复未登记回执；不依赖浏览器内存。
5. `POST /api/uploads/{id}/complete` 原子完成；重复调用返回同一资产。
6. `DELETE /api/uploads/{id}` 显式取消；到期回收同时清理应用分片与原生 multipart。

网络、CORS 或服务故障保留会话，可重选同一文件续传。浏览器直传不会隐式重做整个上传；
过期或需要放弃原会话时选择“重新上传”。完成阶段发生中断后，可再次 complete，
服务器复验已完成的 pending/正式对象并继续发布，不要求客户端重传。

媒体出口支持 GET、HEAD、Range、If-Range、ETag 和实体条件请求。预签名下载的保留 pin
覆盖 URL 有效期及 300 秒下载宽限；删除中的对象拒绝新签名。签名 URL 在过期前是可转交能力。
