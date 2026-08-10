"""Focusable, pet-attached chat panel for the P4 conversation flow."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtCore import QEvent, QRect, QSignalBlocker, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QCloseEvent,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShortcut,
    QShowEvent,
    QTextOption,
)
from PySide6.QtWidgets import (
    QComboBox,
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

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API name
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.send_key_pressed.emit()
                event.accept()
            return
        super().keyPressEvent(event)


class MessageBubble(QFrame):
    """One selectable message plus its terminal status and retry action."""

    retry_clicked = Signal(str)

    def __init__(
        self,
        message_id: str,
        role: str,
        text: str,
        *,
        status: str | None = None,
        retryable: bool = False,
        retry_id: str | None = None,
    ) -> None:
        super().__init__()
        self.message_id = message_id
        self.role = role
        self.retry_id = retry_id or message_id
        self._text = text
        self._retryable = retryable

        self.setObjectName("userBubble" if role == "user" else "assistantBubble")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)

        role_label = QLabel("你" if role == "user" else "Amadeus")
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

        self.retry_button = QPushButton("重试")
        self.retry_button.setObjectName("retryButton")
        self.retry_button.setAutoDefault(False)
        self.retry_button.clicked.connect(self._emit_retry)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.retry_button, 0)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        layout.addWidget(role_label)
        layout.addWidget(self.text_view)
        layout.addLayout(footer)

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


class ChatPanel(QWidget):
    """Compact independent tool window driven only by application-layer events."""

    send_requested = Signal(str)
    stop_requested = Signal()
    retry_requested = Signal(str)
    hide_requested = Signal()
    configure_requested = Signal()
    conversation_switch_requested = Signal(str)
    new_conversation_requested = Signal()
    history_requested = Signal()
    load_older_requested = Signal()
    visibility_changed = Signal(bool)

    def __init__(self, *, always_on_top: bool = True) -> None:
        self._always_on_top = bool(always_on_top)
        super().__init__(None, self._window_flags())
        self._conversation_active = False
        self._turn_locked = False
        self._stop_pending = False
        self._send_pending = False
        self._pending_send_text: str | None = None
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

        self.setObjectName("chatPanel")
        self.setWindowTitle("Amadeus 对话")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
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
        layout.addLayout(composer)

        self._escape_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._escape_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self._request_hide)
        self.conversation_combo.activated.connect(self._request_conversation_switch)
        self.new_conversation_button.clicked.connect(self._request_new_conversation)
        self.manage_history_button.clicked.connect(self.history_requested.emit)
        self._sync_conversation_controls()
        self._sync_action_enabled()

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
            self._chat_enabled
            and self._storage_ready
            and not self._conversation_switch_pending
        )
        self._sync_retry_enabled()
        self._sync_action_enabled()

    def set_storage_availability(self, ready: bool, *, read_only: bool = False) -> None:
        """Allow queued startup input, but disable writes after fail-closed opening."""

        self._storage_ready = not read_only
        self.input.setEnabled(
            self._chat_enabled
            and self._storage_ready
            and not self._conversation_switch_pending
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
        self._history_ready = True
        self._sync_conversation_controls()

    def set_conversation_switch_pending(self, pending: bool) -> None:
        if pending and not self._conversation_switch_pending:
            self._remember_current_draft()
        self._conversation_switch_pending = bool(pending)
        self.input.setEnabled(
            self._chat_enabled
            and self._storage_ready
            and not self._conversation_switch_pending
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
        self._send_pending = False
        self._pending_send_text = None
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
        for index, (message_id, role, text, status, retryable, retry_id) in enumerate(specs):
            bubble = self._create_bubble(
                message_id,
                role,
                text,
                status=status,
                retryable=retryable,
                retry_id=retry_id,
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

    def _request_send(self) -> None:
        if (
            not self._chat_enabled
            or not self._storage_ready
            or self._turn_locked
            or self._send_pending
        ):
            return
        text = self.input.toPlainText()
        if not text.strip():
            return
        self._send_pending = True
        self._pending_send_text = text
        self._sync_action_enabled()
        self.send_requested.emit(text)

        def release_unaccepted_send() -> None:
            if not self._turn_locked:
                self._send_pending = False
                self._pending_send_text = None
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
        if (
            not self._chat_enabled
            or not self._storage_ready
            or self._conversation_switch_pending
        ):
            self.action_button.setEnabled(False)
            return
        if self._conversation_active:
            self.action_button.setEnabled(not self._stop_pending)
        elif self._turn_locked:
            self.action_button.setEnabled(False)
        else:
            self.action_button.setEnabled(
                not self._send_pending and bool(self.input.toPlainText().strip())
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
        changing = self._conversation_switch_pending or self._turn_locked
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

    def _create_bubble(
        self,
        message_id: str,
        role: str,
        text: str,
        *,
        status: object | None,
        retryable: bool,
        retry_id: str | None,
    ) -> MessageBubble:
        bubble = MessageBubble(
            message_id,
            role,
            text,
            status=_enum_text(status) if status is not None else None,
            retryable=retryable,
            retry_id=retry_id,
        )
        bubble.retry_clicked.connect(self.retry_requested.emit)
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
        if status is None:
            status = _member(message, "status", default=None)
        if message_id in self._messages:
            self.update_message(
                message_id,
                text=text,
                status=status,
                retryable=retryable,
                retry_id=turn_id,
            )
        else:
            self.append_message(
                message_id,
                role,
                text,
                status=status,
                retryable=retryable,
                retry_id=turn_id,
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


def _message_spec(message: object) -> tuple[str, str, str, object | None, bool, str | None]:
    message_id = str(_member(message, "message_id", "id"))
    role = _normalise_role(_member(message, "role"))
    text = str(_member(message, "content", "text", default=""))
    status = _member(message, "status", default=None)
    retryable = bool(_member(message, "retryable", default=False))
    retry_id = _member(message, "retry_id", "turn_id", default=None)
    return message_id, role, text, status, retryable, None if retry_id is None else str(retry_id)


def _conversation_spec(conversation: object) -> tuple[str, str, str]:
    conversation_id = str(_member(conversation, "conversation_id", "id"))
    title = (
        str(_member(conversation, "title", "name", default="新对话")).strip()
        or "新对话"
    )
    timestamp = _member(
        conversation,
        "last_activity_at",
        "updated_at",
        "created_at",
        default="",
    )
    formatter = getattr(timestamp, "strftime", None)
    updated = (
        str(formatter("%m-%d %H:%M")) if callable(formatter) else str(timestamp).strip()
    )
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
