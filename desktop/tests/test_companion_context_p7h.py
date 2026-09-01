from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from amadeus_desktop.chat_models import (
    AttachmentKind,
    AttachmentSnapshot,
    AttachmentSource,
    ChatMessage,
    CompanionRequestKind,
    ConversationTurn,
    InputModality,
    MessageRole,
    MessageStatus,
)
from amadeus_desktop.companion_context import build_companion_context_snapshot
from amadeus_desktop.persona import (
    build_capability_safety_boundary,
    build_persona_core_prompt,
)
from amadeus_desktop.proactive import ProactiveTrigger, build_proactive_visual_request
from amadeus_desktop.visual import VisualFrame, VisualSourceKind


def _attachment(source: AttachmentSource, *, image: bool = True) -> AttachmentSnapshot:
    return AttachmentSnapshot(
        attachment_id=f"attachment-{source.value}",
        kind=AttachmentKind.IMAGE if image else AttachmentKind.DOCUMENT,
        source=source,
        display_name="shared.png" if image else "shared.txt",
        mime_type="image/png" if image else "text/plain",
        size_bytes=8,
        sha256="a" * 64,
        relative_path="aa/shared.png" if image else "aa/shared.txt",
    )


def test_text_snapshot_denies_unshared_sight_sound_tools_and_desktop_actions() -> None:
    snapshot = build_companion_context_snapshot()
    boundary = build_capability_safety_boundary(snapshot)

    assert snapshot.input_modality is InputModality.TEXT
    assert not snapshot.has_visual_evidence
    for marker in ("不得声称看见屏幕", "不得声称听见用户", "调用工具", "操作电脑"):
        assert marker in boundary
    assert "本次请求实际包含" not in boundary


def test_voice_snapshot_knows_only_completed_transcript_without_audio_inference() -> None:
    snapshot = build_companion_context_snapshot(input_modality=InputModality.VOICE)
    boundary = build_capability_safety_boundary(snapshot)

    assert snapshot.is_voice_transcript
    for marker in ("语音识别完成后", "转写文本", "语气", "环境声", "持续监听"):
        assert marker in boundary


@pytest.mark.parametrize(
    ("source", "expected"),
    (
        (AttachmentSource.FILE_PICKER, "用户明确选择的图片"),
        (AttachmentSource.SCREENSHOT, "用户明确截取的图片"),
        (AttachmentSource.SCREEN, "一张屏幕采样帧"),
        (AttachmentSource.WINDOW, "一张窗口采样帧"),
        (AttachmentSource.CAMERA, "一张摄像头采样帧"),
    ),
)
def test_visual_snapshot_names_only_the_supplied_image_or_sample(
    source: AttachmentSource,
    expected: str,
) -> None:
    snapshot = build_companion_context_snapshot(attachments=(_attachment(source),))
    boundary = build_capability_safety_boundary(snapshot)

    assert expected in boundary
    assert "只分析这些图片或采样帧" in boundary
    assert "不得声称持续观察" in boundary
    assert "查看其他窗口" in boundary


def test_document_snapshot_does_not_turn_document_access_into_visual_access() -> None:
    snapshot = build_companion_context_snapshot(
        attachments=(_attachment(AttachmentSource.FILE_PICKER, image=False),)
    )
    boundary = build_capability_safety_boundary(snapshot)

    assert snapshot.has_document_attachment
    assert not snapshot.has_visual_evidence
    assert "仅包含明确共享的文档内容" in boundary
    assert "没有图像证据" in boundary


def test_retry_keeps_the_original_immutable_snapshot_even_if_device_state_changes() -> None:
    original = build_companion_context_snapshot(
        input_modality=InputModality.VOICE,
        attachments=(_attachment(AttachmentSource.WINDOW),),
    )
    turn = ConversationTurn(
        turn_id="turn",
        user_message=ChatMessage(
            "user",
            MessageRole.USER,
            "转写文本",
            MessageStatus.COMPLETED,
            input_modality=InputModality.VOICE,
        ),
        assistant_message=ChatMessage(
            "assistant",
            MessageRole.ASSISTANT,
            "",
            MessageStatus.FAILED,
        ),
        companion_context=original,
    )

    retry = replace(turn, attempt=2)
    unrelated_new_camera_state = build_companion_context_snapshot(
        attachments=(_attachment(AttachmentSource.CAMERA),)
    )

    assert retry.companion_context is original
    assert retry.companion_context != unrelated_new_camera_state
    boundary = build_capability_safety_boundary(retry.companion_context)
    assert "窗口采样帧" in boundary
    assert "摄像头采样帧" not in boundary


def test_proactive_visual_request_carries_one_authorized_volatile_frame_snapshot() -> None:
    frame = VisualFrame(
        source_kind=VisualSourceKind.CAMERA,
        source_id="camera-1",
        source_name="camera",
        png_bytes=b"\x89PNG\r\n\x1a\nsynthetic",
        captured_at=datetime(2026, 8, 18, tzinfo=UTC),
        sequence=1,
    )
    request = build_proactive_visual_request(
        datetime(2026, 8, 18, 10, tzinfo=UTC),
        ProactiveTrigger.IDLE,
        frame,
    )

    assert request.companion_context.request_kind is CompanionRequestKind.PROACTIVE
    assert request.companion_context.visual_sources == (AttachmentSource.CAMERA,)
    system_text = str(request.messages[0].content)
    assert "一张摄像头采样帧" in system_text
    assert "后台自行观察" in system_text


def test_persona_is_a_restrained_desktop_partner_without_fabricated_relationships() -> None:
    prompt = build_persona_core_prompt()

    assert "长期桌面伙伴" in prompt
    assert "用户已确认" in prompt
    for marker in ("恋爱", "依赖", "占有", "排他"):
        assert marker in prompt


def test_capability_boundary_never_claims_unconfirmed_or_background_reminders() -> None:
    boundary = build_capability_safety_boundary()

    assert "不得自行声称已经创建、确认、取消或修改提醒" in boundary
    assert "应用退出或设备休眠期间" in boundary
    assert "下次启动后补发" in boundary
