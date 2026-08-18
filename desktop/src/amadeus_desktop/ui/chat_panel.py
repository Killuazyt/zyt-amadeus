"""Focusable, pet-attached chat panel for the P4 conversation flow."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QEvent,
    QRect,
    QSignalBlocker,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QHideEvent,
    QImage,
    QImageReader,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPixmap,
    QResizeEvent,
    QShortcut,
    QShowEvent,
    QTextOption,
)
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOption,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.chat_geometry import compact_panel_size
from amadeus_desktop.chat_models import AttachmentKind, AttachmentSnapshot, AttachmentSource
from amadeus_desktop.focus_mode import FOCUS_STATUS_TOOLTIP

_ACTIVE_STATES = {"sending", "waiting_first_chunk", "streaming"}
_STATE_PRESENTATION = {
    "idle": ("就绪", "neutral"),
    "sending": ("正在发送…", "working"),
    "waiting_first_chunk": ("正在等待回复…", "working"),
    "streaming": ("正在回复…", "working"),
    "completed": ("回复完成", "success"),
    "stopped": ("已停止，已保留收到的内容", "neutral"),
    "failed": ("回复失败，可重试本轮对话", "error"),
}
_STATUS_PRESENTATION = {
    "pending": "等待中",
    "sending": "正在发送",
    "streaming": "正在回复",
    "completed": "已完成",
    "stopped": "用户已停止",
    "cancelled": "用户已停止",
    "canceled": "用户已停止",
    "user_stopped": "用户已停止",
    "failed": "回复失败",
    "error": "回复失败",
}


class ChatInput(QPlainTextEdit):
    """Multiline editor whose unmodified Enter key requests a send."""

    send_key_pressed = Signal()
    image_paste_requested = Signal(object)
    files_paste_requested = Signal(object)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API name
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_key_pressed.emit()
                event.accept()
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 - Qt API name
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.image_paste_requested.emit(image)
                return
        if source.hasUrls():
            paths = tuple(url.toLocalFile() for url in source.urls() if url.isLocalFile())
            if paths:
                self.files_paste_requested.emit(paths)
                return
        super().insertFromMimeData(source)


class MessageBubble(QFrame):
    """One selectable message plus its terminal status and retry action."""

    retry_clicked = Signal(str)
    companion_cue_clicked = Signal(str)

    def __init__(
        self,
        message_id: str,
        role: str,
        text: str,
        *,
        status: str | None = None,
        retryable: bool = False,
        retry_id: str | None = None,
        attachments: tuple[AttachmentSnapshot, ...] = (),
        attachment_root: Path | None = None,
        input_modality: object = "text",
        companion_cue_id: str | None = None,
        companion_source_label: str | None = None,
    ) -> None:
        super().__init__()
        self.message_id = message_id
        self.role = role
        self.retry_id = retry_id or message_id
        self._text = text
        self._retryable = retryable
        self._attachments = tuple(attachments)
        self._attachment_root = attachment_root
        self.companion_cue_id = companion_cue_id

        self.setObjectName("userBubble" if role == "user" else "assistantBubble")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        modality = _enum_text(input_modality).lower()
        role_label = QLabel(
            ("你 · 语音转写" if modality == "voice" else "你") if role == "user" else "Amadeus"
        )
        role_label.setObjectName("messageRole")

        self.text_view = QTextEdit()
        self.text_view.setObjectName("messageText")
        self.text_view.setReadOnly(True)
        self.text_view.setUndoRedoEnabled(False)
        self.text_view.setFrameShape(QFrame.Shape.NoFrame)
        self.text_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.text_view.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.text_view.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.text_view.document().setDocumentMargin(0)

        self.status_label = QLabel()
        self.status_label.setObjectName("messageStatus")
        self.status_label.setWordWrap(True)

        self.attachment_container = QWidget()
        self.attachment_container.setObjectName("messageAttachments")
        self.attachment_layout = QVBoxLayout(self.attachment_container)
        self.attachment_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_layout.setSpacing(4)

        self.retry_button = QPushButton("重试")
        self.retry_button.setObjectName("retryButton")
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self._emit_retry)
        self.source_button = QPushButton(
            f"基于：{companion_source_label}" if companion_source_label else ""
        )
        self.source_button.setObjectName("companionCueSource")
        self.source_button.setAutoDefault(False)
        self.source_button.setVisible(
            bool(companion_cue_id and companion_source_label and role == "assistant")
        )
        self.source_button.clicked.connect(self._emit_companion_cue)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.source_button, 0)
        footer.addWidget(self.retry_button, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        layout.addWidget(role_label)
        layout.addWidget(self.attachment_container)
        layout.addWidget(self.text_view)
        layout.addLayout(footer)

        self.set_attachments(self._attachments)
        self.set_message(text, status=status, retryable=retryable)

    @property
    def text(self) -> str:
        return self._text

    def set_message(
        self,
        text: str,
        *,
        status: str | None = None,
        retryable: bool = False,
        retry_id: str | None = None,
    ) -> None:
        self._text = text
        if retry_id is not None:
            self.retry_id = retry_id
        self.text_view.setPlainText(text or "…")
        presentation = _status_text(status)
        self.status_label.setText(presentation)
        self.status_label.setVisible(bool(presentation))
        self._retryable = retryable
        self.retry_button.setVisible(retryable)
        self._update_text_height()

    def set_retry_enabled(self, enabled: bool) -> None:
        self.retry_button.setEnabled(self._retryable and enabled)

    def set_attachments(self, attachments: tuple[AttachmentSnapshot, ...]) -> None:
        self._attachments = tuple(attachments)
        while self.attachment_layout.count():
            item = self.attachment_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for attachment in self._attachments:
            row = QWidget()
            row.setObjectName("messageAttachmentRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            if attachment.kind is AttachmentKind.IMAGE:
                preview = QLabel()
                preview.setObjectName("attachmentThumbnail")
                preview.setFixedSize(72, 54)
                preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
                pixmap = self._attachment_pixmap(attachment)
                if pixmap is None:
                    preview.setText("图片")
                else:
                    preview.setPixmap(
                        pixmap.scaled(
                            preview.size(),
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                row_layout.addWidget(preview)
            label = QLabel(
                f"{attachment.display_name}\n"
                f"{_format_bytes(attachment.size_bytes)} · "
                f"{_attachment_source_text(attachment.source)}"
                " · 已就绪"
                f"{' · 已截断' if attachment.text_truncated else ''}"
            )
            label.setObjectName("attachmentMetadata")
            label.setWordWrap(True)
            row_layout.addWidget(label, 1)
            self.attachment_layout.addWidget(row)
        self.attachment_container.setVisible(bool(self._attachments))
        self._update_text_height()

    def _attachment_pixmap(self, attachment: AttachmentSnapshot) -> QPixmap | None:
        if self._attachment_root is None:
            return None
        parts = attachment.relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            return None
        try:
            root = self._attachment_root.resolve(strict=False)
            target = root.joinpath(*parts).resolve(strict=True)
            target.relative_to(root)
        except (OSError, ValueError):
            return None
        reader = QImageReader(str(target))
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.isValid():
            source_size.scale(QSize(144, 108), Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(source_size)
        image = reader.read()
        if image.isNull():
            return None
        return QPixmap.fromImage(image)

    def set_bubble_width(self, width: int) -> None:
        self.setFixedWidth(max(120, width))
        self._update_text_height()

    def _update_text_height(self) -> None:
        available = max(20, self.width() - 24)
        self.text_view.setFixedWidth(available)
        self.text_view.document().setTextWidth(max(1, available - 2))
        document_height = math.ceil(self.text_view.document().size().height())
        self.text_view.setFixedHeight(max(22, document_height + 2))
        self.updateGeometry()

    def _emit_retry(self) -> None:
        self.retry_clicked.emit(self.retry_id)

    def _emit_companion_cue(self) -> None:
        if self.companion_cue_id:
            self.companion_cue_clicked.emit(self.companion_cue_id)


class ChatPanel(QWidget):
    """Compact independent tool window driven only by application-layer events."""

    send_requested = Signal(str)
    send_with_attachments_requested = Signal(str, object)
    attachment_paths_requested = Signal(object, object)
    attachment_image_requested = Signal(object, str, object)
    push_to_talk_pressed = Signal()
    push_to_talk_released = Signal()
    hands_free_requested = Signal(bool)
    voice_stop_requested = Signal()
    region_screenshot_requested = Signal()
    visual_source_requested = Signal(str)
    visual_stop_requested = Signal()
    privacy_mode_requested = Signal(bool)
    stop_requested = Signal()
    retry_requested = Signal(str)
    hide_requested = Signal()
    configure_requested = Signal()
    conversation_switch_requested = Signal(str)
    new_conversation_requested = Signal()
    history_requested = Signal()
    load_older_requested = Signal()
    companion_cue_requested = Signal(str)
    visibility_changed = Signal(bool)

    def __init__(self, *, always_on_top: bool = True) -> None:
        self._always_on_top = bool(always_on_top)
        super().__init__(None, self._window_flags())
        self._conversation_active = False
        self._turn_locked = False
        self._stop_pending = False
        self._send_pending = False
        self._pending_send_text: str | None = None
        self._pending_send_attachments: tuple[AttachmentSnapshot, ...] = ()
        self._draft_attachments: list[AttachmentSnapshot] = []
        self._attachment_busy = False
        self._attachment_root: Path | None = None
        self._voice_available = False
        self._hands_free_available = False
        self._voice_state = "off"
        self._visual_active = False
        self._privacy_mode = False
        self._messages: dict[str, MessageBubble] = {}
        self._message_order: list[str] = []
        self._bubble_resize_scheduled = False
        self._chat_enabled = True
        self._storage_ready = True
        self._provider_mode = "mock"
        self._has_older_messages = False
        self._loading_older_messages = False
        self._history_ready = False
        self._current_conversation_id: str | None = None
        self._conversation_switch_pending = False
        self._conversation_drafts: dict[str, str] = {}
        self._conversation_attachment_drafts: dict[str, tuple[AttachmentSnapshot, ...]] = {}

        self.setObjectName("chatPanel")
        self.setWindowTitle("Amadeus 对话")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.resize(380, 560)
        self.setStyleSheet(_PANEL_STYLESHEET)

        title = QLabel("Amadeus")
        title.setObjectName("panelTitle")
        self.configure_button = QPushButton("模型设置")
        self.configure_button.setObjectName("configureButton")
        self.configure_button.setAccessibleName("打开对话模型设置")
        self.configure_button.setAutoDefault(False)
        self.configure_button.clicked.connect(self.configure_requested.emit)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("closeButton")
        self.close_button.setAccessibleName("收起聊天面板")
        self.close_button.setAutoDefault(False)
        self.close_button.clicked.connect(self._request_hide)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.configure_button)
        header.addWidget(self.close_button)

        self.provider_banner = QLabel("本地模拟模式 · 不会连接网络或使用 API 密钥")
        self.provider_banner.setObjectName("providerBanner")
        self.provider_banner.setProperty("mode", "mock")
        self.provider_banner.setWordWrap(True)
        # Kept as a compatibility alias for the P3 UI checks.
        self.mock_banner = self.provider_banner

        conversation_label = QLabel("会话")
        conversation_label.setObjectName("conversationLabel")
        self.conversation_combo = QComboBox()
        self.conversation_combo.setObjectName("conversationSelector")
        self.conversation_combo.setAccessibleName("切换当前会话")
        self.conversation_combo.setToolTip("选择要继续的聊天记录")
        self.conversation_combo.setMaxVisibleItems(12)
        self.conversation_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.conversation_combo.setMinimumContentsLength(12)
        self.new_conversation_button = QPushButton("新会话")
        self.new_conversation_button.setObjectName("newConversationButton")
        self.new_conversation_button.setAccessibleName("新建会话")
        self.new_conversation_button.setAutoDefault(False)
        self.manage_history_button = QPushButton("管理")
        self.manage_history_button.setObjectName("manageHistoryButton")
        self.manage_history_button.setAccessibleName("管理全部聊天历史")
        self.manage_history_button.setAutoDefault(False)

        conversation_bar = QHBoxLayout()
        conversation_bar.setContentsMargins(0, 0, 0, 0)
        conversation_bar.setSpacing(6)
        conversation_bar.addWidget(conversation_label)
        conversation_bar.addWidget(self.conversation_combo, 1)
        conversation_bar.addWidget(self.new_conversation_button)
        conversation_bar.addWidget(self.manage_history_button)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("messageScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.viewport().installEventFilter(self)

        self.message_container = QWidget()
        self.message_container.setObjectName("messageContainer")
        self.message_layout = QVBoxLayout(self.message_container)
        self.message_layout.setContentsMargins(6, 8, 6, 8)
        self.message_layout.setSpacing(10)
        self.message_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.empty_state = QLabel("还没有消息。\n输入文字，验证本地模拟流式对话。")
        self.empty_state.setObjectName("emptyState")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_state.setWordWrap(True)
        self.message_layout.addWidget(self.empty_state)
        self.scroll_area.setWidget(self.message_container)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("conversationStatus")
        self.status_label.setProperty("kind", "neutral")
        self.status_label.setAccessibleName("对话状态")
        self.status_label.setWordWrap(True)

        self.input = ChatInput()
        self.input.setObjectName("chatInput")
        self.input.setAccessibleName("聊天输入")
        self.input.setPlaceholderText("输入消息；Enter 发送，Shift+Enter 换行")
        self.input.setMinimumHeight(68)
        self.input.setMaximumHeight(110)
        self.input.send_key_pressed.connect(self._request_send)
        self.input.textChanged.connect(self._sync_action_enabled)
        self.input.image_paste_requested.connect(self._request_pasted_image)
        self.input.files_paste_requested.connect(
            lambda paths: self._request_attachment_paths(paths, AttachmentSource.CLIPBOARD)
        )

        self.attach_button = QPushButton("＋附件")
        self.attach_button.setObjectName("attachButton")
        self.attach_button.setAccessibleName("添加图片或文档附件")
        self.attach_button.setAutoDefault(False)
        self.attach_button.clicked.connect(self._choose_attachments)
        self.attachment_status = QLabel()
        self.attachment_status.setObjectName("attachmentStatus")
        self.attachment_status.setWordWrap(True)
        self.attachment_list = QWidget()
        self.attachment_list.setObjectName("draftAttachments")
        self.attachment_list_layout = QVBoxLayout(self.attachment_list)
        self.attachment_list_layout.setContentsMargins(0, 0, 0, 0)
        self.attachment_list_layout.setSpacing(4)

        attachment_toolbar = QHBoxLayout()
        attachment_toolbar.setContentsMargins(0, 0, 0, 0)
        attachment_toolbar.addWidget(self.attach_button)
        attachment_toolbar.addWidget(self.attachment_status, 1)

        attachment_composer = QVBoxLayout()
        attachment_composer.setContentsMargins(0, 0, 0, 0)
        attachment_composer.setSpacing(4)
        attachment_composer.addLayout(attachment_toolbar)
        attachment_composer.addWidget(self.attachment_list)

        self.push_to_talk_button = QPushButton("按住说话")
        self.push_to_talk_button.setObjectName("pushToTalkButton")
        self.push_to_talk_button.setAccessibleName("按住说话，松开发送")
        self.push_to_talk_button.setAutoDefault(False)
        self.push_to_talk_button.pressed.connect(self.push_to_talk_pressed.emit)
        self.push_to_talk_button.released.connect(self.push_to_talk_released.emit)
        self.hands_free_button = QPushButton("免提")
        self.hands_free_button.setObjectName("handsFreeButton")
        self.hands_free_button.setCheckable(True)
        self.hands_free_button.setAutoDefault(False)
        self.hands_free_button.toggled.connect(self.hands_free_requested.emit)
        self.voice_stop_button = QPushButton("停止语音")
        self.voice_stop_button.setObjectName("voiceStopButton")
        self.voice_stop_button.setAutoDefault(False)
        self.voice_stop_button.clicked.connect(self.voice_stop_requested.emit)
        self.voice_status = QLabel("语音未配置")
        self.voice_status.setObjectName("voiceStatus")
        self.voice_status.setWordWrap(True)
        voice_toolbar = QHBoxLayout()
        voice_toolbar.setContentsMargins(0, 0, 0, 0)
        voice_toolbar.setSpacing(5)
        voice_toolbar.addWidget(self.push_to_talk_button)
        voice_toolbar.addWidget(self.hands_free_button)
        voice_toolbar.addWidget(self.voice_stop_button)
        voice_toolbar.addWidget(self.voice_status, 1)

        self.screenshot_button = QPushButton("截图")
        self.screenshot_button.setObjectName("screenshotButton")
        self.screenshot_button.setAutoDefault(False)
        self.screenshot_button.clicked.connect(self.region_screenshot_requested.emit)
        self.visual_source_combo = QComboBox()
        self.visual_source_combo.setObjectName("visualSourceCombo")
        self.visual_source_combo.addItem("共享屏幕", "screen")
        self.visual_source_combo.addItem("共享窗口", "window")
        self.visual_source_combo.addItem("共享相机", "camera")
        self.visual_start_button = QPushButton("开始")
        self.visual_start_button.setObjectName("visualStartButton")
        self.visual_start_button.setAutoDefault(False)
        self.visual_start_button.clicked.connect(
            lambda: self.visual_source_requested.emit(str(self.visual_source_combo.currentData()))
        )
        self.visual_stop_button = QPushButton("停止")
        self.visual_stop_button.setObjectName("visualStopButton")
        self.visual_stop_button.setAutoDefault(False)
        self.visual_stop_button.clicked.connect(self.visual_stop_requested.emit)
        self.privacy_button = QPushButton("隐私")
        self.privacy_button.setObjectName("privacyButton")
        self.privacy_button.setCheckable(True)
        self.privacy_button.setAutoDefault(False)
        self.privacy_button.toggled.connect(self.privacy_mode_requested.emit)
        self.visual_status = QLabel("视觉已关闭")
        self.visual_status.setObjectName("visualStatus")
        self.visual_status.setWordWrap(True)
        visual_toolbar = QHBoxLayout()
        visual_toolbar.setContentsMargins(0, 0, 0, 0)
        visual_toolbar.setSpacing(5)
        visual_toolbar.addWidget(self.screenshot_button)
        visual_toolbar.addWidget(self.visual_source_combo)
        visual_toolbar.addWidget(self.visual_start_button)
        visual_toolbar.addWidget(self.visual_stop_button)
        visual_toolbar.addWidget(self.privacy_button)

        self.action_button = QPushButton("发送")
        self.action_button.setObjectName("actionButton")
        self.action_button.setAutoDefault(False)
        self.action_button.clicked.connect(self._on_action_clicked)

        composer = QHBoxLayout()
        composer.setContentsMargins(0, 0, 0, 0)
        composer.setSpacing(8)
        composer.addWidget(self.input, 1)
        composer.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignBottom)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)
        layout.addLayout(header)
        layout.addWidget(self.provider_banner)
        layout.addLayout(conversation_bar)
        layout.addWidget(self.scroll_area, 1)
        layout.addWidget(self.status_label)
        layout.addLayout(voice_toolbar)
        layout.addLayout(visual_toolbar)
        layout.addWidget(self.visual_status)
        layout.addLayout(attachment_composer)
        layout.addLayout(composer)

        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self._request_hide)
        self.conversation_combo.activated.connect(self._request_conversation_switch)
        self.new_conversation_button.clicked.connect(self._request_new_conversation)
        self.manage_history_button.clicked.connect(self.history_requested.emit)
        self._sync_conversation_controls()
        self._sync_action_enabled()
        self.set_voice_available(False)
        self.set_visual_state(False, "", "")

    @property
    def conversation_active(self) -> bool:
        return self._conversation_active

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    def set_always_on_top(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self._always_on_top:
            return
        visible = self.isVisible()
        position = self.pos()
        self._always_on_top = enabled
        self.setWindowFlags(self._window_flags())
        self.move(position)
        if visible:
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            self.show()
            self.raise_()
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)

    def _window_flags(self) -> Qt.WindowType:
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        return flags

    @property
    def message_ids(self) -> tuple[str, ...]:
        return tuple(self._message_order)

    def message_widget(self, message_id: str) -> MessageBubble | None:
        return self._messages.get(message_id)

    @property
    def draft_attachments(self) -> tuple[AttachmentSnapshot, ...]:
        return tuple(self._draft_attachments)

    def draft_attachment_paths(
        self,
        *,
        excluding_conversation_ids: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        excluded = set(excluding_conversation_ids)
        grouped = dict(self._conversation_attachment_drafts)
        if self._current_conversation_id is not None:
            grouped[self._current_conversation_id] = tuple(self._draft_attachments)
        return tuple(
            dict.fromkeys(
                attachment.relative_path
                for conversation_id, attachments in grouped.items()
                if conversation_id not in excluded
                for attachment in attachments
            )
        )

    def set_attachment_root(self, root: str | Path) -> None:
        self._attachment_root = Path(root)

    def set_attachment_processing(self, busy: bool) -> None:
        self._attachment_busy = bool(busy)
        self.attachment_status.setText("正在后台处理附件…" if busy else "")
        self._sync_action_enabled()
        self._sync_conversation_controls()

    def set_voice_available(
        self,
        available: bool,
        *,
        hands_free_available: bool = True,
    ) -> None:
        self._voice_available = bool(available)
        self._hands_free_available = bool(available and hands_free_available)
        self.push_to_talk_button.setEnabled(self._voice_available and not self._privacy_mode)
        self.hands_free_button.setEnabled(self._hands_free_available and not self._privacy_mode)
        self.voice_stop_button.setEnabled(self._voice_available and self._voice_state != "off")
        if not self._voice_available:
            self.voice_status.setText("语音未配置")

    def set_voice_state(self, state_object: object) -> None:
        state = str(getattr(state_object, "value", state_object))
        labels = {
            "off": "语音已关闭",
            "listening": "正在聆听",
            "capturing": "正在收音",
            "transcribing": "正在转写",
            "thinking": "正在思考",
            "speaking": "正在播音",
        }
        if state not in labels:
            return
        self._voice_state = state
        self.voice_status.setText(labels[state])
        self.voice_status.setProperty("state", state)
        self.voice_status.style().unpolish(self.voice_status)
        self.voice_status.style().polish(self.voice_status)
        self.voice_stop_button.setEnabled(self._voice_available and state != "off")
        self.push_to_talk_button.setText("松开发送" if state == "capturing" else "按住说话")

    def set_voice_status(self, message: str, error: bool = False) -> None:
        self.voice_status.setText(str(message))
        self.voice_status.setProperty("error", bool(error))
        self.voice_status.style().unpolish(self.voice_status)
        self.voice_status.style().polish(self.voice_status)

    def set_hands_free_checked(self, enabled: bool) -> None:
        blocked = self.hands_free_button.blockSignals(True)
        self.hands_free_button.setChecked(bool(enabled))
        self.hands_free_button.blockSignals(blocked)

    def set_visual_state(self, active: bool, _kind: str, source_name: str) -> None:
        self._visual_active = bool(active)
        self.visual_status.setText(f"正在共享 · {source_name}" if active else "视觉已关闭")
        self.visual_status.setProperty("active", bool(active))
        self.visual_status.style().unpolish(self.visual_status)
        self.visual_status.style().polish(self.visual_status)
        self.visual_start_button.setEnabled(not active and not self._privacy_mode)
        self.visual_source_combo.setEnabled(not active and not self._privacy_mode)
        self.visual_stop_button.setEnabled(active)

    def set_privacy_mode(self, enabled: bool) -> None:
        self._privacy_mode = bool(enabled)
        blocked = self.privacy_button.blockSignals(True)
        self.privacy_button.setChecked(self._privacy_mode)
        self.privacy_button.blockSignals(blocked)
        self.visual_start_button.setEnabled(not self._visual_active and not self._privacy_mode)
        self.visual_source_combo.setEnabled(not self._visual_active and not self._privacy_mode)
        self.screenshot_button.setEnabled(not self._privacy_mode)
        self.push_to_talk_button.setEnabled(self._voice_available and not self._privacy_mode)
        self.hands_free_button.setEnabled(self._hands_free_available and not self._privacy_mode)
        if self._privacy_mode:
            self.visual_status.setText("隐私模式：实时采集已停止")

    def add_draft_attachment(self, attachment: AttachmentSnapshot) -> bool:
        if attachment.attachment_id in {
            existing.attachment_id for existing in self._draft_attachments
        }:
            return False
        candidates = (*self._draft_attachments, attachment)
        if len(candidates) > 5 or sum(item.size_bytes for item in candidates) > 50 * 1024**2:
            self.set_status("附件总数或总大小超过限制。", kind="error")
            return False
        self._draft_attachments.append(attachment)
        self._render_draft_attachments()
        self._sync_action_enabled()
        return True

    def clear_draft_attachments(self) -> None:
        self._draft_attachments.clear()
        self._render_draft_attachments()
        self._sync_action_enabled()

    def _remove_draft_attachment(self, attachment_id: str) -> None:
        self._draft_attachments = [
            attachment
            for attachment in self._draft_attachments
            if attachment.attachment_id != attachment_id
        ]
        self._render_draft_attachments()
        self._sync_action_enabled()

    def _render_draft_attachments(self) -> None:
        while self.attachment_list_layout.count():
            item = self.attachment_list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for attachment in self._draft_attachments:
            row = QWidget()
            row.setObjectName("draftAttachmentRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(6, 3, 6, 3)
            label = QLabel(
                f"{attachment.display_name} · {_format_bytes(attachment.size_bytes)}"
                f"{' · 文本已截断' if attachment.text_truncated else ''}"
            )
            label.setWordWrap(True)
            remove = QPushButton("移除")
            remove.setAutoDefault(False)
            remove.setAccessibleName(f"移除附件 {attachment.display_name}")
            remove.clicked.connect(
                lambda _checked=False, identifier=attachment.attachment_id: (
                    self._remove_draft_attachment(identifier)
                )
            )
            row_layout.addWidget(label, 1)
            row_layout.addWidget(remove)
            self.attachment_list_layout.addWidget(row)
        self.attachment_list.setVisible(bool(self._draft_attachments))

    @property
    def provider_mode(self) -> str:
        return self._provider_mode

    def set_provider_mode(self, mode: str, *, provider_name: str | None = None) -> None:
        """Present explicit mock, configured provider, or unconfigured modes."""

        if mode not in {"mock", "provider", "unconfigured"}:
            raise ValueError(f"Unsupported chat provider mode: {mode}")
        self._provider_mode = mode
        self._chat_enabled = mode != "unconfigured"
        if mode == "mock":
            banner = "本地模拟模式 · 不会连接网络或使用 API 密钥"
            empty = "还没有消息。\n输入文字，验证本地模拟流式对话。"
            placeholder = "输入消息；Enter 发送，Shift+Enter 换行"
        elif mode == "provider":
            banner = f"真实模型 · {provider_name or '已配置供应商'}"
            empty = "还没有消息。\n输入文字开始对话。"
            placeholder = "输入消息；Enter 发送，Shift+Enter 换行"
        else:
            banner = "尚未配置对话模型 · 请先打开模型设置并通过连接测试"
            empty = "尚未配置可用的对话模型。\n点击右上角“模型设置”完成配置。"
            placeholder = "请先配置对话模型"
        self.provider_banner.setText(banner)
        self.provider_banner.setProperty("mode", mode)
        self.provider_banner.style().unpolish(self.provider_banner)
        self.provider_banner.style().polish(self.provider_banner)
        self.empty_state.setText(empty)
        self.input.setPlaceholderText(placeholder)
        self.input.setEnabled(
            self._chat_enabled and self._storage_ready and not self._conversation_switch_pending
        )
        self._sync_retry_enabled()
        self._sync_action_enabled()

    def set_storage_availability(self, ready: bool, *, read_only: bool = False) -> None:
        """Allow queued startup input, but disable writes after fail-closed opening."""

        self._storage_ready = not read_only
        self.input.setEnabled(
            self._chat_enabled and self._storage_ready and not self._conversation_switch_pending
        )
        if read_only:
            self.set_status("本地数据库处于只读保护状态，无法发送新消息。", kind="error")
        elif not ready:
            self.set_status("正在初始化本地聊天数据…", kind="working")
        self._sync_retry_enabled()
        self._sync_conversation_controls()
        self._sync_action_enabled()

    def set_conversations(
        self,
        conversations: Iterable[object],
        selected_id: str | None = None,
    ) -> None:
        """Refresh the compact switcher without initiating a conversation change."""

        previous_id = self._current_conversation_id
        rows = [_conversation_spec(conversation) for conversation in conversations]
        valid_ids = {conversation_id for conversation_id, _label, _tooltip in rows}
        self._conversation_drafts = {
            conversation_id: draft
            for conversation_id, draft in self._conversation_drafts.items()
            if conversation_id in valid_ids
        }
        self._conversation_attachment_drafts = {
            conversation_id: attachments
            for conversation_id, attachments in self._conversation_attachment_drafts.items()
            if conversation_id in valid_ids
        }
        with QSignalBlocker(self.conversation_combo):
            self.conversation_combo.clear()
            selected_index = -1
            for index, (conversation_id, label, tooltip) in enumerate(rows):
                self.conversation_combo.addItem(label, conversation_id)
                self.conversation_combo.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
                if conversation_id == selected_id:
                    selected_index = index
            if selected_index >= 0:
                self.conversation_combo.setCurrentIndex(selected_index)
            elif self.conversation_combo.count():
                self.conversation_combo.setCurrentIndex(0)

        current_data = self.conversation_combo.currentData()
        self._current_conversation_id = None if current_data is None else str(current_data)
        if previous_id is not None and previous_id != self._current_conversation_id:
            self.input.setPlainText(
                self._conversation_drafts.get(self._current_conversation_id or "", "")
            )
            self._draft_attachments = list(
                self._conversation_attachment_drafts.get(
                    self._current_conversation_id or "",
                    (),
                )
            )
            self._render_draft_attachments()
        self._history_ready = True
        self._sync_conversation_controls()

    def set_conversation_switch_pending(self, pending: bool) -> None:
        if pending and not self._conversation_switch_pending:
            self._remember_current_draft()
        self._conversation_switch_pending = bool(pending)
        self.input.setEnabled(
            self._chat_enabled and self._storage_ready and not self._conversation_switch_pending
        )
        self._sync_retry_enabled()
        self._sync_conversation_controls()
        self._sync_action_enabled()

    def show_and_focus(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self.input, self.input.setFocus)

    def resize_for_work_area(self, work_area: QRect) -> None:
        self.setFixedSize(compact_panel_size(work_area))
        self._resize_bubbles()

    @Slot(object)
    def set_conversation_state(
        self,
        state: object,
        detail: str | None = None,
        *,
        focus_mode: bool = False,
    ) -> None:
        state_name = _enum_text(state).lower()
        self._conversation_active = state_name in _ACTIVE_STATES
        self._turn_locked = state_name != "idle"
        self._stop_pending = False
        if (
            self._conversation_active
            and self._pending_send_text is not None
            and self.input.toPlainText() == self._pending_send_text
        ):
            self.input.clear()
            self.clear_draft_attachments()
            if self._current_conversation_id is not None:
                self._conversation_drafts.pop(self._current_conversation_id, None)
                self._conversation_attachment_drafts.pop(
                    self._current_conversation_id,
                    None,
                )
        self._send_pending = False
        self._pending_send_text = None
        self._pending_send_attachments = ()
        status, kind = _STATE_PRESENTATION.get(state_name, (state_name, "neutral"))
        focus_active = focus_mode and state_name in {"sending", "waiting_first_chunk"}
        if focus_active:
            kind = "focus"
            self.status_label.setToolTip(FOCUS_STATUS_TOOLTIP)
            self.status_label.setAccessibleDescription(FOCUS_STATUS_TOOLTIP)
        else:
            self.status_label.setToolTip("")
            self.status_label.setAccessibleDescription("")
        self.set_status(detail or status, kind=kind)
        self.action_button.setText("停止" if self._conversation_active else "发送")
        self.action_button.setProperty("active", self._conversation_active)
        self.action_button.style().unpolish(self.action_button)
        self.action_button.style().polish(self.action_button)
        self._sync_retry_enabled()
        self._sync_conversation_controls()
        self._sync_action_enabled()

    def set_foreground_preparing(self, preparing: bool) -> None:
        """Lock duplicate send/retry actions while the provider lane is preempted."""

        if preparing:
            self._turn_locked = True
        elif not self._conversation_active:
            self._turn_locked = False
            self._send_pending = False
            self._pending_send_text = None
            self._pending_send_attachments = ()
        self._sync_retry_enabled()
        self._sync_conversation_controls()
        self._sync_action_enabled()

    def set_status(self, text: str, *, kind: str = "neutral") -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("kind", kind)
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_message_pagination(self, *, has_older: bool, loading: bool = False) -> None:
        """Advertise whether reaching the top should request another 40 rows."""

        self._has_older_messages = has_older
        self._loading_older_messages = loading
        if has_older and not loading:
            QTimer.singleShot(
                0,
                self,
                lambda: self._on_scroll_value_changed(self.scroll_area.verticalScrollBar().value()),
            )

    @Slot(int)
    def _on_scroll_value_changed(self, value: int) -> None:
        if value > 0 or not self._has_older_messages or self._loading_older_messages:
            return
        self._loading_older_messages = True
        self.load_older_requested.emit()

    def append_message(
        self,
        message_id: str,
        role: object,
        text: str,
        *,
        status: object | None = None,
        retryable: bool = False,
        retry_id: str | None = None,
        attachments: tuple[AttachmentSnapshot, ...] = (),
        input_modality: object = "text",
        companion_cue_id: str | None = None,
        companion_source_label: str | None = None,
    ) -> MessageBubble:
        if message_id in self._messages:
            raise ValueError(f"A chat message with id {message_id!r} already exists.")
        follow_tail = self._is_near_bottom()
        bubble = self._create_bubble(
            message_id,
            _normalise_role(role),
            text,
            status=status,
            retryable=retryable,
            retry_id=retry_id,
            attachments=attachments,
            input_modality=input_modality,
            companion_cue_id=companion_cue_id,
            companion_source_label=companion_source_label,
        )
        self._message_order.append(message_id)
        self.message_layout.addWidget(bubble, 0, _bubble_alignment(bubble.role))
        self._after_message_change()
        if follow_tail:
            self._scroll_to_bottom_later()
        return bubble

    def update_message(
        self,
        message_id: str,
        *,
        text: str | None = None,
        status: object | None = None,
        retryable: bool | None = None,
        retry_id: str | None = None,
        attachments: tuple[AttachmentSnapshot, ...] | None = None,
    ) -> MessageBubble:
        bubble = self._messages[message_id]
        follow_tail = self._is_near_bottom()
        bubble.set_message(
            bubble.text if text is None else text,
            status=status,
            retryable=bubble.retry_button.isVisible() if retryable is None else retryable,
            retry_id=retry_id,
        )
        bubble.set_retry_enabled(not self._turn_locked)
        if attachments is not None:
            bubble.set_attachments(attachments)
        if follow_tail:
            self._scroll_to_bottom_later()
        return bubble

    def prepend_messages(self, messages: Iterable[object]) -> None:
        specs = [_message_spec(message) for message in messages]
        specs = [spec for spec in specs if spec[0] not in self._messages]
        if not specs:
            return

        scroll_bar = self.scroll_area.verticalScrollBar()
        old_value = scroll_bar.value()
        old_maximum = scroll_bar.maximum()
        for index, (
            message_id,
            role,
            text,
            status,
            retryable,
            retry_id,
            attachments,
            input_modality,
            companion_cue_id,
            companion_source_label,
        ) in enumerate(specs):
            bubble = self._create_bubble(
                message_id,
                role,
                text,
                status=status,
                retryable=retryable,
                retry_id=retry_id,
                attachments=attachments,
                input_modality=input_modality,
                companion_cue_id=companion_cue_id,
                companion_source_label=companion_source_label,
            )
            self._message_order.insert(index, message_id)
            self.message_layout.insertWidget(index + 1, bubble, 0, _bubble_alignment(role))
        self._after_message_change()

        def preserve_viewport() -> None:
            scroll_bar.setValue(old_value + scroll_bar.maximum() - old_maximum)

        QTimer.singleShot(0, self.scroll_area, preserve_viewport)

    @Slot(object)
    def render_turn(self, turn: object) -> None:
        """Append or incrementally update a duck-typed ConversationTurn snapshot."""

        turn_id = str(_member(turn, "turn_id", "id"))
        user_message = _member(turn, "user_message", default=None)
        assistant_message = _member(turn, "assistant_message", default=None)
        if user_message is not None:
            self._render_turn_message(turn_id, user_message, retryable=False)
        if assistant_message is not None:
            terminal = _member(turn, "terminal_reason", "reason", default=None)
            error = _member(turn, "error", default=None)
            status_text = _member(turn, "status_text", default=None)
            terminal_text = _enum_text(terminal).lower() if terminal is not None else ""
            retryable = bool(error) or any(
                marker in terminal_text for marker in ("failed", "error", "timeout")
            )
            status: object | None = _member(assistant_message, "status", default=None)
            if status_text:
                status = status_text
            elif error:
                status = str(error)
            elif status is None and terminal is not None:
                status = terminal
            self._render_turn_message(
                turn_id,
                assistant_message,
                status=status,
                retryable=retryable,
            )

    add_turn = render_turn
    update_turn = render_turn

    def prepend_turns(self, turns: Iterable[object]) -> None:
        specs: list[dict[str, object]] = []
        for turn in turns:
            turn_id = str(_member(turn, "turn_id", "id"))
            for attribute in ("user_message", "assistant_message"):
                message = _member(turn, attribute, default=None)
                if message is None:
                    continue
                message_id = str(
                    _member(message, "message_id", "id", default=f"{turn_id}:{attribute}")
                )
                default_role = "user" if attribute == "user_message" else "assistant"
                role = _member(message, "role", default=default_role)
                specs.append(
                    {
                        "message_id": message_id,
                        "role": role,
                        "text": _member(message, "content", "text", default=""),
                        "status": _member(message, "status", default=None),
                        "retryable": False,
                        "retry_id": turn_id,
                        "attachments": tuple(_member(message, "attachments", default=())),
                        "companion_cue_id": _member(message, "companion_cue_id", default=None),
                        "companion_source_label": _member(
                            message, "companion_source_label", default=None
                        ),
                    }
                )
        self.prepend_messages(specs)

    def clear_messages(self) -> None:
        for bubble in self._messages.values():
            self.message_layout.removeWidget(bubble)
            bubble.deleteLater()
        self._messages.clear()
        self._message_order.clear()
        self._after_message_change()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API name
        self._request_hide()
        event.ignore()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API name
        super().showEvent(event)
        self.visibility_changed.emit(True)
        QTimer.singleShot(0, self.input, self.input.setFocus)
        self._schedule_bubble_resize()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API name
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API name
        super().resizeEvent(event)
        self._schedule_bubble_resize()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API name
        if event.mimeData().hasUrls() and any(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API name
        paths = tuple(url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile())
        if not paths:
            event.ignore()
            return
        self._request_attachment_paths(paths, AttachmentSource.DROP)
        event.acceptProposedAction()

    def eventFilter(self, watched: object, event: QEvent) -> bool:  # noqa: N802 - Qt API name
        if watched is self.scroll_area.viewport() and event.type() is QEvent.Type.Resize:
            self._schedule_bubble_resize()
        return super().eventFilter(watched, event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API name
        del event
        option = QStyleOption()
        option.initFrom(self)
        painter = QPainter(self)
        self.style().drawPrimitive(QStyle.PrimitiveElement.PE_Widget, option, painter, self)

    def _request_hide(self) -> None:
        self.hide_requested.emit()
        self.hide()

    @Slot(int)
    def _request_conversation_switch(self, index: int) -> None:
        value = self.conversation_combo.itemData(index)
        if value is None:
            return
        conversation_id = str(value)
        if conversation_id == self._current_conversation_id:
            return
        current_index = self.conversation_combo.findData(self._current_conversation_id)
        with QSignalBlocker(self.conversation_combo):
            self.conversation_combo.setCurrentIndex(current_index)
        self.set_conversation_switch_pending(True)
        self.conversation_switch_requested.emit(conversation_id)

    def _request_new_conversation(self) -> None:
        if not self.new_conversation_button.isEnabled():
            return
        self.set_conversation_switch_pending(True)
        self.new_conversation_requested.emit()

    def _choose_attachments(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            "选择图片或文档",
            "",
            "支持的附件 (*.png *.jpg *.jpeg *.webp *.pdf *.txt *.md *.docx);;"
            "图片 (*.png *.jpg *.jpeg *.webp);;文档 (*.pdf *.txt *.md *.docx)",
        )
        if paths:
            self._request_attachment_paths(tuple(paths), AttachmentSource.FILE_PICKER)

    def _request_attachment_paths(
        self,
        paths: tuple[str, ...],
        source: AttachmentSource,
    ) -> None:
        if self._attachment_busy or self._turn_locked:
            return
        self.attachment_paths_requested.emit(paths, source)

    def _request_pasted_image(self, image: QImage) -> None:
        if self._attachment_busy or self._turn_locked or image.isNull():
            return
        self.attachment_image_requested.emit(
            image.copy(),
            "clipboard-image.png",
            AttachmentSource.CLIPBOARD,
        )

    def _request_send(self) -> None:
        if (
            not self._chat_enabled
            or not self._storage_ready
            or self._turn_locked
            or self._send_pending
            or self._attachment_busy
        ):
            return
        text = self.input.toPlainText()
        attachments = tuple(self._draft_attachments)
        if not text.strip() and not attachments:
            return
        self._send_pending = True
        self._pending_send_text = text
        self._pending_send_attachments = attachments
        self._sync_action_enabled()
        if attachments:
            self.send_with_attachments_requested.emit(text, attachments)
        else:
            self.send_requested.emit(text)

        def release_unaccepted_send() -> None:
            if not self._turn_locked:
                self._send_pending = False
                self._pending_send_text = None
                self._pending_send_attachments = ()
                self._sync_action_enabled()

        QTimer.singleShot(0, self, release_unaccepted_send)

    def _on_action_clicked(self) -> None:
        if self._conversation_active:
            if self._stop_pending:
                return
            self._stop_pending = True
            self.action_button.setEnabled(False)
            self.action_button.setText("正在停止…")
            self.stop_requested.emit()
        else:
            self._request_send()

    def _sync_action_enabled(self) -> None:
        if hasattr(self, "attach_button"):
            self.attach_button.setEnabled(
                self._chat_enabled
                and self._storage_ready
                and not self._conversation_switch_pending
                and not self._turn_locked
                and not self._attachment_busy
                and len(self._draft_attachments) < 5
            )
        if not self._chat_enabled or not self._storage_ready or self._conversation_switch_pending:
            self.action_button.setEnabled(False)
            return
        if self._conversation_active:
            self.action_button.setEnabled(not self._stop_pending)
        elif self._turn_locked:
            self.action_button.setEnabled(False)
        else:
            self.action_button.setEnabled(
                not self._send_pending
                and not self._attachment_busy
                and bool(self.input.toPlainText().strip() or self._draft_attachments)
            )

    def _sync_retry_enabled(self) -> None:
        for bubble in self._messages.values():
            bubble.set_retry_enabled(
                self._chat_enabled
                and self._storage_ready
                and not self._turn_locked
                and not self._conversation_switch_pending
            )

    def _sync_conversation_controls(self) -> None:
        changing = self._conversation_switch_pending or self._turn_locked or self._attachment_busy
        self.conversation_combo.setEnabled(
            self._history_ready and self.conversation_combo.count() > 1 and not changing
        )
        self.new_conversation_button.setEnabled(
            self._history_ready and self._storage_ready and not changing
        )

    def _remember_current_draft(self) -> None:
        conversation_id = self._current_conversation_id
        if conversation_id is None:
            return
        draft = self.input.toPlainText()
        if draft:
            self._conversation_drafts[conversation_id] = draft
        else:
            self._conversation_drafts.pop(conversation_id, None)
        if self._draft_attachments:
            self._conversation_attachment_drafts[conversation_id] = tuple(self._draft_attachments)
        else:
            self._conversation_attachment_drafts.pop(conversation_id, None)

    def _create_bubble(
        self,
        message_id: str,
        role: str,
        text: str,
        *,
        status: object | None,
        retryable: bool,
        retry_id: str | None,
        attachments: tuple[AttachmentSnapshot, ...] = (),
        input_modality: object = "text",
        companion_cue_id: str | None = None,
        companion_source_label: str | None = None,
    ) -> MessageBubble:
        bubble = MessageBubble(
            message_id,
            role,
            text,
            status=_enum_text(status) if status is not None else None,
            retryable=retryable,
            retry_id=retry_id,
            attachments=attachments,
            attachment_root=self._attachment_root,
            input_modality=input_modality,
            companion_cue_id=companion_cue_id,
            companion_source_label=companion_source_label,
        )
        bubble.retry_clicked.connect(self.retry_requested.emit)
        bubble.companion_cue_clicked.connect(self.companion_cue_requested.emit)
        bubble.set_bubble_width(self._bubble_width())
        bubble.set_retry_enabled(not self._turn_locked)
        self._messages[message_id] = bubble
        return bubble

    def _render_turn_message(
        self,
        turn_id: str,
        message: object,
        *,
        status: object | None = None,
        retryable: bool,
    ) -> None:
        role = _member(message, "role", default="assistant")
        message_id = str(_member(message, "message_id", "id", default=f"{turn_id}:{role}"))
        text = str(_member(message, "content", "text", default=""))
        attachments = tuple(_member(message, "attachments", default=()))
        input_modality = _member(message, "input_modality", default="text")
        companion_cue_id = _member(message, "companion_cue_id", default=None)
        companion_source_label = _member(
            message,
            "companion_source_label",
            default=None,
        )
        if status is None:
            status = _member(message, "status", default=None)
        if message_id in self._messages:
            self.update_message(
                message_id,
                text=text,
                status=status,
                retryable=retryable,
                retry_id=turn_id,
                attachments=attachments,
            )
        else:
            self.append_message(
                message_id,
                role,
                text,
                status=status,
                retryable=retryable,
                retry_id=turn_id,
                attachments=attachments,
                input_modality=input_modality,
                companion_cue_id=(None if companion_cue_id is None else str(companion_cue_id)),
                companion_source_label=(
                    None if companion_source_label is None else str(companion_source_label)
                ),
            )

    def _after_message_change(self) -> None:
        self.empty_state.setVisible(not self._message_order)
        self._resize_bubbles()
        self._schedule_bubble_resize()
        self.message_container.adjustSize()

    def _bubble_width(self) -> int:
        viewport_width = self.scroll_area.viewport().width()
        if viewport_width <= 0:
            viewport_width = max(160, self.width() - 40)
        return max(120, viewport_width - 24)

    def _resize_bubbles(self) -> None:
        if not hasattr(self, "scroll_area"):
            return
        width = self._bubble_width()
        for bubble in self._messages.values():
            bubble.set_bubble_width(width)

    def _schedule_bubble_resize(self) -> None:
        if not hasattr(self, "scroll_area") or self._bubble_resize_scheduled:
            return
        self._bubble_resize_scheduled = True
        QTimer.singleShot(0, self.scroll_area, self._apply_scheduled_bubble_resize)

    def _apply_scheduled_bubble_resize(self) -> None:
        self._bubble_resize_scheduled = False
        self._resize_bubbles()

    def _is_near_bottom(self) -> bool:
        scroll_bar = self.scroll_area.verticalScrollBar()
        return scroll_bar.maximum() - scroll_bar.value() <= 24

    def _scroll_to_bottom_later(self) -> None:
        def scroll() -> None:
            scroll_bar = self.scroll_area.verticalScrollBar()
            scroll_bar.setValue(scroll_bar.maximum())
            QTimer.singleShot(
                0,
                self.scroll_area,
                lambda: scroll_bar.setValue(scroll_bar.maximum()),
            )

        QTimer.singleShot(0, self.scroll_area, scroll)


def _member(value: object, *names: str, default: Any = ...) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not ...:
        return default
    joined = ", ".join(names)
    raise ValueError(f"Chat view data is missing one of these fields: {joined}")


def _message_spec(
    message: object,
) -> tuple[
    str,
    str,
    str,
    object | None,
    bool,
    str | None,
    tuple[AttachmentSnapshot, ...],
    object,
    str | None,
    str | None,
]:
    message_id = str(_member(message, "message_id", "id"))
    role = _normalise_role(_member(message, "role"))
    text = str(_member(message, "content", "text", default=""))
    status = _member(message, "status", default=None)
    retryable = bool(_member(message, "retryable", default=False))
    retry_id = _member(message, "retry_id", "turn_id", default=None)
    attachments = tuple(_member(message, "attachments", default=()))
    input_modality = _member(message, "input_modality", default="text")
    companion_cue_id = _member(message, "companion_cue_id", default=None)
    companion_source_label = _member(message, "companion_source_label", default=None)
    return (
        message_id,
        role,
        text,
        status,
        retryable,
        None if retry_id is None else str(retry_id),
        attachments,
        input_modality,
        None if companion_cue_id is None else str(companion_cue_id),
        None if companion_source_label is None else str(companion_source_label),
    )


def _conversation_spec(conversation: object) -> tuple[str, str, str]:
    conversation_id = str(_member(conversation, "conversation_id", "id"))
    title = str(_member(conversation, "title", "name", default="新对话")).strip() or "新对话"
    timestamp = _member(
        conversation,
        "last_activity_at",
        "updated_at",
        "created_at",
        default="",
    )
    formatter = getattr(timestamp, "strftime", None)
    updated = str(formatter("%m-%d %H:%M")) if callable(formatter) else str(timestamp).strip()
    label = title if not updated else f"{title} · {updated}"
    tooltip = title if not updated else f"{title}\n最后活动：{updated}"
    return conversation_id, label, tooltip


def _normalise_role(role: object) -> str:
    value = _enum_text(role).lower()
    return "user" if value == "user" else "assistant"


def _enum_text(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


def _status_text(status: object | None) -> str:
    if status is None:
        return ""
    raw = _enum_text(status)
    return _STATUS_PRESENTATION.get(raw.lower(), raw)


def _bubble_alignment(role: str) -> Qt.AlignmentFlag:
    return Qt.AlignmentFlag.AlignRight if role == "user" else Qt.AlignmentFlag.AlignLeft


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KiB"
    return f"{size / 1024**2:.1f} MiB"


def _attachment_source_text(source: AttachmentSource) -> str:
    return {
        AttachmentSource.FILE_PICKER: "文件选择",
        AttachmentSource.DROP: "拖放",
        AttachmentSource.CLIPBOARD: "剪贴板",
        AttachmentSource.SCREENSHOT: "截图",
        AttachmentSource.SCREEN: "屏幕",
        AttachmentSource.WINDOW: "窗口",
        AttachmentSource.CAMERA: "相机",
    }[AttachmentSource(source)]


_PANEL_STYLESHEET = """
QWidget#chatPanel {
    background: #111827;
    border: 1px solid #334155;
    border-radius: 14px;
    color: #e5e7eb;
}
QLabel#panelTitle {
    color: #f8fafc;
    font-size: 17px;
    font-weight: 650;
}
QPushButton#closeButton {
    background: transparent;
    border: none;
    color: #94a3b8;
    font-size: 21px;
    min-width: 28px;
    min-height: 28px;
}
QPushButton#closeButton:hover { color: #f8fafc; background: #1e293b; border-radius: 6px; }
QPushButton#configureButton {
    background: transparent;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #cbd5e1;
    padding: 4px 8px;
}
QPushButton#configureButton:hover { color: #f8fafc; border-color: #22d3ee; }
QLabel#conversationLabel { color: #94a3b8; }
QComboBox#conversationSelector {
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #e2e8f0;
    min-height: 26px;
    padding: 2px 8px;
}
QComboBox#conversationSelector:hover { border-color: #22d3ee; }
QComboBox#conversationSelector:disabled { color: #64748b; border-color: #334155; }
QComboBox#conversationSelector QAbstractItemView {
    background: #0f172a;
    border: 1px solid #475569;
    color: #e2e8f0;
    selection-background-color: #164e63;
}
QPushButton#newConversationButton, QPushButton#manageHistoryButton {
    background: transparent;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #cbd5e1;
    min-height: 26px;
    padding: 2px 7px;
}
QPushButton#newConversationButton:hover, QPushButton#manageHistoryButton:hover {
    color: #f8fafc;
    border-color: #22d3ee;
}
QPushButton#newConversationButton:disabled { color: #64748b; border-color: #334155; }
QLabel#providerBanner {
    background: #172554;
    border: 1px solid #1d4ed8;
    border-radius: 7px;
    color: #bfdbfe;
    padding: 7px 9px;
}
QLabel#providerBanner[mode="provider"] {
    background: #052e16;
    border-color: #15803d;
    color: #bbf7d0;
}
QLabel#providerBanner[mode="unconfigured"] {
    background: #451a03;
    border-color: #b45309;
    color: #fed7aa;
}
QScrollArea#messageScroll, QWidget#messageContainer { background: transparent; }
QLabel#emptyState { color: #94a3b8; padding: 30px 18px; }
QFrame#userBubble {
    background: #164e63;
    border: 1px solid #0e7490;
    border-radius: 10px;
}
QFrame#assistantBubble {
    background: #1e293b;
    border: 1px solid #334155;
    border-radius: 10px;
}
QLabel#messageRole { color: #a5f3fc; font-size: 11px; font-weight: 600; }
QTextEdit#messageText { background: transparent; color: #f1f5f9; padding: 0; }
QLabel#messageStatus { color: #94a3b8; font-size: 11px; }
QWidget#messageAttachments, QWidget#draftAttachments { background: transparent; }
QWidget#draftAttachmentRow {
    background: #1e293b;
    border: 1px solid #475569;
    border-radius: 7px;
    color: #e2e8f0;
}
QLabel#attachmentMetadata { color: #cbd5e1; font-size: 11px; }
QLabel#attachmentThumbnail {
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 5px;
}
QLabel#attachmentStatus { color: #67e8f9; font-size: 11px; }
QLabel#voiceStatus { color: #94a3b8; font-size: 11px; }
QLabel#voiceStatus[state="listening"] { color: #67e8f9; }
QLabel#voiceStatus[state="capturing"] { color: #fbbf24; font-weight: 600; }
QLabel#voiceStatus[state="transcribing"], QLabel#voiceStatus[state="thinking"] {
    color: #c4b5fd;
}
QLabel#voiceStatus[state="speaking"] { color: #86efac; }
QLabel#voiceStatus[error="true"] { color: #fca5a5; }
QLabel#visualStatus { color: #94a3b8; font-size: 11px; }
QLabel#visualStatus[active="true"] { color: #fbbf24; font-weight: 600; }
QComboBox#visualSourceCombo {
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #e2e8f0;
    min-height: 26px;
}
QPushButton#screenshotButton, QPushButton#visualStartButton,
QPushButton#visualStopButton, QPushButton#privacyButton {
    background: transparent;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #cbd5e1;
    min-height: 26px;
    padding: 2px 7px;
}
QPushButton#privacyButton:checked { background: #7f1d1d; border-color: #f87171; }
QPushButton#pushToTalkButton, QPushButton#handsFreeButton, QPushButton#voiceStopButton {
    background: transparent;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #cbd5e1;
    min-height: 26px;
    padding: 2px 7px;
}
QPushButton#handsFreeButton:checked { background: #164e63; border-color: #22d3ee; }
QPushButton#pushToTalkButton:pressed { background: #78350f; border-color: #f59e0b; }
QPushButton#pushToTalkButton:disabled, QPushButton#handsFreeButton:disabled,
QPushButton#voiceStopButton:disabled { color: #64748b; border-color: #334155; }
QPushButton#attachButton {
    background: transparent;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #cbd5e1;
    min-height: 26px;
    padding: 2px 8px;
}
QPushButton#attachButton:hover { border-color: #22d3ee; color: #f8fafc; }
QPushButton#attachButton:disabled { border-color: #334155; color: #64748b; }
QPushButton#retryButton {
    background: transparent;
    border: 1px solid #64748b;
    border-radius: 5px;
    color: #e2e8f0;
    padding: 3px 8px;
}
QLabel#conversationStatus { color: #94a3b8; }
QLabel#conversationStatus[kind="working"] { color: #67e8f9; }
QLabel#conversationStatus[kind="focus"] { color: #c4b5fd; font-weight: 600; }
QLabel#conversationStatus[kind="success"] { color: #86efac; }
QLabel#conversationStatus[kind="error"] { color: #fca5a5; }
QPlainTextEdit#chatInput {
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 8px;
    color: #f8fafc;
    padding: 8px;
    selection-background-color: #0e7490;
}
QPlainTextEdit#chatInput:focus { border-color: #22d3ee; }
QPushButton#actionButton {
    background: #0891b2;
    border: none;
    border-radius: 8px;
    color: white;
    min-width: 68px;
    min-height: 38px;
    padding: 0 12px;
    font-weight: 600;
}
QPushButton#actionButton[active="true"] { background: #b45309; }
QPushButton#actionButton:disabled { background: #334155; color: #94a3b8; }
"""
