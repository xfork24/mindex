"""拆分镜全链路评测（eval/split_eval.py）的指标测试。"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

EVAL_PATH = Path(__file__).resolve().parents[1] / "eval" / "split_eval.py"


def load_split_eval():
    """按文件路径加载 eval/split_eval.py（eval/ 不是包），同一进程内只加载一次。"""
    name = "split_eval"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, EVAL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


split_eval = load_split_eval()


@pytest.fixture
def evaluator():
    return split_eval


# ---- 生成器：分镜文本由已知数量的英文词、汉字与纯标点词元拼成，便于独立计算参考值 ----

ENGLISH = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCXYZ0123456789", min_size=1, max_size=8)
HAN = st.text(alphabet="山水城市夜景灯塔美食人群老街", min_size=1, max_size=6)
PUNCT = st.sampled_from(["—", ",", "-", "…", "·", "，"])
SPACE = st.text(alphabet=" \t\n", max_size=3)


@st.composite
def shot_text(draw):
    """返回 (文本, 英文词数, 汉字数)。"""
    tokens, words, han = [], 0, 0
    for _ in range(draw(st.integers(0, 8))):
        kind = draw(st.sampled_from(["en", "han", "punct"]))
        if kind == "en":
            tokens.append(draw(ENGLISH))
            words += 1
        elif kind == "han":
            token = draw(HAN)
            tokens.append(token)
            han += len(token)
        else:
            tokens.append(draw(PUNCT))
    if not tokens:
        tokens, words = [draw(ENGLISH)], 1
    return " ".join(tokens), words, han


def pad(draw, text):
    return draw(SPACE) + text + draw(SPACE)


@st.composite
def case_result(draw):
    """返回 (结果字典, 参考吻合, 实际分镜的 (英文词数, 汉字数) 列表)。"""
    shots = draw(st.lists(shot_text(), min_size=1, max_size=5))
    expected = [text for text, _, _ in shots]
    mode = draw(st.sampled_from(["same", "padded", "count", "content", "failed"]))
    if mode == "failed":
        result = {"id": "c", "status": "failed", "error": "拆分镜调用失败：超时", "matched": False,
                  "expected": expected, "actual": None}
        return result, False, []
    measured = [(w, h) for _, w, h in shots]
    if mode == "same":
        actual, matched = list(expected), True
    elif mode == "padded":
        actual, matched = [pad(draw, t) for t in expected], True
    elif mode == "count":
        extra, w, h = draw(shot_text())
        if draw(st.booleans()) or len(expected) == 1:
            actual, measured = expected + [extra], measured + [(w, h)]
        else:
            actual, measured = expected[:-1], measured[:-1]
        matched = False
    else:  # 镜数相同，但某一镜内容不同（追加一个英文词）
        i = draw(st.integers(0, len(expected) - 1))
        word = draw(ENGLISH)
        actual = list(expected)
        actual[i] = actual[i] + " " + word
        measured[i] = (measured[i][0] + 1, measured[i][1])
        matched = False
    result = {"id": "c", "status": "ok", "error": None, "matched": matched, "expected": expected,
              "actual": [{"text": t, "visual": "画面", "query": "查询"} for t in actual]}
    return result, matched, measured


# Feature: studio-material-consistency, Property 21: 评测指标
@settings(max_examples=100, deadline=None)
@given(st.lists(case_result(), max_size=8))
def test_property_21_metrics(cases):
    results = [r for r, _, _ in cases]

    # case_matches：只有首尾空白不同时吻合；镜数或内容不同、失败用例都不吻合
    for result, matched, _ in cases:
        assert split_eval.case_matches(result["expected"], split_eval.actual_texts(result)) is matched

    # match_rate = 吻合数 ÷ 总数 × 100，取值 [0, 100]，空集为 0.0
    rate = split_eval.match_rate(results)
    if not cases:
        assert rate == 0.0
    else:
        expected_rate = Fraction(sum(1 for _, m, _ in cases if m) * 100, len(cases))
        assert abs(Fraction(rate) - expected_rate) < Fraction(1, 10**9)
    assert 0.0 <= rate <= 100.0

    # average_seconds = 各镜（英文词/2.5 + 汉字/4）的平均值，保留 1 位小数；失败用例不计
    durations = [Fraction(w) / Fraction(5, 2) + Fraction(h, 4) for _, _, ms in cases for w, h in ms]
    average = split_eval.average_seconds(results)
    assert average == round(average, 1)
    if not durations:
        assert average == 0.0
    else:
        exact = sum(durations) / len(durations)
        assert abs(Fraction(average) - exact) <= Fraction(1, 20) + Fraction(1, 10**9)


# ---- 单元测试：吻合度五种情形（需求 11.6） ----


def ok_result(expected, actual):
    return {"id": "c", "status": "ok", "error": None, "expected": expected,
            "actual": [{"text": t, "visual": "画面", "query": "查询"} for t in actual]}


@pytest.mark.parametrize(
    ("expected", "actual", "matched"),
    [
        (["A b c,", "d e f."], ["A b c,", "d e f."], True),               # 完全一致
        (["A b c,", "d e f."], ["  A b c,\n", "\td e f.  "], True),       # 仅首尾空白不同
        (["A b c,", "d e f."], ["A b c, d e f."], False),                 # 镜数不同
        (["A b c,", "d e f."], ["A b", "c, d e f."], False),              # 镜数相同但边界不同
    ],
    ids=["identical", "whitespace", "count", "boundary"],
)
def test_case_matches_cases(evaluator, expected, actual, matched):
    assert evaluator.case_matches(expected, actual) is matched
    assert evaluator.match_rate([ok_result(expected, actual)]) == (100.0 if matched else 0.0)


def test_match_rate_empty_case_set(evaluator):
    assert evaluator.match_rate([]) == 0.0
    assert evaluator.metrics([]) == {"match_rate": 0.0, "short_shots": 0, "average_seconds": 0.0}


def test_failed_case_never_matches(evaluator):
    failed = {"id": "c", "status": "failed", "error": "拆分镜调用失败：超时", "expected": ["a"], "actual": None}
    assert evaluator.case_matches(["a"], None) is False
    assert evaluator.match_rate([failed, ok_result(["a b c"], ["a b c"])]) == 50.0


# ---- 用例集（需求 11.1） ----

SENTENCE_57 = (
    "From world-class airport marvels and tranquil UNESCO greenery to savory local dishes and dazzling "
    "night views, Singapore delivers an unforgettable blend of experiences for every traveler."
)
SENTENCE_58 = (
    "Welcome to Singapore — the Lion City, where nature, culture, and modern life come together "
    "in the most beautiful ways."
)


def test_case_set_size_and_required_sentences(evaluator):
    cases = evaluator.load_cases(evaluator.DEFAULT_CASES)
    assert 12 <= len(cases) <= 16
    assert all({"id", "text", "expected", "note"} <= set(case) for case in cases)
    assert len({case["id"] for case in cases}) == len(cases)
    by_text = {case["text"]: case["expected"] for case in cases}
    assert len(by_text[SENTENCE_57]) == 5
    assert by_text[SENTENCE_58] == [
        "Welcome to Singapore — the Lion City,",
        "where nature, culture, and modern life come together in the most beautiful ways.",
    ]
    # 每条用例的期望切分本身应覆盖原文（去空白后拼接一致）。
    for case in cases:
        assert "".join("".join(case["expected"]).split()) == "".join(case["text"].split()), case["id"]


# ---- 报告路径（需求 11.5） ----


def test_report_path_naming(evaluator, tmp_path):
    path = evaluator.report_path(tmp_path, datetime(2026, 10, 26, 9, 5, 7))
    assert path == tmp_path / "eval-split-20261026-090507.json"
    assert re.fullmatch(r"eval-split-\d{8}-\d{6}\.json", evaluator.report_path(tmp_path).name)


# ---- main / run：替身驱动的端到端（全部写入 tmp_path） ----

CASE_TEXT = "港口清晨的渔船靠岸了。山顶日出云海翻涌壮观。老街夜市灯火通明热闹。"


def write_cases(tmp_path, cases=None):
    path = tmp_path / "cases.jsonl"
    cases = cases or [{"id": "c1", "text": CASE_TEXT, "expected": [
        "港口清晨的渔船靠岸了。", "山顶日出云海翻涌壮观。", "老街夜市灯火通明热闹。"], "note": "测试"}]
    path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8")
    return path


def read_report(path):
    return json.loads(path.read_text(encoding="utf-8"))


class CountingSearcher:
    """召回替身：按调用顺序依次返回 counts 中指定条数的结果。"""

    def __init__(self, counts):
        self.counts = list(counts)
        self.requests = []

    def text(self, request):
        self.requests.append(request)
        n = self.counts[len(self.requests) - 1]
        return {"results": [
            {"source_name": f"素材{i}", "start": float(i), "end": float(i) + 1.5,
             "annotation": {"description": f"画面描述{i}"}}
            for i in range(n)
        ]}


def test_main_without_materials_writes_report_to_output(evaluator, config, tmp_path, capsys):
    from conftest import FixtureStoryboarder

    boarder = FixtureStoryboarder(config)
    output = tmp_path / "out" / "report.json"
    code = evaluator.main(["--cases", str(write_cases(tmp_path)), "--output", str(output)],
                          config=config, storyboarder=boarder)
    assert code == 0
    report = read_report(output)
    assert report["metrics"]["match_rate"] == 100.0
    assert report["materials"] is None and report["recall"] is False
    case = report["cases"][0]
    assert case["status"] == "ok" and case["matched"] is True
    assert all({"text", "visual", "query"} <= set(shot) for shot in case["actual"])
    assert boarder.vocabularies == [None]
    assert "切分吻合度 100.0%" in capsys.readouterr().out
    assert not list(config.data_dir.glob("eval-split-*.json"))


def test_main_recall_lists_top3_fewer_and_none(evaluator, config, tmp_path):
    from conftest import FixtureStoryboarder

    searcher = CountingSearcher([5, 2, 0])
    output = tmp_path / "report.json"
    code = evaluator.main(["--cases", str(write_cases(tmp_path)), "--recall", "--output", str(output)],
                          config=config, storyboarder=FixtureStoryboarder(config), searcher=searcher)
    assert code == 0
    shots = read_report(output)["cases"][0]["actual"]
    assert [len(s["recall"]) for s in shots] == [3, 2, 0]
    assert shots[0]["recall"][0] == {"source_name": "素材0", "start": 0.0, "end": 1.5, "description": "画面描述0"}
    assert "recall_note" not in shots[0] and "recall_note" not in shots[1]
    assert shots[2]["recall_note"] == evaluator.NO_RECALL == "该镜无召回结果"
    assert [r.text for r in searcher.requests] == [s["query"] for s in shots]
    assert all(r.limit == 3 for r in searcher.requests)


def test_main_missing_job_exits_nonzero_without_split(evaluator, config, tmp_path, capsys):
    from conftest import FixtureStoryboarder

    boarder = FixtureStoryboarder(config)
    output = tmp_path / "report.json"
    code = evaluator.main(["--cases", str(write_cases(tmp_path)), "--materials", "no-such-job",
                           "--output", str(output)], config=config, storyboarder=boarder)
    assert code != 0
    assert boarder.calls == 0
    assert not output.exists()
    assert "找不到创作任务：no-such-job" in capsys.readouterr().err


def test_main_empty_digest_logs_reason_and_splits_without_vocabulary(evaluator, config, tmp_path, capsys):
    from conftest import FixtureStoryboarder

    from mindex.studio import studio
    from mindex.studio.material_digest import EMPTY_INDEX

    job = studio.create_job(config, "测试大纲")  # 临时数据目录中没有生效索引
    boarder = FixtureStoryboarder(config)
    output = tmp_path / "report.json"
    code = evaluator.main(
        ["--cases", str(write_cases(tmp_path)), "--materials", job["id"], "--recall", "--output", str(output)],
        config=config, storyboarder=boarder, searcher=CountingSearcher([1, 1, 1]),
    )
    assert code == 0
    assert boarder.vocabularies == [None]
    out = capsys.readouterr().out
    assert "素材摘要为空" in out and EMPTY_INDEX in out
    assert read_report(output)["materials"]["empty_reason"] == EMPTY_INDEX


@pytest.fixture
def indexed(config, video_file):
    from conftest import FixtureAnalyzer, FixtureEmbedder

    from mindex.processing.indexing import IndexBuilder
    from mindex.processing.pipeline import Pipeline

    embedder = FixtureEmbedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    source_id = pipeline.prepare(video_file)
    pipeline.analyze(source_id)
    IndexBuilder(config, embedder).build(activate=True)
    return config, pipeline.store


def test_run_materials_passes_vocabulary_to_storyboarder(evaluator, indexed, tmp_path):
    from conftest import FixtureStoryboarder

    from mindex.studio import studio
    from mindex.studio.material_digest import build_digest, merged_vocabulary

    config, store = indexed
    job = studio.create_job(config, "测试大纲", store=store)
    expected_vocabulary = merged_vocabulary(build_digest(config, store, studio.load_job(config, job["id"])))
    assert expected_vocabulary

    job_file = studio.job_path(config, job["id"])
    before = job_file.read_bytes()
    boarder = FixtureStoryboarder(config)
    logs = []
    cases = evaluator.load_cases(write_cases(tmp_path))
    report = evaluator.run(config, cases + cases, job_id=job["id"], storyboarder=boarder, store=store,
                           log=logs.append)
    assert boarder.vocabularies == [expected_vocabulary, expected_vocabulary]
    assert report["materials"]["empty_reason"] is None
    assert report["materials"]["stats"]["sources"] == 1
    assert any(line.startswith("素材摘要：") for line in logs)
    assert job_file.read_bytes() == before  # 只读：job.json 不变
