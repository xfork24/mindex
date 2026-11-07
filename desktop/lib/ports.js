"use strict";
// 回环端口：页面的 localStorage（界面语言、偏好）按 origin 隔离，origin 含端口，所以端口必须持久化复用。
const net = require("node:net");

const HOST = "127.0.0.1";

function isPortFree(port, host = HOST) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.unref();
    server.once("error", () => resolve(false));
    server.listen({ port, host, exclusive: true }, () => server.close(() => resolve(true)));
  });
}

function freePort(host = HOST) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.once("error", reject);
    server.listen({ port: 0, host }, () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

// 返回 {port, changed}：changed 表示之前保存过端口但现在被占用、改用了新端口（界面偏好会重置）。
async function choosePort(saved, { check = isPortFree, pick = freePort } = {}) {
  if (saved && (await check(saved))) {
    return { port: saved, changed: false };
  }
  return { port: await pick(), changed: Boolean(saved) };
}

module.exports = { HOST, isPortFree, freePort, choosePort };
