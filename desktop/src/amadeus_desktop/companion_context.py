"""Authorization-aware capability snapshots for companion requests."""

from __future__ import annotations

from collections.abc import Iterable

from amadeus_desktop.chat_models import (
    AttachmentKind,
    AttachmentSnapshot,
    AttachmentSource,
    CompanionContextSnapshot,
    CompanionRequestKind,
    InputModality,
)


def build_companion_context_snapshot(
    *,
    input_modality: InputModality = InputModality.TEXT,
    attachments: Iterable[AttachmentSnapshot] = (),
    request_kind: CompanionRequestKind = CompanionRequestKind.CONVERSATION,
) -> CompanionContextSnapshot:
    """Describe only bytes and transcript data included in this request."""

    modality = InputModality(input_modality)
    supplied = tuple(attachments)
    visual_sources = tuple(
        attachment.source for attachment in supplied if attachment.kind is AttachmentKind.IMAGE
    )
    return CompanionContextSnapshot(
        request_kind=CompanionRequestKind(request_kind),
        input_modality=modality,
        visual_sources=visual_sources,
        has_document_attachment=any(
            attachment.kind is AttachmentKind.DOCUMENT for attachment in supplied
        ),
    )


def build_proactive_visual_context(source: AttachmentSource) -> CompanionContextSnapshot:
    """Describe one volatile frame sampled for one proactive request."""

    return CompanionContextSnapshot(
        request_kind=CompanionRequestKind.PROACTIVE,
        input_modality=InputModality.TEXT,
        visual_sources=(AttachmentSource(source),),
    )


def build_proactive_text_context() -> CompanionContextSnapshot:
    """Describe a proactive request that contains no user media or transcript."""

    return CompanionContextSnapshot(request_kind=CompanionRequestKind.PROACTIVE)
