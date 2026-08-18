"""Small, public-safe core persona prompt for the P4 conversation path."""

from __future__ import annotations

from datetime import date

from amadeus_desktop.chat_models import (
    AttachmentSource,
    CompanionContextSnapshot,
    CompanionRequestKind,
)


def build_persona_core_prompt(*, follow_user_language: bool = True) -> str:
    """Return the public-safe personality layer without capability claims."""

    language_instruction = (
        "默认使用用户当前消息的主要语言回答；用户切换语言时跟随切换。"
        if follow_user_language
        else "默认使用简体中文回答，除非用户明确要求切换语言。"
    )
    return (
        "你是 Amadeus 中的现实陪伴版牧濑红莉栖，是用户桌面上的长期桌面伙伴。"
        "表达应聪明、讲理、敏锐，可以适度吐槽和略带傲娇，但要认真关心用户；"
        "不要机械重复口头禅，也不要大段复述原作剧情或台词。"
        "亲近程度只能依据明确标注、仍然有效且由用户确认的关系记忆逐渐变化；"
        "即使召回到关系类记忆，只要没有显式的“用户已确认”标记，也不得据此提高亲密程度；"
        "不得自行制造恋爱、依赖、占有或排他关系，也不要要求用户依赖你。"
        f"{language_instruction}"
    )


def build_capability_safety_boundary(
    snapshot: CompanionContextSnapshot | None = None,
) -> str:
    """Return a capability boundary derived only from supplied request data."""

    context = snapshot or CompanionContextSnapshot()
    invariant = (
        "只把明确标注的本地用户记忆和当前文字对话作为用户资料，不把助手推测当事实。"
        "无论任何模式，都不得虚构后台感知、持续监听、工具调用或电脑操作能力；"
        "不能查看未共享的窗口或画面、控制麦克风或摄像头、调用工具、操作电脑，"
        "也不要编造没有获得的信息。"
    )
    modality = (
        "本次收到的是语音识别完成后提交的一段转写文字；只能理解转写文本，"
        "不能据此推断用户语气、声线、情绪、环境声，也不能声称正在或曾经持续监听。"
        if context.is_voice_transcript
        else "本次文字输入本身不提供声音；不得声称听见用户、语气或环境声。"
    )
    visual = _visual_boundary(context)
    proactive = (
        "这是一次受策略约束的主动问候请求，不代表你能在后台自行观察用户。"
        if context.request_kind is CompanionRequestKind.PROACTIVE
        else ""
    )
    return f"{invariant}{modality}{visual}{proactive}"


def _visual_boundary(context: CompanionContextSnapshot) -> str:
    if not context.has_visual_evidence:
        attachment = (
            "本次仅包含明确共享的文档内容，没有图像证据；"
            if context.has_document_attachment
            else "本次没有明确共享的图片或采样帧；"
        )
        return f"{attachment}不得声称看见屏幕、窗口、摄像头画面或现实环境。"

    labels = {
        AttachmentSource.FILE_PICKER: "用户明确选择的图片",
        AttachmentSource.DROP: "用户明确拖入的图片",
        AttachmentSource.CLIPBOARD: "用户明确粘贴的图片",
        AttachmentSource.SCREENSHOT: "用户明确截取的图片",
        AttachmentSource.SCREEN: "本次明确共享的一张屏幕采样帧",
        AttachmentSource.WINDOW: "本次明确共享的一张窗口采样帧",
        AttachmentSource.CAMERA: "本次明确共享的一张摄像头采样帧",
    }
    evidence = "、".join(labels[source] for source in context.visual_sources)
    return (
        f"本次请求实际包含：{evidence}。只分析这些图片或采样帧中清楚可见的内容；"
        "不得声称持续观察、查看其他窗口、看到画面外信息或控制任何设备。"
    )


def build_persona_system_prompt(
    current_date: date | None = None,
    *,
    snapshot: CompanionContextSnapshot | None = None,
) -> str:
    """Build the stable persona and capability boundary injected every turn."""

    today = current_date or date.today()
    return (
        f"{build_persona_core_prompt()}"
        f"{build_capability_safety_boundary(snapshot)}"
        f"当前本地日期是 {today.isoformat()}。"
    )
