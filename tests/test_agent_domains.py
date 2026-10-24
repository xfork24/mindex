"""智能助手工具领域的动态加载测试。"""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from mindex.agent.domains import touch
from mindex.agent.llm.scripted import ScriptedLlm, call, say
from mindex.agent.registry import ToolOutput, ToolRegistry, ToolSpec
from mindex.agent.runtime import BuiltinRuntime
from mindex.agent.session_store import SessionStore
from mindex.agent.tools import meta
from mindex.shared.agent_domains import DOMAIN_INFO, LOADABLE_DOMAINS, PAGE_DOMAIN
from mindex.shared.config import AgentConfig, StrictModel

# 合法领域 + meta + 未知名称，覆盖需要忽略的输入。
DOMAIN_NAMES = st.one_of(
    st.sampled_from(LOADABLE_DOMAINS),
    st.just("meta"),
    st.sampled_from(["unknown", "", "Studio", "admin"]),
    st.text(max_size=8),
)


def reference_touch(loaded: list[str], domains: list[str], limit: int = 3) -> list[str]:
    """参考模型：从最近端往回取不重复的合法领域，取满 limit 个后恢复为旧→新顺序。"""
    recent: list[str] = []
    for domain in reversed([*loaded, *domains]):
        if domain in LOADABLE_DOMAINS and domain not in recent:
            recent.append(domain)
    return list(reversed(recent[:limit]))


# Feature: agent-capability-expansion, Property 1: 领域 LRU
@settings(max_examples=100, deadline=None)
@given(loaded=st.lists(DOMAIN_NAMES, max_size=6), domains=st.lists(DOMAIN_NAMES, max_size=8))
def test_touch_lru_property(loaded: list[str], domains: list[str]) -> None:
    """**Validates: Requirements 1.3, 1.4, 1.5**"""
    loaded_before = list(loaded)
    domains_before = list(domains)

    result = touch(loaded, domains)

    # 不修改入参
    assert loaded == loaded_before
    assert domains == domains_before
    # 最多 3 个、无重复、不含 meta 与未知领域
    assert len(result) <= 3
    assert len(set(result)) == len(result)
    assert all(domain in LOADABLE_DOMAINS for domain in result)
    assert "meta" not in result
    # 最后被 touch 的合法领域在末尾
    valid_touched = [domain for domain in domains if domain in LOADABLE_DOMAINS]
    if valid_touched:
        assert result[-1] == valid_touched[-1]
    # 结果恰为最近使用的那几个领域
    assert result == reference_touch(loaded, domains)


def test_touch_examples() -> None:
    assert touch([], ["studio"]) == ["studio"]
    assert touch(["studio", "library"], ["studio"]) == ["library", "studio"]
    assert touch(["studio", "library", "search"], ["persons"]) == ["library", "search", "persons"]
    assert touch(["studio"], ["meta", "bogus"]) == ["studio"]
    assert touch(["studio", "library"], ["search"], limit=0) == []


# ---------------------------------------------------------------- 运行时：每步工具集合与未加载拦截
# 轻量运行时：真实 BuiltinRuntime + 临时 SessionStore + 合成注册表（真实 meta 工具 + 每个领域两个计数工具），
# 不启动完整应用，100 个样例也能很快跑完。


class NoArgs(StrictModel):
    pass


def synthetic_registry(counter: Counter) -> ToolRegistry:
    def handler(name: str):
        async def execute(args, ctx) -> ToolOutput:
            counter[name] += 1
            return ToolOutput({"tool": name}, text=f"{name} ok")
        return execute

    specs = list(meta.specs())
    for domain in LOADABLE_DOMAINS:
        for suffix in ("a", "b"):
            name = f"{domain}_{suffix}"
            specs.append(ToolSpec(name, domain, f"{domain} 测试工具", NoArgs, "read", handler(name)))
    return ToolRegistry(specs)


def run_turn(script: list, page: str | None, preloaded: list[str] = ()) -> tuple[ScriptedLlm, Counter, list, list]:
    """跑一轮脚本化对话，返回 (llm, 处理函数调用计数, 事件日志, 结束时的已加载领域)。"""
    counter: Counter = Counter()
    registry = synthetic_registry(counter)
    llm = ScriptedLlm(script)
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp))
        runtime = BuiltinRuntime(store, registry=registry, llm_factory=lambda: llm,
                                 client_factory=lambda resolve: None, settings=AgentConfig)

        async def main():
            sid = store.create("性质测试")["id"]
            if preloaded:
                store.update(sid, loaded_domains=list(preloaded))
            await runtime.prompt(sid, [{"type": "text", "text": "开始"}],
                                 context={"page": page} if page else None)
            await runtime.wait_idle(sid)
            return store.events(sid), runtime.loaded_domains(sid)

        events, loaded = asyncio.run(main())
    return llm, counter, events, loaded


