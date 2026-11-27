"""Script / Scene：前端播放器的数据契约。

## 为什么按 Scene 组织，而不是一大段旁白

如果只返回一段文字 + 一个音频，前端无法知道「讲到第几句该换图」。
按 Scene 切分后，每一段自带：讲什么（narration）、显示什么（subtitle + visual）、
依据哪条 Claim（claim_ids）。

## 时间轴由音频实测时长反推

不要预设「每段 8 秒」。语速、标点停顿都会让真实时长偏离，
硬切的结果就是字幕和声音错位。

正确流程：

    1. 生成 narration
    2. 按 Scene 分别合成音频，拿到 audio_duration_ms
    3. 用 recalc_timeline() 反推 start_ms / end_ms

## visual.type 是受约束枚举

不允许 LLM 自由生成前端组件代码，只返回这六种类型之一，
前端按类型渲染预置组件。未知类型降级为 text。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

VisualType = Literal["illustration", "text", "diagram", "chart", "formula", "comparison"]

VISUAL_LABELS = {
    "illustration": "插图",
    "text": "文字卡片",
    "diagram": "示意图",
    "chart": "数据图表",
    "formula": "公式",
    "comparison": "对比展示",
}


class Visual(BaseModel):
    """一个 Scene 的画面。"""

    type: VisualType = "text"
    description: str = ""
    asset_url: str | None = None
    # comparison 类型用左右两栏，其他类型忽略
    left: str | None = None
    right: str | None = None

    @field_validator("type", mode="before")
    @classmethod
    def unknown_type_falls_back(cls, v):
        """LLM 可能返回没见过的类型，降级为 text 而不是抛错。"""
        allowed = {"illustration", "text", "diagram", "chart", "formula", "comparison"}
        return v if v in allowed else "text"


class Scene(BaseModel):
    """播放器的一帧。"""

    scene_id: str
    start_ms: int = 0
    end_ms: int = 0

    narration: str = ""
    subtitle: str = ""

    visual: Visual = Field(default_factory=Visual)
    highlight_terms: list[str] = Field(default_factory=list)

    # 溯源：这一帧讲的内容依据哪几条
    claim_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    # TTS 分段产物
    audio_url: str | None = None
    audio_duration_ms: int | None = None

    @property
    def duration_ms(self) -> int:
        if self.end_ms > self.start_ms:
            return self.end_ms - self.start_ms
        return self.audio_duration_ms or 0

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_url)


class Script(BaseModel):
    """一份完整讲解脚本（对应一个深度）。"""

    script_id: str
    package_id: str = ""
    depth: Literal["beginner", "advanced", "professional"] = "beginner"

    title: str = ""
    scenes: list[Scene] = Field(default_factory=list)
    total_duration_ms: int = 0

    # 生成日志：用了哪个模型、多少 token，便于排查
    meta: dict = Field(default_factory=dict)

    def recalc_timeline(self) -> "Script":
        """按音频实测时长重排时间轴。

        没有音频的 Scene 用 narration 字数粗估（每秒约 4.5 字），
        保证播放器在 TTS 未就绪时也能预览。
        """
        cursor = 0
        for s in self.scenes:
            dur = s.audio_duration_ms
            if not dur:
                dur = max(2000, int(len(s.narration) / 4.5 * 1000))
            s.start_ms = cursor
            s.end_ms = cursor + dur
            cursor += dur
        self.total_duration_ms = cursor
        return self

    @property
    def scene_count(self) -> int:
        return len(self.scenes)

    @property
    def has_audio(self) -> bool:
        return any(s.has_audio for s in self.scenes)

    def scene_at(self, position_ms: int) -> Scene | None:
        """按播放位置找当前 Scene，播放器用。"""
        for s in self.scenes:
            if s.start_ms <= position_ms < s.end_ms:
                return s
        return self.scenes[-1] if self.scenes else None

    def evidence_of_scene(self, scene: Scene) -> list[str]:
        return list(scene.evidence_ids)
