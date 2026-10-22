"""Qwen 视觉分析客户端（OpenAI 兼容 chat 协议）。"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from mindex.models import adapters
from mindex.models.adapters import (
    AnalysisContentRejected,
    InvalidAnalysisResponse,
    InvalidAnalysisStructure,
    ModelError,
    TruncatedAnalysisResponse,
    analysis_content_rejection,
    parse_json,
)
from mindex.shared.config import Config
from mindex.shared.media import frame_data
from mindex.shared.trace import record
from mindex.shared.types import Annotation

log = logging.getLogger(__name__)


def analysis_options(service, *, text_only=False):
    """Use documented provider controls; never guess cloud-specific parameters."""
    host = urlparse(service.base_url).hostname or ""
    model = service.model.lower()
    mode = getattr(service, "thinking_mode", "auto")
    options = {}
    if host == "open.bigmodel.cn":
        if mode != "provider":
            if model.startswith("glm-5.3"):
                options["reasoning_effort"] = "low"
            else:
                options["thinking"] = {"type": "disabled"}
        if text_only:
            options["response_format"] = {"type": "json_object"}
    elif host in {"api.minimax.cn", "api.minimax.io", "api.minimaxi.com", "api.minimaxi.chat"}:
        if mode != "provider":
            if model.startswith("minimax-m3.1") or mode == "low":
                options["reasoning_effort"] = "low"
            elif model == "minimax-m3":
                options["thinking"] = {"type": "disabled"}
        # Separate reasoning is an output format switch, not a way to disable thinking.
        options["reasoning_split"] = True
    elif service.llama_template:
        options["chat_template_kwargs"] = {"enable_thinking": mode == "provider"}
        options["response_format"] = {"type": "json_object"}
    elif host == "api.deepseek.com" and mode != "provider":
        options["thinking"] = {"type": "disabled"}
        options["response_format"] = {"type": "json_object"}
    elif host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} and mode != "provider":
        options["enable_thinking"] = False
    return options


class QwenAnalyzer:
    def __init__(self, config: Config):
        self.config = config
        self.profile = config.analyzer

    def _chat(self, content: list[dict], instructions: str, *, validate: Callable | None = None):
        attempts = self.profile.response_retries + 1
        budget = min(self.profile.initial_output_tokens, self.profile.max_output_tokens)
        retry_instructions = instructions
        for attempt in range(attempts):
            try:
                return self._chat_once(content, retry_instructions, budget, validate=validate)
            except InvalidAnalysisResponse as exc:
                if attempt == attempts - 1:
                    raise ModelError(
                        f"分析响应连续 {attempts} 次无效，自动重试已耗尽：{exc}；"
                        "已完成片段保留，可重试继续检查点"
                    ) from exc
                if isinstance(exc, InvalidAnalysisStructure):
                    retry_instructions = (
                        instructions + "\n上次响应未通过结构校验：" + str(exc)
                        + "。请根据原始输入重新生成符合上述字段类型和约束的完整 JSON，"
                        "逐项检查类型；不要解释错误或输出 JSON 之外的任何文字。"
                    )
                budget = min(budget * 2, self.profile.max_output_tokens)
                delay = min(2 ** attempt, 8)
                log.warning(
                    "分析模型 %s 响应无效（%d/%d）：%s；%s 秒后重试，max_tokens=%d",
                    self.profile.model, attempt + 1, attempts, exc, delay, budget,
                )
                record("analysis-retry", {
                    "model": self.profile.model, "attempt": attempt + 1,
                    "attempts": attempts, "delay_seconds": delay, "error": str(exc), "max_tokens": budget,
                })
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _chat_once(
        self, content: list[dict], instructions: str, max_tokens: int, *, validate: Callable | None = None,
    ):
        try:
            return self._chat_response(content, instructions, max_tokens, validate=validate)
        except ModelError as exc:
            # HTTP 200 can itself contain truncated/non-JSON content. Network
            # retries and permanent HTTP errors keep their existing behavior.
            if not isinstance(exc, InvalidAnalysisResponse):
                if isinstance(exc.__cause__, httpx.HTTPStatusError):
                    reason = analysis_content_rejection(exc.__cause__.response)
                    if reason:
                        raise AnalysisContentRejected(reason) from exc
                if isinstance(exc.__cause__, json.JSONDecodeError):
                    raise InvalidAnalysisResponse("分析服务响应不是有效 JSON") from exc
                if isinstance(exc.__cause__, httpx.HTTPStatusError) and exc.__cause__.response.status_code == 500:
                    raise InvalidAnalysisResponse(str(exc)) from exc
            raise

    def _chat_response(
        self, content: list[dict], instructions: str, max_tokens: int, *, validate: Callable | None = None,
    ):
        started = perf_counter()
        text_only = all(item.get("type") == "text" for item in content)
        payload = adapters.request(
            "POST",
            self.profile.base_url.rstrip("/") + "/chat/completions",
            timeout=self.profile.timeout,
            headers=self.profile.headers(),
            json={
                "model": self.profile.model,
                "temperature": 0,
                "max_tokens": max_tokens,
                **analysis_options(self.profile, text_only=text_only),
                "messages": [
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": content},
                ],
            },
        )
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        log.info(
            "LLM %s %.2f 秒 · 输入 %s token · 输出 %s token · 思考 %s token · 模式 %s",
            self.profile.model, perf_counter() - started,
            usage.get("prompt_tokens", "未知"), usage.get("completion_tokens", "未知"),
            details.get("reasoning_tokens", "未知"), self.profile.thinking_mode,
        )
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            text = message.get("content")
            if choice.get("finish_reason") in {"content_filter", "sensitive"} or message.get("refusal"):
                raise AnalysisContentRejected("模型内容安全策略拒绝此片段")
            if choice.get("finish_reason") == "length":
                raise TruncatedAnalysisResponse(
                    "分析服务输出预算耗尽（finish_reason=length，"
                    f"max_tokens={max_tokens}，正文 {len(text) if isinstance(text, str) else 0} 字，"
                    f"推理 {len(message.get('reasoning_content') or '')} 字）；请提高输出 token 上限"
                )
            if not isinstance(text, str) or not text.strip():
                raise InvalidAnalysisResponse(
                    f"分析服务未返回正文（finish_reason={choice.get('finish_reason')}）"
                )
            result = parse_json(text)
            return validate(result) if validate is not None else result
        except (InvalidAnalysisResponse, KeyError, TypeError, IndexError, AttributeError) as exc:
            log.warning(
                "分析模型 %s 返回无效响应（max_tokens=%d）：\n%s\n—— 模型返回结束 ——",
                self.profile.model, max_tokens, json.dumps(payload, ensure_ascii=False, indent=2),
            )
            if isinstance(exc, InvalidAnalysisResponse):
                raise
            raise InvalidAnalysisResponse("分析服务返回格式不符合聊天接口约定") from exc

    @staticmethod
    def _validate_annotation(result) -> Annotation:
        try:
            return Annotation.model_validate(result)
        except ValidationError as exc:
            # Feed back field paths and expected types, not untrusted model text.
            errors = "; ".join(
                f"{'.'.join(str(part) for part in error['loc']) or '根对象'}: {error['msg']}"
                for error in exc.errors(include_url=False, include_input=False)
            )
            raise InvalidAnalysisStructure(f"分析字段校验失败：{errors}") from exc

    def analyze(self, path: Path, dialogue: list[dict]) -> Annotation:
        content = [
            {
                "type": "text",
                "text": "以下是按时间排列的视频帧及对白。对白仅供上下文参考：\n"
                + json.dumps(dialogue, ensure_ascii=False),
            }
        ]
        for timestamp, uri in frame_data(path, self.config.video.analysis_frames):
            content += [
                {"type": "text", "text": f"片段内 {timestamp:.3f} 秒"},
                {"type": "image_url", "image_url": {"url": uri}},
            ]
        instructions = (
            "为影视片段返回简洁、客观、可检索的中文描述。"
            "输出只能是一个合法 JSON 对象，从 { 开始到 } 结束。"
            "禁止 Markdown、代码块、前言、总结、分析过程、字段说明及 JSON 之外的任何文字。"
            "仅使用以下字段，description 必填，其余仅有明确依据时输出，未知或空列表省略："
            "description（字符串，1到2句，尽量不超过120字，概括主体、关键动作和环境）；"
            "actions（动作顺序）、people（可见人物外观）、objects（主要物品）、scene（地点与环境）、"
            "emotions（可观察情绪）、atmosphere（氛围）、shot_size（景别）、angle（角度）、"
            "camera_motion（镜头运动）、visual_evidence（关键画面依据）、"
            "dialogue_inference（仅对白支持的推断）、uncertainty（影响理解的重要不确定项）。"
            "除 description 外字段都是字符串数组，每项用短语，通常每字段最多3项；"
            "这些字段即使只有一项也必须写成数组，不能写成字符串、对象或 null；"
            '例如 scene 正确为 ["夜间城市滨水区"]，不能写成 "夜间城市滨水区"；'
            'shot_size 正确为 ["远景"]，不能写成 "远景"。'
            '格式示例：{"description":"城市滨水区的灯光倒映在水面。",'
            '"scene":["夜间城市滨水区","水面倒映灯光"],"shot_size":["远景"]}。'
            "示例仅说明格式，内容须以当前画面为准。输出前检查 description 是非空字符串，"
            "其余已输出字段均为数组且每个元素都是字符串；未知字段省略，不用 null 或空字符串占位。"
            "visual_evidence、dialogue_inference、uncertainty 各最多2项，不重复复述描述。"
            "只保留有区分度且有依据的信息，不列无关细节，不逐帧复述。"
            "情绪须有表情或行为依据；人物仅描述外观，不识别真实身份。"
            "禁止虚构声音、姓名、身份或画面外事件。"
        )
        return self._chat(content, instructions, validate=self._validate_annotation)

    def group_scenes(self, units: list[dict]) -> list[list[int]]:
        if not units:
            return []
        compact = [
            {"index": index, "start": round(unit["start"], 3), "end": round(unit["end"], 3),
             **({"description": unit["description"][:160]} if "description" in unit else {}),
             **({"scene": unit["scene"][:3]} if unit.get("scene") else {})}
            for index, unit in enumerate(units)
        ]
        content = [{"type": "text", "text": json.dumps(compact, ensure_ascii=False, separators=(",", ":"))}]
        example = {"scene_starts": [0, 2] if len(units) > 2 else [0]}
        return self._chat(
            content,
            (
                "将连续影视片段按场景、对白和事件组织成连续场景。"
                "只选择场景边界，由程序展开为完整分组。"
                '输出只能是一个 JSON 对象，唯一字段为 scene_starts，表示每个场景的起始片段 index。'
                f"本批共有 {len(units)} 个片段，index 为 0 到 {len(units) - 1}；"
                f"格式示例：{json.dumps(example, separators=(',', ':'))}。示例仅说明格式。"
                "从 { 开始到 } 结束，禁止代码块、分组说明、理由、标题、总结、"
                "逐片段复述、方案比较及 JSON 之外的任何文字，不增加其他字段。"
                "scene_starts 必须是非空整数数组，首项为0，其余为新场景起始片段的 index，"
                "严格递增、无重复，必须在本批索引范围内。无需逐项列出场景内的片段。"
                "每个场景从起始 index 延续到下一个起始 index 的前一项；"
                f"最后一个场景自动延续到片段 {len(units) - 1}。索引不是时间戳。"
                "目标10到30秒是偏好而非最低时长；场景改变、短转场或结尾允许不足10秒，"
                "不要为凑时长合并无关场景。每组最长45秒。"
                "输入时间戳为原视频秒数；组时长=最后end-最先start。仅返回最终场景起始索引。"
            ),
            validate=lambda result: self._validate_scene_groups(result, units),
        )

    @staticmethod
    def _validate_scene_groups(result, units: list[dict]) -> list[list[int]]:
        # Ask the model only for boundaries. Expanding them locally guarantees
        # coverage, even when it does not enumerate the last few input clips.
        if isinstance(result, dict) and "scene_starts" in result:
            starts = result["scene_starts"]
            if set(result) != {"scene_starts"}:
                raise InvalidAnalysisStructure("场景边界响应只能包含 scene_starts 字段")
            if not isinstance(starts, list) or not starts or any(type(i) is not int for i in starts):
                raise InvalidAnalysisStructure("scene_starts 必须是非空整数数组，索引必须为整数")
            if starts[0] != 0:
                raise InvalidAnalysisStructure("scene_starts 首项必须为0，以覆盖开头片段")
            if any(i < 0 or i >= len(units) for i in starts):
                raise InvalidAnalysisStructure(f"scene_starts 索引必须在0到{len(units) - 1}之间")
            if any(left >= right for left, right in pairwise(starts)):
                raise InvalidAnalysisStructure("scene_starts 必须严格递增且无重复")
            groups = [list(range(start, end)) for start, end in pairwise([*starts, len(units)])]
        else:
            # Keep accepting the earlier wire format, but never guess how to
            # repair missing/reordered indices and alter a semantic boundary.
            groups = result.get("groups") if isinstance(result, dict) else None
        if not isinstance(groups, list) or any(not isinstance(g, list) or not g for g in groups):
            raise InvalidAnalysisStructure("无效场景分组：groups 必须是非空整数数组的数组")
        flat = [i for group in groups for i in group]
        if any(type(i) is not int for i in flat):
            raise InvalidAnalysisStructure("场景分组索引必须为整数")
        if flat != list(range(len(units))):
            seen = set(flat)
            missing = [i for i in range(len(units)) if i not in seen]
            duplicates = sorted(i for i in seen if flat.count(i) > 1)
            outside = sorted(i for i in seen if i < 0 or i >= len(units))
            mismatch = next(
                (index for index, actual in enumerate(flat) if actual != index),
                min(len(flat), len(units)),
            )
            raise InvalidAnalysisStructure(
                "场景分组必须连续、完整且无重复；"
                f"期望按顺序覆盖0到{len(units) - 1}，缺失索引={missing}，"
                f"重复索引={duplicates}，越界索引={outside}，首个不匹配位置={mismatch}；"
                "请改用 scene_starts 返回场景起始索引"
            )
        if any(units[g[-1]]["end"] - units[g[0]]["start"] > 45.001 for g in groups):
            raise InvalidAnalysisStructure("场景分组超过45秒")
        return groups
