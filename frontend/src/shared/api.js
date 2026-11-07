/* 共享请求封装：各页面的 api() 都是它的薄封装。
 * - request(url, options)：options 原样交给 fetch（method/headers/body/FormData/signal 等）。
 * - json(url, method, body, options)：普通对象按 JSON 发送；FormData 原样发送；undefined/null 不带请求体。
 * 返回解析后的 JSON；204 或成功但非 JSON 的响应返回 {}。
 * 失败时抛出 Error：message 为 MindexI18n.system(响应体)（可随语言切换渲染的文案），
 * 并附带 status、detail（响应体 detail 字段）与 data（完整响应体）。网络错误与中止（AbortError）原样抛出。 */
(function () {
  "use strict";
  const root = typeof window !== "undefined" ? window : globalThis;
  function failure(response, data) {
    const i18n = root.MindexI18n;
    const error = new Error(i18n ? i18n.system(data) : String(data?.detail ?? response.status));
    error.status = response.status;
    error.detail = data?.detail;
    error.data = data;
    return error;
  }
  const studioRevisions = new Map();
  async function request(url, options = {}) {
    const job = String(url).match(/^\/api\/studio\/jobs\/([^/?]+)/)?.[1];
    if (
      job &&
      options.method &&
      options.method !== "GET" &&
      options.method !== "HEAD" &&
      studioRevisions.has(job)
    ) {
      options = {
        ...options,
        headers: { "If-Match": studioRevisions.get(job), ...(options.headers || {}) },
      };
    }
    const response = await fetch(url, options);
    const studioRevision = response.headers?.get("X-Studio-Revision");
    if (job && studioRevision && response.ok) studioRevisions.set(job, studioRevision);
    if (response.status === 204) return {};
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (error?.name === "AbortError") throw error;
      data = response.ok ? {} : { detail: `HTTP ${response.status}` };
    }
    if (!response.ok) throw failure(response, data);
    if (data?._analysis_translations)
      root.MindexAnalysisText?.addTranslations(data._analysis_translations);
    return data;
  }
  function json(url, method = "GET", body, options = {}) {
    const init = { ...options, method };
    if (typeof FormData !== "undefined" && body instanceof FormData) init.body = body;
    else if (body !== undefined && body !== null) {
      init.headers = { "Content-Type": "application/json", ...(options.headers || {}) };
      init.body = JSON.stringify(body);
    }
    return request(url, init);
  }
  // 增量 SHA256：每次仅保留当前块，避免对长视频使用 file.arrayBuffer()。
  class Sha256 {
    constructor() {
      this.h = new Uint32Array([
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab,
        0x5be0cd19,
      ]);
      this.words = new Uint32Array(64);
      this.pending = new Uint8Array(64);
      this.used = 0;
      this.length = 0;
    }
    update(bytes) {
      this.length += bytes.length;
      let offset = 0;
      while (offset < bytes.length) {
        const count = Math.min(64 - this.used, bytes.length - offset);
        this.pending.set(bytes.subarray(offset, offset + count), this.used);
        this.used += count;
        offset += count;
        if (this.used === 64) {
          this.block(this.pending);
          this.used = 0;
        }
      }
      return this;
    }
    block(bytes) {
      const k = Sha256.k,
        w = this.words,
        r = Sha256.rotate;
      for (let i = 0; i < 16; i++)
        w[i] =
          (bytes[i * 4] << 24) |
          (bytes[i * 4 + 1] << 16) |
          (bytes[i * 4 + 2] << 8) |
          bytes[i * 4 + 3];
      for (let i = 16; i < 64; i++) {
        const a = w[i - 15],
          b = w[i - 2];
        w[i] =
          (w[i - 16] +
            (r(a, 7) ^ r(a, 18) ^ (a >>> 3)) +
            w[i - 7] +
            (r(b, 17) ^ r(b, 19) ^ (b >>> 10))) >>>
          0;
      }
      let [a, b, c, d, e, f, g, h] = this.h;
      for (let i = 0; i < 64; i++) {
        const t = (h + (r(e, 6) ^ r(e, 11) ^ r(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i]) >>> 0;
        const u = ((r(a, 2) ^ r(a, 13) ^ r(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
        h = g;
        g = f;
        f = e;
        e = (d + t) >>> 0;
        d = c;
        c = b;
        b = a;
        a = (t + u) >>> 0;
      }
      [a, b, c, d, e, f, g, h].forEach((v, i) => {
        this.h[i] = (this.h[i] + v) >>> 0;
      });
    }
    hex() {
      const bits = BigInt(this.length) * 8n;
      const ending = new Uint8Array((this.used < 56 ? 64 : 128) - this.used);
      ending[0] = 128;
      for (let i = 0; i < 8; i++)
        ending[ending.length - 1 - i] = Number((bits >> BigInt(i * 8)) & 255n);
      this.update(ending);
      return [...this.h].map((v) => v.toString(16).padStart(8, "0")).join("");
    }
  }
  Sha256.rotate = (x, n) => (x >>> n) | (x << (32 - n));
  Sha256.k = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  async function uploadSource(file, onProgress = () => {}, options = {}) {
    const transport = options.transport || "api";
    const digest = new Sha256(),
      chunkSize = 1024 * 1024;
    for (let offset = 0; offset < file.size; offset += chunkSize)
      digest.update(new Uint8Array(await file.slice(offset, offset + chunkSize).arrayBuffer()));
    const sha256 = digest.hex(),
      storageKey = `mindex.upload:${sha256}:${file.size}${transport === "api" ? "" : ":s3"}`;
    let session;
    try {
      session = JSON.parse(root.sessionStorage?.getItem(storageKey) || "null");
    } catch (_) {
      /* 存储禁用时仍可完成上传。 */
    }
    if (options.restart) {
      if (session)
        try {
          await request(`/api/uploads/${session.id}`, {
            method: "DELETE",
            headers: { "X-Upload-Token": session.upload_token },
          });
        } catch (error) {
          if (![404, 409, 410].includes(error.status)) throw error;
        }
      session = null;
    }
    if (session) {
      try {
        const status = await request(`/api/uploads/${session.id}`, {
          headers: { "X-Upload-Token": session.upload_token },
        });
        session = { ...session, ...status };
      } catch (error) {
        if (transport === "api" && (error.status === 404 || error.status === 410)) session = null;
        else throw error;
      }
      if (
        session &&
        (["aborted", "expired"].includes(session.state) ||
          (session.state === "pending" && session.expires_at <= Date.now() / 1000))
      ) {
        if (transport === "s3")
          throw new Error(
            root.MindexI18n?.msg("ui.upload_restart_required") || "上传会话已过期，请显式重新上传",
          );
        session = null;
      }
    }
    if (!session) {
      session = await json("/api/uploads", "POST", {
        filename: file.name,
        size: file.size,
        sha256,
        transport,
      });
      try {
        root.sessionStorage?.setItem(storageKey, JSON.stringify(session));
      } catch (_) {
        /* 会话凭据仍在当前调用中保留。 */
      }
    }
    const headers = { "X-Upload-Token": session.upload_token },
      present = new Set(session.parts.map((p) => p.number));
    for (let offset = 0, number = 1; offset < file.size; offset += session.part_size, number++) {
      if (!present.has(number) && session.state === "pending") {
        const chunk = file.slice(offset, offset + session.part_size);
        if (session.transport === "s3") {
          const signed = await json(
            `/api/uploads/${session.id}/parts/${number}/sign`,
            "POST",
            undefined,
            { headers },
          );
          const response = await fetch(signed.url, {
            method: "PUT",
            body: chunk,
            headers: signed.headers,
            credentials: "omit",
          });
          if (!response.ok)
            throw failure(response, {
              detail:
                root.MindexI18n?.msg("ui.upload_direct_failed", { p0: response.status }) ||
                `S3 上传失败：HTTP ${response.status}`,
            });
          const etag = response.headers.get("ETag");
          if (!etag)
            throw new Error(
              root.MindexI18n?.msg("ui.upload_cors_etag") ||
                "S3 CORS 未暴露 ETag，请修复部署后续传",
            );
          await json(
            `/api/uploads/${session.id}/parts/${number}/receipt`,
            "POST",
            { etag },
            { headers },
          );
        } else
          await request(`/api/uploads/${session.id}/parts/${number}`, {
            method: "PUT",
            headers,
            body: chunk,
          });
      }
      onProgress(Math.min(100, Math.round(((offset + session.part_size) / file.size) * 100)));
    }
    const asset = await json(`/api/uploads/${session.id}/complete`, "POST", undefined, { headers });
    const result = await json(`/api/admin/sources/import-upload/${asset.asset_id}`, "POST");
    try {
      root.sessionStorage?.removeItem(storageKey);
    } catch (_) {
      /* 不影响服务端幂等结果。 */
    }
    return result;
  }
  root.MindexApi = { request, json, uploadSource, Sha256 };
})();
