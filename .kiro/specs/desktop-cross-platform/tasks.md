# Tasks

任务分解对应 [plans/WIN_LINUX_NATIVE_PLAN.md](../../../plans/WIN_LINUX_NATIVE_PLAN.md) 的实施顺序：后端跨平台层 → 外壳跨平台 → 构建链（Linux 先）→ 打包 → 冒烟 → CI → 文档。每阶段在至少一个真实目标平台验证后才进入下一阶段。

## 阶段 0：spec 与 spike

- [x] 0.1 spec 三件套与 `plans/WIN_LINUX_NATIVE_PLAN.md`、`plans/README.md` 登记
  - _Requirements: 全部（文档框架）_
- [ ] 0.2 spike S1–S6（Windows/Linux 环境可得时执行；结论回填 design.md §7，失败调整对应阶段方案）
  - _Requirements: 1.2, 2.3, 3.1, 3.3, 4.1_

## 阶段 1：后端跨平台层

- [x] 1.1 `shared/locks.py`：fcntl 条件导入 + `_lock_region/_unlock_region` 平台分支（Windows LockFileEx ctypes）
  - _Requirements: 1.1–1.3_
- [x] 1.2 `shared/process_ops.py`：spawn_detached / alive / process_start / stop_tree
  - _Requirements: 2.1–2.4_
- [x] 1.3 `settings/local_services.py` 与 `cli_persons.py` 接入 process_ops
  - _Requirements: 2.2_
- [ ] 1.4 验证：mac 上 `tests/test_locks.py`、`tests/test_local_services.py`、`tests/test_processing_lock.py` 全绿且行为不变；Windows 环境可得时同套验证（S4 含 filelock 互操作）
  - _Requirements: 1.1, 2.1–2.4_

## 阶段 2：外壳跨平台

- [x] 2.1 `lib/paths.js` binaryName；`lib/sidecar.js` IS_WIN 分支（PowerShell 父链、taskkill stopGroup、windowsHide）；`main.js` setAppUserModelId
  - _Requirements: 3.1–3.2_
- [x] 2.2 托盘兜底：`createTray` try/catch + `shouldHide` trayAvailable + strings 文案
  - _Requirements: 3.3_
- [x] 2.3 测试：`desktop/test` 注入式 Windows 分支用例；`tests/desktop_smoke.cjs` 平台断言与分支；`tests/test_desktop_smoke.py` 去 darwin skip
  - _Requirements: 3.1–3.5_
- [ ] 2.4 验证：mac 回归集（node --test、dev/frozen 冒烟、快速 pytest）；Linux dev 冒烟
  - _Requirements: 3.5_

## 阶段 3：构建链（先 Linux 后 Windows）

- [x] 3.1 `build-desktop-ffmpeg.sh` 三环境分支（uname 分派、digest()、工具链、libass 后端、configure、验收、strip）
  - _Requirements: 4.1–4.2_
- [x] 3.2 `build-desktop-sidecar.sh` 去 Darwin 硬检查；`mindex.spec` darwin 条件 kwargs；`check-desktop-sidecar.py` 跨平台
  - _Requirements: 4.3, 4.5_
- [x] 3.3 新增 `build-desktop-sidecar.ps1`、`build-desktop-linux.sh`、`build-desktop-win.ps1`
  - _Requirements: 4.4_
- [ ] 3.4 验证：Linux 全链（ffmpeg ldd 验收 + AppImage/deb + check 冒烟）；Windows 全链（DLL 白名单 + NSIS/win-unpacked）；mac `build-desktop-mac.sh` 零回归
  - _Requirements: 4.1–4.5_

## 阶段 4：打包配置

- [x] 4.1 `desktop/package.json` build 段加 win/nsis/linux
  - _Requirements: 5.1_
- [ ] 4.2 验证：三平台制品产出与 unpacked 冒烟可用
  - _Requirements: 5.1, 6.3_

## 阶段 5：冒烟与测试集成

- [ ] 5.1 三平台 packaged 冒烟（mac dmg / win win-unpacked / linux linux-unpacked + xvfb）；真机验证记录 `docs/VERIFICATION.md`
  - _Requirements: 6.3, 7.3_

## 阶段 6：CI

- [x] 6.1 `ci.yml` desktop 与 desktop-package 三平台矩阵 + 缓存
  - _Requirements: 6.1–6.2_
- [ ] 6.2 验证：workflow_dispatch 三平台全绿、产三类 artifact
  - _Requirements: 6.1–6.2_

## 阶段 7：文档收尾

- [x] 7.1 `docs/desktop.md` 三平台化；`CLAUDE.md`、`README.md`、`docs/README.md`、`THIRD_PARTY_NOTICES.md`、`plans/ROADMAP.md` 同步
  - _Requirements: 7.1–7.2_
- [ ] 7.2 全量验证（ruff、快速 pytest、node --check、messages/assets/文档链接检查）并提交
  - _Requirements: 7.2_
