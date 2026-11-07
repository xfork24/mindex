# 代码规范化重构计划(参照 Google Style Guides,不改逻辑)

> 状态:已执行完毕(2026-11-07,含两项追加需求与一次事故重放,见「追加需求」与「事故与重放」节)。分支 `style-normalization`。
> 执行结果摘要:722 文件 +79,531/-30,929 行;函数内 import 提升千余处;E501 3,271→0;
> 实施中的关键决策与发现见文末「执行附记」。

## Context

系统性修复所有源码文件的编码不规范问题(典型如 Python import 散布在函数体内),参照 Google Python/JS/HTML+CSS 规范,**只改风格、不改逻辑**。

三路并行审计已完成,关键发现:

- **Python**(`backend/src` 334 文件):191 个文件共 1,331 处函数内 import(~88 处纯 stdlib 可零风险提升;~67 处可选依赖守卫应保留;24 文件的 `TYPE_CHECKING` 合规;其余首方导入需逐个验证循环依赖);`cli.py` 重复导入 `Pipeline` 12 次、`shared/storage.py` 重复导入 `scaling_schema` 9 次;2 处导入分组违规(`models/adapters.py:8`、`models/compatibility.py:5`);1 个全大写函数名(`shared/db/schema_pg.py:412`);~116 处单引号异常;@110 行宽超限 1,272 行;14 个文件缺模块 docstring
- **JS**:引号风格分裂(双引号 86% / 单引号 ~3,900 处集中在 15 个文件);`multiselect.js` 11 处 `var` + 压缩单行块;2 处 snake_case 局部变量;缩进/分号/命名整体干净
- **HTML**:1 个真实 bug(`login.html:15` 重复 `class` 属性);charset meta 位置靠后;doctype/charset 大小写;395 行超长
- **CSS**:~1,007 条单行压缩规则(Google 要求每声明一行)
- 全仓库无 JS/CSS 工具配置;ruff 未覆盖 `scripts/`、`eval/`,E501 未启用

## 已确认决策

1. **行宽**:Python 保持 110(修复超限行),JS/HTML 用 Prettier printWidth=100
2. **范围**:全部 — backend/src、frontend/src、desktop、scripts/、eval/、tests/(285 py + 64 cjs)
3. **JS 引号**:统一双引号
4. **工具链**:引入配置并接入 CI 防回退

**始终排除**:`frontend/src/shared/messages.js`(生成文件)、`marked.umd.js`、`purify.min.js`(vendored)、`node_modules`、`build/`、`desktop/dist`、`.venv*`、`models/`、所有缓存目录。

## 实施步骤

### Phase 0: 准备
- 从 `scaling-refactor`(当前 clean)创建分支 `style-normalization`;不主动 commit,默认保留工作区改动供审查
- 记录基线:`uv run ruff check backend/src tests`、`node --check` 清单、desktop `npm test` 当前通过状态

