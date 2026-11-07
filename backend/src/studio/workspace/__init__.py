"""版本化创作工作台。方案是可变指针；版本与构建记录是不可变历史。

所有修改由 studio_api 在全局 Studio 锁内完成。旧任务文件只在迁移时读取，并继续经产物接口可访问。

本包按职责分模块（见 docs/studio.md），这里显式导出公共入口，调用方统一 ``from studio import
workspace as ws`` 后使用 ``ws.xxx``。测试替换函数时按"在哪里查找就在哪里替换"：外部调用方（如
studio_api 调用 ``ws.run_build``、``ws.load``）替换本包属性；包内调用替换定义模块的属性，例如
``builder.execute``、``signatures.match``、``signatures.specification``；阶段专属的常量替换处理器模块，
例如 ``handlers.script.WRITER_PROMPT_VERSION``。

阶段注册表见 ``stages``：``REGISTRY`` 登记 ``handlers/`` 下的处理器，``shot_stages`` 按镜头来源给出
逐镜阶段序列；``sources`` 定位并复验 clip/image 来源文件。
"""

# 阶段注册与公共导出的装配顺序有语义，保持原顺序。
# isort: off
from studio.workspace import (
    handlers,  # noqa: F401 — 导入即登记全部阶段处理器
    music,
    post,
    publish,
    references,
    sources,
    stages,
    transitions,
    variants,
)
from studio.workspace.builder import (
    CANCELLED,
    VersionDiagnostics,
    cancel_remaining,
    execute,
    recover,
    run_build,
)
from studio.workspace.details import compact_version, detail, recall_closure, retrieval_dependencies
from studio.workspace.handlers.animate import save_manual as save_animation
from studio.workspace.handlers.overlay import save_manual as save_overlay
from studio.workspace.handlers.publish import save_metadata
from studio.workspace.media_nodes import alignment, clip_alignment, render_node, source_shots, validate_range
from studio.workspace.persistence import load, normalize_script
from studio.workspace.plans import (
    BOARD_FIELDS,
    PlanOptions,
    ReferenceRef,
    add_script,
    board_snapshot,
    canvas_of,
    canvas_render,
    choose,
    effective_voice,
    empty_board,
    needs_recall,
    new_plan,
    normalize_voice_options,
    remove_plan,
    restore,
    restore_script_board,
    script_stale,
    script_text,
    set_board,
)
from studio.workspace.post import compose
from studio.workspace.preflight import column_nodes, node_dependencies, preflight
from studio.workspace.signatures import (
    DetailContext,
    conflicts,
    match,
    query_media,
    recall_signature,
    settings,
    signature_matches,
    specification,
)
from studio.workspace.stages import REGISTRY, SHOT_SOURCES, RunContext, StageHandler, register, shot_stages
from studio.workspace.versions import (
    SCRIPT_ORIGINS,
    STAGES,
    RevisionConflict,
    add_version,
    current_revision,
    file_info,
    file_path,
    ordered_shots,
    path,
    persist,
    plan,
    require_revision,
    require_workspace_revision,
    revision,
    root,
    valid_files,
    version,
)
# isort: on

__all__ = [
    "BOARD_FIELDS",
    "CANCELLED",
    "REGISTRY",
    "SCRIPT_ORIGINS",
    "SHOT_SOURCES",
    "STAGES",
    "DetailContext",
    "PlanOptions",
    "ReferenceRef",
    "RevisionConflict",
    "RunContext",
    "StageHandler",
    "VersionDiagnostics",
    "add_script",
    "add_version",
    "alignment",
    "board_snapshot",
    "cancel_remaining",
    "canvas_of",
    "canvas_render",
    "choose",
    "clip_alignment",
    "column_nodes",
    "compact_version",
    "compose",
    "conflicts",
    "current_revision",
    "detail",
    "effective_voice",
    "empty_board",
    "execute",
    "file_info",
    "file_path",
    "load",
    "match",
    "music",
    "needs_recall",
    "new_plan",
    "node_dependencies",
    "normalize_script",
    "normalize_voice_options",
    "ordered_shots",
    "path",
    "persist",
    "plan",
    "post",
    "preflight",
    "publish",
    "query_media",
    "recall_closure",
    "recall_signature",
    "recover",
    "references",
    "register",
    "remove_plan",
    "render_node",
    "require_revision",
    "require_workspace_revision",
    "restore",
    "restore_script_board",
    "retrieval_dependencies",
    "revision",
    "root",
    "run_build",
    "save_animation",
    "save_metadata",
    "save_overlay",
    "script_stale",
    "script_text",
    "set_board",
    "settings",
    "shot_stages",
    "signature_matches",
    "source_shots",
    "sources",
    "specification",
    "stages",
    "transitions",
    "valid_files",
    "validate_range",
    "variants",
    "version",
]
