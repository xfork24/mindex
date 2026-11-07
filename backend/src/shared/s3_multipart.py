"""浏览器原生 multipart：仅签名会话 pending key，服务端核对真实分片。"""

from __future__ import annotations

import mimetypes

from shared.upload_sessions import UploadError


class S3Multipart:
    def __init__(self, store):
        self.store, self.media = store, store.media

    def key(self, session_id):
        return f"pending/uploads/{session_id}/object"

    def args(self, row):
        return {"Bucket": self.media.bucket, "Key": self.media._object_key(self.key(row["id"]), None)}

    def uploads(self, row):
        args = self.args(row)
        return [
            item
            for page in self.media.client.get_paginator("list_multipart_uploads").paginate(
                Bucket=args["Bucket"], Prefix=args["Key"]
            )
            for item in page.get("Uploads", [])
            if item["Key"] == args["Key"]
        ]

    def ensure(self, row):
        if row["multipart_id"]:
            return row["multipart_id"]
        uploads = self.uploads(row)
        if uploads:
            upload_id = uploads[0]["UploadId"]
            for item in uploads[1:]:
                self.media.client.abort_multipart_upload(**self.args(row), UploadId=item["UploadId"])
        else:
            upload_id = self.media.client.create_multipart_upload(
                **self.args(row),
                ContentType=mimetypes.guess_type(row["filename"])[0] or "application/octet-stream",
            )["UploadId"]
        self.store.execute(
            "UPDATE upload_sessions SET multipart_id=?,pending_key=? WHERE id=?",
            (upload_id, self.key(row["id"]), row["id"]),
        )
        return upload_id

    def sign(self, row, number, expires):
        expected = min(row["part_size"], row["size"] - (number - 1) * row["part_size"])
        if number < 1 or number > 10000 or expected <= 0:
            raise UploadError("分片编号无效", 422)
        upload_id = self.ensure(row)
        params = {**self.args(row), "UploadId": upload_id, "PartNumber": number, "ContentLength": expected}
        url = self.media.browser_client.generate_presigned_url(
            "upload_part", Params=params, ExpiresIn=expires
        )
        return {
            "number": number,
            "url": url,
            "method": "PUT",
            "headers": {},
            "size": expected,
            "expires_in": expires,
        }

    def parts(self, row):
        if not row["multipart_id"]:
            return []
        parts = []
        for page in self.media.client.get_paginator("list_parts").paginate(
            **self.args(row), UploadId=row["multipart_id"]
        ):
            for item in page.get("Parts", []):
                number, size = item["PartNumber"], item["Size"]
                expected = min(row["part_size"], row["size"] - (number - 1) * row["part_size"])
                if not 1 <= number <= 10000 or size != expected or expected <= 0:
                    raise UploadError("S3 分片大小或编号不符合会话声明，请重新上传该分片", 422)
                parts.append({"number": number, "size": size, "etag": item["ETag"], "sha256": ""})
        with self.store.connect() as db:
            db.execute("DELETE FROM upload_parts WHERE session_id=?", (row["id"],))
            for part in parts:
                db.execute(
                    "INSERT INTO upload_parts(session_id,number,size,sha256,object_key,etag) "
                    "VALUES(?,?,?,?,?,?)",
                    (row["id"], part["number"], part["size"], "", self.key(row["id"]), part["etag"]),
                )
        return sorted(parts, key=lambda item: item["number"])

    def complete(self, row, parts):
        key = self.key(row["id"])
        # 远端完成成功而应用状态尚未提交时，恢复读取同一 pending 对象复算摘要。
        try:
            obj = self.media.stat(key)
        except FileNotFoundError:
            upload_id = row["multipart_id"] or self.ensure(row)
            self.media.client.complete_multipart_upload(
                **self.args(row),
                UploadId=upload_id,
                MultipartUpload={"Parts": [{"PartNumber": p["number"], "ETag": p["etag"]} for p in parts]},
            )
            obj = self.media.stat(key)
        if obj.size != row["size"]:
            raise UploadError("S3 完成对象大小不符", 422)
        return key

    def abort(self, row):
        # 包含创建成功但 UploadId 尚未落账的恢复窗口。
        for item in self.uploads(row):
            self.media.client.abort_multipart_upload(**self.args(row), UploadId=item["UploadId"])


def probe_browser(media, origins):
    """核验实际 browser endpoint 的签名与 CORS，不覆盖 bucket CORS 配置。"""
    from urllib.parse import urlsplit
    from uuid import uuid4

    import httpx

    key = media._object_key("pending/capability-probe/browser-" + uuid4().hex, None)
    args = {"Bucket": media.bucket, "Key": key}
    upload_id = media.client.create_multipart_upload(**args, ContentType="application/octet-stream")[
        "UploadId"
    ]
    try:
        url = media.browser_client.generate_presigned_url(
            "upload_part", Params={**args, "UploadId": upload_id, "PartNumber": 1}, ExpiresIn=60
        )
        if (
            urlsplit(url).netloc
            != urlsplit(media.section.browser_endpoint_url or media.section.endpoint_url).netloc
        ):
            raise ValueError("签名 endpoint 与浏览器 endpoint 不一致")
        with httpx.Client(timeout=5, follow_redirects=False) as client:
            for origin in origins:
                preflight = client.options(
                    url,
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "PUT",
                        "Access-Control-Request-Headers": "content-type",
                    },
                )
                allowed = preflight.headers.get("access-control-allow-origin")
                methods = {
                    x.strip().upper()
                    for x in preflight.headers.get("access-control-allow-methods", "").split(",")
                }
                if preflight.status_code >= 400 or allowed not in {"*", origin} or "PUT" not in methods:
                    raise ValueError(f"浏览器 origin 的 PUT CORS 预检失败：{origin}")
                response = client.put(
                    url,
                    content=b"browser-signature-probe",
                    headers={"Origin": origin, "Content-Type": "application/octet-stream"},
                )
                expose = {
                    x.strip().lower()
                    for x in response.headers.get("access-control-expose-headers", "").split(",")
                }
                if (
                    response.status_code >= 400
                    or response.headers.get("access-control-allow-origin") not in {"*", origin}
                    or not response.headers.get("etag")
                    or not ({"*", "etag"} & expose)
                ):
                    raise ValueError(f"浏览器 endpoint 的签名或 ETag CORS 暴露失败：{origin}")
        return "签名与实际 endpoint/CORS 探针通过"
    finally:
        media.client.abort_multipart_upload(**args, UploadId=upload_id)