### Phase 1: 工具配置
- `pyproject.toml`:`select` 增加 `"E501"`(line-length=110 生效);`src` 视需要加入 `tests`(修正测试辅助模块的首方归类);处理 `scripts/agent_eval.py` 从 tests 导入的 isort 归类
- 新增根 `.prettierrc`:`printWidth: 100`、`singleQuote: false`、`semi: true`、2 空格,其余默认;新增 `.prettierignore`(上述排除目录/文件 + `backend/src/studio/overlays/layer.html`,见 Phase 4)
- CI(`.github/workflows/ci.yml`):ruff 行改为 `ruff check backend/src tests scripts eval`;新增 `ruff format --check backend/src tests scripts eval`;新增 prettier 步骤(`npx prettier@<固定版本> --check`,覆盖 frontend/src、desktop、tests/*.cjs、scripts/*.cjs)
- 更新 `AGENT.md` 预提交命令清单

### Phase 2: Python 修复(主体)
按包分批执行,每批后立即冒烟验证:

1. **机械修复**:`ruff check --fix`(导入排序,含 adapters.py/compatibility.py 分组);`ruff format` 覆盖 backend/src、tests、scripts、eval(统一双引号、修 ~116 处单引号异常及格式)
2. **import 提升战役**(用户核心诉求,逐文件处理函数内 import):
   - stdlib 导入(~88 处)全部提升到顶部 → 零风险
   - 未守卫的必装三方依赖 → 提升
   - **保留不动**:try/except 守卫的可选依赖(playwright/mcp/psycopg/boto3 探测)、`TYPE_CHECKING` 块、`models/adapters.py` 文档声明的工厂按需导入
   - 首方 `mindex.*` 导入:尝试提升;每包批后跑全量模块导入冒烟(pkgutil 遍历 import 所有 `mindex.*` 子模块)暴露循环依赖,失败处回退为懒加载并加一行注释说明原因
   - 去重:`cli.py` Pipeline×12、`storage.py` scaling_schema×9、`studio/workspace/post.py` ass/brands/concat_media 各 3-4 次 → 提升为单次顶部导入
   - `cli.py:802` `import logging as _logging` → 提升为顶部 `import logging`(视局部命名冲突情况)
   - 同样策略扫 tests/、scripts/、eval/ 中的函数内 import
3. **行宽**:开启 E501 后逐条修复(format 自动换行后剩余的长字符串/注释用相邻字面量拼接或重排列注释;字符串内容不变;总数以实际扫描为准,backend/src ~1,272 + tests/scripts/eval 待扫)
4. **命名**:`DELIVERY_PLATFORMS_SEED` → `delivery_platforms_seed`(`shared/db/schema_pg.py:412` 及调用点);保留 `file_response as FileResponse` 别名(有意的 FastAPI 命名对齐)与 scripts 的 kebab-case 文件名(按路径运行,重命名会破坏 CI 引用)
5. **模块 docstring**:为 14 个缺失文件补一行中文模块 docstring(`api.py`、`cli.py`、`processing/pipeline.py`、`processing/indexing.py`、`admin/admin.py`、`shared/media.py`、`shared/config.py`、`shared/types.py`、`search/api.py`、`search/search.py`、`diagnostics/benchmark.py`、`diagnostics/evaluation.py`、`models/analyzers.py`、`persons/person_api.py`)。函数级 docstring 不在本次范围(数百个、属内容创作,列为可选后续)
6. scripts/、eval/ 首次纳入 ruff 后暴露的存量违规(UP/B/PLW 等)在小文件内机械修复;已有 `# noqa: BLE001`(47 处)与 `# noqa: F401`(11 处)全部保留

### Phase 3: JS 修复
- Prettier 全量格式化(双引号、printWidth 100):`frontend/src/*/*.js`(排除 vendored/生成)、`desktop/{*.js,lib/*.js,pages/*.js,test/*.js,scripts/*.js}`、`tests/*.cjs`、`scripts/capture-readme-screenshots.cjs`
- `multiselect.js`:`var`→`const`/`let`(11 处);修复 37-38、47-48 行的无花括号 for + 压缩单行块(加花括号换行,不改语义)
- `settings.js:1231` `channel_weights`→`channelWeights`、`studio-stages.js:606` `inputs_by_shot`→`inputsByShot`(局部变量重命名)
- **保留**:24 处 `== null`/`!= null`(Google JS 指南明确允许该惯用法);`shot-player.js` 的非 defer 加载(功能性选择,非风格问题)

### Phase 4: HTML/CSS 修复
- `login.html:15`:删除重复的第二个 `class` 属性(HTML 规范浏览器取首个,删除后渲染与现状完全一致;先核对 `.login-panel` CSS 是否存在,结果写进总结)
- `settings.html:132` 内联 `style=` → 迁移为 settings.css 类(声明原样搬移)
- 全部 15 页:`<!doctype html>`→`<!DOCTYPE html>`、charset 值改小写 `utf-8`、charset meta 移到首个 `<script>` 之前(1024 字节规范要求)
- Prettier 格式化 HTML(超长行换行、属性引号规范化;`data-i18n` 单引号/实体转义两种写法归一,DOM 属性值不变)与 CSS(~1,007 条单行规则展开为每声明一行)
- `layer.html`(渲染覆盖模板)默认排除:内含 `/*MINDEX_DATA*/` 注入标记与自包含内联脚本,先实现时核实注入逻辑再决定是否纳入

### Phase 5: 端到端验证(全部通过才算完成)
1. `uv run ruff check backend/src tests scripts eval`(含新 E501)✅
2. `uv run ruff format --check backend/src tests scripts eval` ✅
3. `npx prettier --check`(对应清单)✅
4. **循环导入冒烟**:pkgutil 遍历导入全部 `mindex.*` 子模块 ✅
5. `uv run mindex --help`(CLI 启动正常)
6. `bash scripts/typecheck.sh`(mypy shared+models 基线不回退)
7. `uv run pytest --ci-suite fast -x -q --timeout=120`(标准快速回归)
8. `uv run python scripts/check-doc-links.py`、`build-messages.py --check`、`build-assets.py --check`(证明生成文件未被误改)
9. `node --check`(CI 同款清单)+ `cd desktop && npm test`
10. 浏览器冒烟(环境允许时):起后端跑 1-2 个 Playwright .cjs(登录页验证 login.html、persons 页验证 multiselect)

### Phase 6: 汇报
按类别统计(import 提升 X 处、去重 Y 处、行宽 Z 行、引号 N 处、CSS 规则展开 M 条等),列出所有保留不改的特例及原因。

## 风险与缓解
- **import 提升暴露循环依赖** → 按包分批 + 全量导入冒烟,失败即回退该处并注释
- **大 diff 难复核** → 按类型分批、每批 git diff 抽查;format/prettier 属确定性变更
- **长字符串折行引入文本变化** → 仅相邻字面量拼接、不改内容;fast 测试兜底
- **CSS/HTML 格式化改变级联** → 只动格式不动选择器顺序/`!important`(31 处 `!important` 与 5 组重复选择器保留不动——属渲染行为)

## 参考规范
- https://google.github.io/styleguide/pyguide.html
- https://google.github.io/styleguide/jsguide.html
- https://google.github.io/styleguide/htmlcssguide.html

## 执行附记(实施中发现的关键情况)

1. **ruff E501 按"显示宽度"计**:全角字符算 2 列,中文文本行字符数 ≤110 仍可能违规;修复按宽度预算折行(隐式拼接保证字符串值逐字节一致)。
2. **保留为懒加载的三类正当例外**(AGENT.md 约定已同步更新):
   - try/except 守卫的可选依赖探测(torch/transformers/mcp/playwright/psycopg 等);
   - CLI 入口文件(cli*.py)的重依赖(mindex.api 924ms/agent.tools 679ms/worker_runtime 609ms 等,经 importtime 实测,提升会使 `mindex --help` 从 0.28s 涨至 1.05s;回退后 0.39s);
   - 循环导入环根文件(shared/db/*、storage、files、queue_dispatch、upload_sessions、s3_multipart、processing/dialogue 等,经 pkgutil 全量导入冒烟逐个定位)与 `studio/workspace/` 注册顺序敏感包(REGISTRY 字典键序=导入序,提升 media_nodes 的 overlay 导入会改变阶段注册顺序)。
3. **monkeypatch 晚绑定**:27 个文件把 `from mindex.X import name` 改为 `from mindex import X` + `X.name()` 调用,保留测试对模块属性打补丁的能力(mcp_server/shot_media 等案例回归验证)。
4. **JS API 契约保护**:`inputs_by_shot`、`channel_weights` 等请求体字段保持 snake_case(仅局部变量改 camelCase);`data.business.search.channel_weights` 等 API 字段访问不动。
5. **测试源码锚点**:`tests/search_cascading_filters_unit.cjs` 以源码文本截取 app.js 代码段,prettier 引号归一后锚点已同步更新。
6. **login.html 重复 class 属性**:合并为 `class="panel login-panel"`——`.login-panel{max-width:28rem}` 样式此前因 HTML 重复属性被浏览器丢弃从未生效,此修复使登录表单按作者意图限宽(渲染行为变化,已知会)。
7. **CLI 启动耗时**:0.28s → ~0.39s(+0.1s,来自 stdlib 顶部导入与 config 模块 yaml 顶部导入),可接受。
8. `# noqa: E501` 零新增(所有超长行均真实折行);既有 `noqa: BLE001/F401` 全部保留。

## 追加需求(2026-11-07 用户批准)

1. **cli/ 包重构**:cli.py → `cli/__init__.py`,cli_*.py → `cli/{agent,clips,e2e,persons,studio,topics,uploads}.py`。
   - pyproject 入口 `mindex.cli:app` 不变(包导入兼容);mindex.spec 用 collect_submodules 自动适应。
   - tests/AGENT.md/CLAUDE.md/docs/architecture.md 同步;`test_import_boundaries` 登记 cli 为装配层(与 app 同级,不入 UPPER_MODULES;FORBIDDEN maintenance→{app,cli})。
   - 其余根目录孤立文件经评估保留原位:api/upload_api(API 装配)、gateway/desktop_entry(服务入口,PyInstaller 指纹)、doctor(诊断)、worker_*(worker 基础设施)。
2. **包内相对导入**:backend/src 内 2,000+ 处 `from mindex.x.y import z` 全部改为相对导入(如 `from shared.media import probe`),去掉 mindex 前缀。
   - 直接删前缀(`from shared.media import ...`)会制造双模块身份(mindex.shared 与 shared 并存,isinstance/注册表全部失效),故采用相对导入实现"精简"目标。
   - tests/scripts/eval 在包外,保持绝对导入(唯一正确形式);importlib 字符串、入口点、spec 保持绝对。
   - `test_import_boundaries` 扫描器升级:相对导入按文件包深度换算为绝对名后再比对(同等严格度)。

## 事故与重放(透明记录)

实施追加需求时一次误操作(`git checkout -- backend/src` 未区分暂存区)回退了 268 个未暂存文件的首轮修复。
经 dangling-commit 排查确认无可恢复的近终态后,按既定脚本流水线确定性重放:
format → import 提升(按包) → 循环消解(pkgutil 冒烟驱动) → monkeypatch 晚绑定 → 手工修复 → E501(修复器+手工) → docstring/命名 → 相对导入(修正点数公式后单次全量转换)。
重放中发现并修复的深层问题:
- 相对导入点数公式差一(跨包需 `..` 误生成为 `.`),以及 `from mindex.A import B`(子模块导入)被误解析到本包子模块——以 HEAD 为真值对照修复,混合绑定行按名字逐一定位拆分。
- 全部验证指标恢复:冒烟 333/0、E501=0、零 SyntaxWarning、mypy/边界测试/UI 冒烟全绿。

## 原始需求(2026-11-07 用户重申,以此为准)

1. **孤立文件归包**:backend/src 下的孤立 python 文件放入独立文件夹——cli 包(cli_*.py → cli/{agent,clips,e2e,persons,studio,topics,uploads}.py,已完成);其余孤立文件也归入合适文件夹(必要时新建包)。
2. **短导入(用户明确否决相对导入)**:所有 python 导入去掉 `mindex.` 前缀——`from mindex.xxx.yyy import zzz` → `from xxx.yyy import zzz`;`import mindex.xxx` → `import xxx`。**不要** `from ..shared.media import probe`,要写 `from shared.media import probe`(引用的是 mindex 包内部组件)。"修正查询根目录":调整 sys.path/打包/测试配置,使 `shared.*`、`models.*`、`cli.*` 等短名在开发、测试、wheel、桌面冻结全场景下可解析,并保持**单一模块身份**(全部代码统一用短名,禁止混用 `mindex.*` 子模块导入,防止 `shared` 与 `mindex.shared` 双身份破坏 isinstance/注册表/单例)。

执行顺序:计划更新 → 孤立文件归包 → 全仓短导入转换 + 根目录修正 → 完整验证(smoke/ruff/E501/mypy/fast)。

## 追加需求二(2026-11-07 用户批准)

**包内模块去"包名_"前缀(递归)**:backend/src 下所有 `pkg/pkg_xxx.py` 改为 `pkg/xxx.py`,重名冲突时给冲突文件选合适新名;同步全部 import(含模块属性调用、monkeypatch 字符串目标)。清单(15 个,预检零冲突):

- settings/settings_api.py → settings/api.py
- studio/studio_{api,artifacts,brands_api,diagnostics,files,logs,models,music_api,render,select,steps,templates_api,tools_api,window}.py → studio/{api,artifacts,brands_api,diagnostics,files,logs,models,music_api,render,select,steps,templates_api,tools_api,window}.py

执行约束:与 E501 收尾代理的内容修改存在写盘竞争,待代理完成后执行;重命名用 git mv 保留历史;完成后跑全模块导入冒烟,再统一执行 fast 全量(用户要求:修复全部完成后一次性跑测试)。


## 最终交付状态(2026-11-07)

**验证结果(全部绿)**:
- ruff 全规则(check)+ format --check:backend/src tests scripts eval 全通过
- 模块导入冒烟:336 模块 0 失败,零双身份(sys.modules 无 mindex.* 子模块残留)
- mypy 基线(shared+models):99 文件无问题
- Prettier 全量 --check、node --check、desktop 单测 32/32:全过
- 生成物一致性:messages.js / 资源 ?v= / 文档链接 84 文件 0 失效
- pytest --ci-suite fast:**2154 passed / 0 failed**(经三轮回归+逐项修复后归零)
- CLI 启动:`mindex --help` ≈0.27s(与基线持平;短名自举无额外开销)

**最终形态**:
- 归包:cli/(8 文件)、api/(REST+uploads)、server/(gateway/desktop_entry)、worker/(runtime/handlers)、diagnostics/(doctor/desktop_check 并入)
- 短导入:全仓 `from shared.media import probe` 风格(查询根目录经 backend/src/__init__.py 自举 + conftest/scripts 引导 + mindex.spec 短包收集);跨进程 -m/-c 入口保留 `mindex.` 前缀(经包自举,内部短名)
- 去前缀:settings_api→settings/api、studio_*→studio/{api,artifacts,...} 等 15 文件,import 全同步
- 函数内 import:stdlib 与必装依赖全部顶部;保留三类规范例外并登记——try/except 守卫(75)、循环依赖环根(降级+注释)、测试晚绑定/monkeypatch 豁免
- E501=0(仅 2 处带理由 noqa);docstring 补齐 17 个模块;DELIVERY_PLATFORMS_SEED→delivery_platforms_seed 等命名修复
- JS/HTML/CSS:Prettier 全量、var→const、DOCTYPE/charset 规范、login.html 重复 class 修复(注意:.login-panel 样式因此首次生效,登录框按作者意图限宽)、内联 style 迁 CSS
- 工具链:pyproject(E501+first-party 短名+per-file-ignores)、.prettierrc/.prettierignore、CI ruff 扩至 scripts/eval + ruff format + prettier --check、AGENT.md 约定同步

**已知环境性失败(非回归)**:tests/test_persons.py::test_real_bytetrack_* 需 `uv sync --extra person`(supervision),本机未装,stash 基线同样失败。
