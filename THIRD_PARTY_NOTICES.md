# 第三方组件声明

仓库内随前端分发的第三方文件。文件按原样复制自 npm 公共仓库的发布包，未做修改；升级时请同步更新本表与 `docs/agent.md`，并运行 `python scripts/build-assets.py` 更新页面里的资源版本号（`?v=`）。

| 文件 | 组件与版本 | 许可证 | 来源（npm 包内路径） | 发布包 integrity | 文件 SHA-256 |
|---|---|---|---|---|---|
| `frontend/src/shared/marked.umd.js` | [marked](https://github.com/markedjs/marked) 18.1.1 | MIT | `marked-18.1.1.tgz` → `package/lib/marked.umd.js` | `sha512-zEGgrtUG8crk+TrMacJSVABU4DYDDkS8uqgU3ohrn/2fW9C2AEfUrO3m6IhpIpQpl7YcVpiXXC9cSMWDANT3dg==` | `b82b9334406d8bdd16b6236c08de1cb65b2a859873ca9503d9cc0b292ca3c12a` |
| `frontend/src/shared/purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.17 | MPL-2.0 OR Apache-2.0（本项目按 Apache-2.0 使用） | `dompurify-3.4.17.tgz` → `package/dist/purify.min.js` | `sha512-B/pSlYVHQ58kqISD/yDjuJ1yfzI34m1gF4lAod2bwSRhiN4K5Cl3H8Af7hqB2PmTL911tdEudnnXn5FnhRoz0w==` | `cd37898f9782adf54820c413191b5691803f0f137c799d6edcfd38523b4c2c31` |

两个文件都保留了原始的版权与许可证头注释。用途：智能助手面板把模型回复的 Markdown 用 marked 转成 HTML，再经 DOMPurify 按白名单净化后显示（见 `frontend/src/shared/agent-markdown.js`）。

许可证全文：

- marked（MIT）：Copyright (c) 2018+, MarkedJS；Copyright (c) 2011-2018, Christopher Jeffrey。全文见 <https://github.com/markedjs/marked/blob/v18.1.1/LICENSE.md>。
- DOMPurify（Apache-2.0 / MPL-2.0）：Copyright (c) Cure53 and other contributors。全文见 <https://github.com/cure53/DOMPurify/blob/3.4.17/LICENSE>。
