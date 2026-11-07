"""选题挖掘测试共用：确定性 RSS/Atom 样例、HTTP 桩、脚本化选题模型与已分析的颜色素材库。

只用于工程测试：颜色向量与脚本化模型不代表真实模型质量。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from email.utils import format_datetime

import httpx

# 发布时间相对当前时刻生成，让候选条目始终落在挖掘的时效窗口内。
NOW = datetime.now().astimezone().replace(microsecond=0)
PUB_1 = NOW - timedelta(hours=2)
PUB_2 = NOW - timedelta(days=1)
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>财经要闻</title>
<item><title>央行宣布&lt;b&gt;降息&lt;/b&gt; 25 个基点</title><link>https://news.example.com/a/1/</link>
<description><![CDATA[<p>央行今日宣布下调政策利率，<b>市场</b>反应积极。</p>]]></description>
<pubDate>{format_datetime(PUB_1)}</pubDate></item>
<item><title>红色消费品牌发布财报</title><link>https://news.example.com/a/2</link>
<description>营收同比增长。</description><dc:date>{PUB_2.isoformat()}</dc:date></item>
<item><title>  </title><link>https://news.example.com/empty</link></item>
</channel></rss>""".encode()

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>科技动态</title>
<entry><title>蓝色芯片发布会</title><link rel="alternate" href="https://tech.example.com/chip"/>
<link rel="enclosure" href="https://tech.example.com/chip.mp3"/><summary>新一代芯片发布。</summary>
<updated>2026-10-24T09:30:00Z</updated></entry>
</feed>""".encode()

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>历史</title></channel>
<item><title>考古新发现</title><link>https://history.example.com/x</link><dc:date>2026-10-20</dc:date></item>
</rdf:RDF>""".encode()

XXE = b"""<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel>
<item><title>&x;</title></item></channel></rss>"""


class Routes:
    """httpx.MockTransport 处理器：按 URL（不含查询串）返回固定内容，并记录全部请求。"""

    def __init__(self, routes: dict | None = None):
        self.routes = dict(routes or {})
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = str(request.url).split("?", 1)[0]
        value = self.routes.get(key)
        if value is None:
            return httpx.Response(404, text="not found")
        if callable(value):
            return value(request)
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, (dict, list)):
            return httpx.Response(200, json=value)
        return httpx.Response(200, content=value)


def install(monkeypatch, routes: dict | None = None) -> Routes:
    from topics.sources import base

    handler = Routes(routes)
    monkeypatch.setattr(base, "TRANSPORT", httpx.MockTransport(handler))
    return handler


class ScriptedTopicLlm:
    """脚本化选题模型：按系统提示词里的任务关键词路由到确定性的 JSON 回复，并记录每次请求。

    - 归纳卡片：每个候选簇一张卡片，标题取簇里第一条条目的标题；关键画面按标题里的颜色词给出；
    - 受众匹配：标题含"红"的卡片 0.9，其余 0.6；
    - 大纲：固定格式，推荐模板按标题选 hot/history/base（"不存在"出现时故意给出不存在的模板）。
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.fail_next: list[str] = []  # 依次返回的非法输出，用于测试校验与重试

    def __call__(self, system: str, parts: list[dict]) -> str:
        text = "\n".join(part.get("text", "") for part in parts)
        self.calls.append({"system": system, "text": text})
        if self.fail_next:
            return self.fail_next.pop(0)
        if "素材候选相关性核验" in system:
            payload = json.loads(parts[0]["text"].split("\n", 1)[1])
            decisions = []
            for candidate in payload["candidates"]:
                evidence = candidate["evidence"]
                color = next((c for c in ("红", "蓝", "绿", "紫") if c in candidate["visual"]), None)
                matched = color is None or color in evidence
                decisions.append(
                    {
                        "candidate": candidate["candidate"],
                        "verdict": "matched" if matched else "rejected",
                        "reason": "画面主体相符" if matched else "画面颜色不符",
                        "quote": json.loads(evidence)["description"] if matched else "",
                    }
                )
            return json.dumps({"decisions": decisions}, ensure_ascii=False)
        if "归纳为选题卡片" in system:
            clusters = re.findall(r"【候选簇 (\d+)】\n- (?:\[[^\]]*\] )?([^\n（]+)", text)
            cards = []
            for index, title in clusters:
                color = next((c for c in ("红", "蓝", "绿") if c in title), "绿")
                cards.append(
                    {
                        "cluster": int(index),
                        "title": f"选题：{title.strip()[:30]}",
                        "angle": "解释事件背后的逻辑",
                        "reason": "近期热议，受众关心",
                        "visuals": [f"{color}色画面", f"{color}色特写", "紫色天空"],
                    }
                )
            return json.dumps({"cards": cards}, ensure_ascii=False)
        if "受众匹配" in system:
            ids = re.findall(r"【卡片 (\d+)】([^\n]*)", text)
            return json.dumps(
                {"scores": [{"card": int(i), "score": 0.9 if "红" in title else 0.6} for i, title in ids]},
                ensure_ascii=False,
            )
        if "视频大纲" in system:
            title = re.search(r"选题：([^\n]*)", text)
            title = title.group(1) if title else "选题"
            template = "nope-template" if "不存在" in title else "history" if "考古" in title else "hot"
            return json.dumps(
                {"outline": f"开场：{title}。\n中段：讲清事件。\n结尾：总结观点。", "template_id": template},
                ensure_ascii=False,
            )
        raise AssertionError(f"未预期的选题模型请求：{system[:80]}")


def topic_embedder(config):
    """颜色 fixture 嵌入的选题测试版：
    - 文字含"红/蓝/绿"时与 FixtureEmbedder 相同（落在素材库三种颜色的维度上）；
    - 含"紫"时落在素材库没有的第 4 维（用于制造素材缺口）；
    - 其余文字按内容哈希落在第 5–8 维（彼此不同、与素材画面正交），让不同标题的候选条目分到不同簇。"""
    import hashlib

    import numpy as np

    from conftest import FixtureEmbedder

    class TopicEmbedder(FixtureEmbedder):
        def text(self, texts, role="document"):
            vectors = super().text(texts, role)
            for text, vector in zip(texts, vectors, strict=True):
                if "紫" in text:
                    vector[:] = np.eye(8)[3].tolist()
                elif not any(color in text for color in "红蓝绿"):
                    digest = hashlib.sha256(text.encode("utf-8")).digest()
                    tail = np.array([b - 127.5 for b in digest[:4]], dtype=np.float64)
                    tail /= np.linalg.norm(tail) or 1
                    vector[:] = [0.0, 0.0, 0.0, 0.0, *tail.tolist()]
            return vectors

    return TopicEmbedder(config)


def analyzed_library(config, video_file):
    """已分析并发布 shot 索引的颜色素材库（红、蓝、绿三镜），返回选题测试用的 fixture 嵌入。"""
    from conftest import FixtureAnalyzer
    from processing.indexing import IndexBuilder
    from processing.pipeline import Pipeline

    config.studio.recall.granularity = "shot"
    embedder = topic_embedder(config)
    pipeline = Pipeline(config, analyzer=FixtureAnalyzer(embedder))
    with pipeline.lock():
        pipeline.prepare(video_file)
        pipeline.analyze()
        IndexBuilder(config, embedder).build(kinds=("shot",), activate=True)
    return embedder
