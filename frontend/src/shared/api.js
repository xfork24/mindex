/* 共享请求封装：各页面的 api() 都是它的薄封装。
 * - request(url, options)：options 原样交给 fetch（method/headers/body/FormData/signal 等）。
 * - json(url, method, body, options)：普通对象按 JSON 发送；FormData 原样发送；undefined/null 不带请求体。
 * 返回解析后的 JSON；204 或成功但非 JSON 的响应返回 {}。
 * 失败时抛出 Error：message 为 MindexI18n.system(响应体)（可随语言切换渲染的文案），
 * 并附带 status、detail（响应体 detail 字段）与 data（完整响应体）。网络错误与中止（AbortError）原样抛出。 */
(function () {
  'use strict';
  const root = typeof window !== 'undefined' ? window : globalThis;
  function failure(response, data) {
    const i18n = root.MindexI18n;
    const error = new Error(i18n ? i18n.system(data) : String(data?.detail ?? response.status));
    error.status = response.status;
    error.detail = data?.detail;
    error.data = data;
    return error;
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 204) return {};
    let data;
    try {
      data = await response.json();
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      data = response.ok ? {} : {detail: `HTTP ${response.status}`};
    }
    if (!response.ok) throw failure(response, data);
    if (data?._analysis_translations) root.MindexAnalysisText?.addTranslations(data._analysis_translations);
    return data;
  }
  function json(url, method = 'GET', body, options = {}) {
    const init = {...options, method};
    if (typeof FormData !== 'undefined' && body instanceof FormData) init.body = body;
    else if (body !== undefined && body !== null) {
      init.headers = {'Content-Type': 'application/json', ...(options.headers || {})};
      init.body = JSON.stringify(body);
    }
    return request(url, init);
  }
  root.MindexApi = {request, json};
})();
