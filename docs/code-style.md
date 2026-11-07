# 代码规范与检查

本仓库参考 [Google Python](https://google.github.io/styleguide/pyguide.html)、
[Google JavaScript](https://google.github.io/styleguide/jsguide.html) 与
[Google HTML/CSS](https://google.github.io/styleguide/htmlcssguide.html) 规范。
排版、语言规则、依赖装配和运行行为分别验证；格式检查通过不能证明重构没有改变行为。

## 项目约定

- Python 使用 4 空格、110 列、双引号与 Ruff。JS/HTML/CSS 使用 2 空格，Prettier 建议行宽为 100，JS 使用双引号与分号。
  这些是原需求已确认的项目选择，不机械套用参考规范中的 80 列和 JS 单引号。
  `.prettierrc` 还显式固定空格、LF、多行尾逗号、箭头函数参数括号和对象括号内空格；
  `quoteProps: consistent` 保证同一对象需要引用某个属性名时，其余属性名也使用引号。
- Python 源码以 `mindex` 安装，组件统一使用绝对短名，例如 `from shared.media import probe`。
  安装入口由 `mindex` 根包完成查询路径自举，再调用短名组件；独立脚本也须先完成自举。
  禁止 `mindex.*` 子模块导入与相对导入。独立进程的 `python -m mindex.<模块>` 是自举入口的例外。
- 模块与包需要 docstring；使用真正的三引号字符串，不能把 docstring 写成多个相邻字面量。
  拆分普通长字符串使用相邻字面量，并逐字保持运行时内容，尤其是提示词、SQL 和内嵌 JS。
- 导入按标准库、第三方与首方分组。标准库导入置顶；`resource` 等平台专属模块按平台条件加载。
  类和类型的明确命名导入沿用现有项目形式。需要晚绑定的函数、工厂、类和可变常量通过模块属性访问，
  例如 `deployment.ADAPTER_VERSION`，避免提升导入后冻结旧值或绕过测试补丁。
- JS 控制结构使用花括号，一条声明只定义一个变量，优先 `const`，需要重赋值时使用 `let`，禁止 `var`。
  局部变量使用 camelCase；API 的 snake_case 字段保持原契约，必要时显式写出键值映射。
  保留用于同时判断 `null`/`undefined` 的 `== null` 与 `!= null`。
  创作页的经典脚本共享全局作用域，`check-studio-scripts.cjs` 按 HTML 加载顺序合并检查；
  跨文件重新赋值的变量必须保留 `let`，不能只根据一个文件里的引用判为 `const`。
- HTML 使用小写 HTML5 doctype 和 `utf-8`，编码声明在前 1024 字节内且位于脚本之前。
  静态业务页面不使用内联 `style`；重复属性、重复 ID 和缺失图片 `alt` 均视为缺陷。
- CSS 保持选择器与声明顺序，保留有效的回退声明和既有 `!important`。
  不为排序、缩写或消除重复选择器而改变级联结果。Stylelint 检查独立可判定的语法与有效性问题。

## 延迟导入

延迟导入必须有具体的 `# 延迟导入：原因` 注释。连续的一组导入可以共用一条原因说明。
允许的情况包括：可选模型或协议依赖、已识别的循环导入、CLI 帮助启动成本、安装入口自举、
阶段注册顺序，以及必须保留的局部绑定或异常降级范围。不能把普通必装依赖随意放进函数体。

工作区的阶段注册顺序有语义，`studio/workspace/` 和 `handlers/` 的包入口保留显式装配顺序；
局部的 isort 豁免须说明这一原因。不能用一次“先导入所有模块”的冒烟代替冷启动验证。

测试可以在用例或夹具中按需导入被测组件，以保留 monkeypatch 时机和可选后端的环境门控；
标准库仍置顶。可选依赖的导入失败测试、真实服务、跨进程任务和普通业务调用必须分别验证。

## 工具和范围

```bash
uv sync --extra dev --extra person
npm ci --ignore-scripts --no-audit --no-fund
uv run ruff check backend/src tests scripts eval
uv run ruff format --check backend/src tests scripts eval
uv run python scripts/check-code-style.py
npm run format:check
npm run lint:js
npm run lint:css
npm run test:js
uv run python scripts/build-messages.py --check
uv run python scripts/build-assets.py --check
```

Python 模块文档由 Ruff 的 `D100`/`D104` 检查；导入位置、短导入、延迟原因和静态 HTML
结构由 `check-code-style.py` 检查。该脚本也补查 Ruff 单词豁免之外的 110 列显示宽度，
保留长 URL 和已有带理由的 `E501` 例外。ESLint 与 Stylelint 补足 Prettier 不检查的语言和 CSS 规则。
Node 工具版本固定在根目录 `package-lock.json`，CI 使用 `npm ci` 安装。

生成的 `messages.js`、第三方库、模型权重、依赖、缓存与构建产物不参与源码格式化。
根目录模型权重排除必须写成 `/models`，不能用 `models` 排除同名源码包。
渲染覆盖模板有注入标记，Electron 启动页需要自包含；这些文件不适用静态业务页面的样式分离检查。

改动 JS/CSS 后运行 `scripts/build-assets.py`；改动文案后先生成 `messages.js`。
回归分组、浏览器环境和外部后端门控见[测试约定](testing.md)。目录或导入变动还需验证
CLI、全模块冷启动、wheel 资源及冻结后端，不能只运行进程内测试。

旧版嵌入适配器把自身源码哈希纳入空间身份，源码排版变化也会改变该哈希。
保留现有身份与兼容性校验，不通过伪造旧哈希绕过检查；真实模型与既有索引仍须按现有门槛验收。