META_TOOLS = {spec.name for spec in meta.specs()}
PAGES = st.one_of(st.none(), st.sampled_from(sorted(PAGE_DOMAIN)), st.just("unknown-page"))
DOMAIN_SETS = st.lists(st.sampled_from(LOADABLE_DOMAINS), min_size=1, max_size=3)
ACTIONS = st.one_of(
    st.tuples(st.just("load"), DOMAIN_SETS),
    st.tuples(st.just("tool"), st.sampled_from([f"{d}_{s}" for d in LOADABLE_DOMAINS for s in ("a", "b")])),
)


def expected_tools(loaded: list[str]) -> set[str]:
    names = set(META_TOOLS)
    for domain in loaded:
        names |= {f"{domain}_a", f"{domain}_b"}
    return names


# Feature: agent-capability-expansion, Property 2: 每步工具集合
@settings(max_examples=100, deadline=None)
@given(page=PAGES, actions=st.lists(ACTIONS, max_size=8))
def test_step_tools_match_loaded_domains(page: str | None, actions: list[tuple]) -> None:
    """**Validates: Requirements 1.2, 1.7**"""
    script = []
    for kind, value in actions:
        script.append([call("load_tools", domains=value)] if kind == "load" else [call(value)])
    script.append([say("完成")])

    llm, counter, events, final_loaded = run_turn(script, page)

    # 参考模型：轮次开始 touch 页面领域；load_tools 在本步生效、下一步可见；成功执行的领域工具 touch 其领域。
    loaded = touch([], [PAGE_DOMAIN[page]]) if page in PAGE_DOMAIN else []
    expected_per_step = [expected_tools(loaded)]
    executed: Counter = Counter()
    for kind, value in actions:
        if kind == "load":
            loaded = touch(loaded, value)
        else:
            domain = value.rsplit("_", 1)[0]
            if domain in loaded:
                loaded = touch(loaded, [domain])
                executed[value] += 1
        expected_per_step.append(expected_tools(loaded))

    assert len(llm.requests) == len(script)
    for request, expected in zip(llm.requests, expected_per_step, strict=True):
        names = [tool["name"] for tool in request.tools]
        assert len(names) == len(set(names))
        assert set(names) == expected
    assert final_loaded == loaded
    # 已加载领域的工具照常执行，未加载的一次也不执行
    assert counter == executed
    # 加载工具的事件按调用顺序落库
    loads = [e["data"] for e in events if e["type"] == "plugin:mindex/tools-loaded"]
    assert [item["domains"] for item in loads] == [value for kind, value in actions if kind == "load"]


@st.composite
def unloaded_call(draw):
    preloaded = touch([], draw(st.lists(st.sampled_from(LOADABLE_DOMAINS), max_size=4)))
    page = draw(PAGES)
    active = touch(preloaded, [PAGE_DOMAIN[page]]) if page in PAGE_DOMAIN else preloaded
    candidates = [d for d in LOADABLE_DOMAINS if d not in active]
    domain = draw(st.sampled_from(candidates))
    tool = f"{domain}_{draw(st.sampled_from('ab'))}"
    return preloaded, page, active, domain, tool


# Feature: agent-capability-expansion, Property 3: 未加载即不执行
@settings(max_examples=100, deadline=None)
@given(case=unloaded_call())
def test_unloaded_domain_tool_is_not_executed(case: tuple) -> None:
    """**Validates: Requirements 1.9**"""
    preloaded, page, active, domain, tool = case

    llm, counter, events, final_loaded = run_turn([[call(tool)], [say("完成")]], page, preloaded)

    assert counter[tool] == 0
    assert sum(counter.values()) == 0
    results = [e["data"] for e in events if e["type"] == "tool/result"]
    assert len(results) == 1
    error = results[0]["error"]
    assert error["code"] == "TOOL_NOT_LOADED"
    assert error["domain"] == domain
    assert results[0]["message"]["isError"] is True
    text = "".join(b["text"] for b in results[0]["message"]["content"])
    assert f'load_tools(domains=["{domain}"])' in text
    # 被拦截的调用不改变已加载领域，下一步工具集合也不含该领域
    assert final_loaded == active
    assert tool not in {t["name"] for t in llm.requests[-1].tools}


# ---------------------------------------------------------------- 示例：非法参数、跨轮/重启保持、领域目录


def make_runtime(data_dir: Path, llm: ScriptedLlm, counter: Counter | None = None) -> tuple[SessionStore, BuiltinRuntime]:
    store = SessionStore(data_dir)
    runtime = BuiltinRuntime(store, registry=synthetic_registry(counter if counter is not None else Counter()),
                             llm_factory=lambda: llm, client_factory=lambda resolve: None, settings=AgentConfig)
    return store, runtime


async def prompt_turn(runtime: BuiltinRuntime, sid: str, page: str | None = None) -> None:
    await runtime.prompt(sid, [{"type": "text", "text": "继续"}], context={"page": page} if page else None)
    await runtime.wait_idle(sid)


def test_load_tools_invalid_arguments_keep_loaded_domains() -> None:
    """**Validates: Requirements 1.8**"""
    invalid = [["meta"], ["unknown"], [], ["studio", "library", "search", "persons"], ["studio", "meta"]]
    script = [[call("load_tools", domains=value)] for value in invalid] + [[say("完成")]]

    llm, _, events, loaded = run_turn(script, None, ["library", "index"])

    results = [e["data"] for e in events if e["type"] == "tool/result"]
    assert len(results) == len(invalid)
    for result in results:
        assert result["error"]["code"] == "invalid_arguments"
        assert result["message"]["isError"] is True
    # 不加载任何领域，也不追加加载事件；每步工具集合保持不变
    assert loaded == ["library", "index"]
    assert not [e for e in events if e["type"] == "plugin:mindex/tools-loaded"]
    for request in llm.requests:
        assert {t["name"] for t in request.tools} == expected_tools(["library", "index"])


def test_loaded_domains_persist_across_turns_and_restart() -> None:
    """**Validates: Requirements 1.6**"""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        first_llm = ScriptedLlm([[call("load_tools", domains=["search", "persons"])], [say("已加载")], [say("第二轮")]])
        store, runtime = make_runtime(data_dir, first_llm)

        async def first() -> str:
            sid = store.create("跨轮")["id"]
            await prompt_turn(runtime, sid, "studio")
            await prompt_turn(runtime, sid)  # 第二轮无页面上下文
            return sid

        sid = asyncio.run(first())
        # 第二轮沿用会话记住的页面上下文，开头再次 touch studio
        assert runtime.loaded_domains(sid) == ["search", "persons", "studio"]
        # 第二轮的首个请求仍带着上一轮加载的领域
        assert len(first_llm.requests) == 3
        assert {t["name"] for t in first_llm.requests[2].tools} == expected_tools(["studio", "search", "persons"])

        # 模拟服务重启：同一数据目录上新建存储与运行时
        restart_llm = ScriptedLlm([[call("persons_a")], [say("重启后")]])
        counter: Counter = Counter()
        store2, runtime2 = make_runtime(data_dir, restart_llm, counter)
        assert store2.get(sid)["loaded_domains"] == ["search", "persons", "studio"]
        asyncio.run(prompt_turn(runtime2, sid))

        assert {t["name"] for t in restart_llm.requests[0].tools} == expected_tools(["studio", "search", "persons"])
        assert counter["persons_a"] == 1  # 重启后已加载领域的工具直接可用
        assert runtime2.loaded_domains(sid) == ["search", "studio", "persons"]


def test_system_prompt_lists_domain_catalog() -> None:
    """**Validates: Requirements 1.10**"""
    llm, _, _, loaded = run_turn([[call("load_tools", domains=["index"])], [say("完成")]], "admin")
    assert loaded == ["library", "index"]
    assert len(llm.requests) == 2

    for request, active in zip(llm.requests, [{"library"}, {"library", "index"}], strict=True):
        lines = request.system.splitlines()
        for domain in LOADABLE_DOMAINS:
            state = "已加载" if domain in active else "未加载"
            line = next(item for item in lines if item.startswith(f"- {domain}（"))
            assert line.startswith(f"- {domain}（{state}）：{DOMAIN_INFO[domain]}。")
            assert f"代表工具：{domain}_a、{domain}_b" in line
        assert "load_tools" in request.system
        assert "3 个领域" in request.system
        assert "TOOL_NOT_LOADED" in request.system
