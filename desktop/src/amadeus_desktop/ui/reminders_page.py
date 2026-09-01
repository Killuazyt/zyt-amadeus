"""Settings page for local one-shot reminders and scheduled follow-ups."""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QDate, QDateTime, QTime, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.local_data_service import ReminderListSnapshot
from amadeus_desktop.storage_models import (
    TemporalCommitment,
    TemporalCommitmentKind,
    TemporalCommitmentStatus,
)
from amadeus_desktop.temporal_commitments import TemporalDraftSpec

_STATUS_LABELS = {
    TemporalCommitmentStatus.DRAFT: "待确认",
    TemporalCommitmentStatus.SCHEDULED: "已安排",
    TemporalCommitmentStatus.DUE: "已到期",
    TemporalCommitmentStatus.SURFACED: "待处理",
    TemporalCommitmentStatus.COMPLETED: "已完成",
    TemporalCommitmentStatus.CANCELLED: "已取消",
}
_KIND_LABELS = {
    TemporalCommitmentKind.REMINDER: "精确提醒",
    TemporalCommitmentKind.SCHEDULED_FOLLOWUP: "定时跟进",
}


class RemindersPage(QWidget):
    refresh_requested = Signal(str, str)
    create_requested = Signal()
    save_requested = Signal(str, object)
    complete_requested = Signal(str)
    cancel_requested = Signal(str)
    snooze_requested = Signal(str, int)
    delete_requested = Signal(str)
    source_requested = Signal(str, str)
    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: dict[str, TemporalCommitment] = {}
        self._selected_id: str | None = None
        self._writable = False

        title = QLabel("提醒")
        title.setStyleSheet("font-size: 18px; font-weight: 650;")
        description = QLabel(
            "一次性提醒和定时跟进完全在本地确认与调度。应用退出期间不会后台运行，"
            "错过的项目会在下次启动时聚合提示。"
        )
        description.setWordWrap(True)

        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索提醒正文")
        self.filter = QComboBox()
        for label, value in (
            ("全部", ""),
            ("待确认", "draft"),
            ("已安排", "scheduled"),
            ("已到期 / 待处理", "outstanding"),
            ("已完成", "completed"),
            ("已取消", "cancelled"),
        ):
            self.filter.addItem(label, value)
        self.refresh_button = QPushButton("刷新")
        self.new_button = QPushButton("新建提醒")
        self.export_button = QPushButton("导出 JSON…")
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.search, 1)
        toolbar.addWidget(self.filter)
        toolbar.addWidget(self.refresh_button)
        toolbar.addWidget(self.new_button)
        toolbar.addWidget(self.export_button)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ("类型", "状态", "内容", "时间", "来源", "更新时间")
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)

        self.kind = QComboBox()
        self.kind.addItem("精确提醒", TemporalCommitmentKind.REMINDER.value)
        self.kind.addItem("定时跟进", TemporalCommitmentKind.SCHEDULED_FOLLOWUP.value)
        self.content = QTextEdit()
        self.content.setAcceptRichText(False)
        self.content.setPlaceholderText("最多 500 字")
        self.content.setMaximumHeight(110)
        self.due = QDateTimeEdit()
        self.due.setCalendarPopup(True)
        self.due.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.show_content = QCheckBox("允许单项系统通知显示正文")
        self.original_time = QLabel("—")
        self.original_time.setWordWrap(True)
        self.source = QLabel("—")
        self.source.setWordWrap(True)

        form = QFormLayout()
        form.addRow("类型", self.kind)
        form.addRow("内容", self.content)
        form.addRow("本地时间", self.due)
        form.addRow("隐私", self.show_content)
        form.addRow("原确认时间", self.original_time)
        form.addRow("来源", self.source)

        self.save_button = QPushButton("确认并安排")
        self.complete_button = QPushButton("标记完成")
        self.snooze_button = QPushButton("稍后 10 分钟")
        self.cancel_button = QPushButton("取消")
        self.source_button = QPushButton("跳转来源")
        self.delete_button = QPushButton("永久删除")
        actions = QHBoxLayout()
        for button in (
            self.save_button,
            self.complete_button,
            self.snooze_button,
            self.cancel_button,
            self.source_button,
            self.delete_button,
        ):
            actions.addWidget(button)
        actions.addStretch(1)

        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addLayout(form)
        detail_layout.addLayout(actions)
        detail_layout.addStretch(1)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.table)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        self.status = QLabel("提醒数据正在初始化…")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(description)
        layout.addLayout(toolbar)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)

        self.refresh_button.clicked.connect(self._request_refresh)
        self.search.returnPressed.connect(self._request_refresh)
        self.filter.currentIndexChanged.connect(self._request_refresh)
        self.new_button.clicked.connect(self.create_requested.emit)
        self.export_button.clicked.connect(self.export_requested.emit)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.save_button.clicked.connect(self._save)
        self.complete_button.clicked.connect(self._complete)
        self.snooze_button.clicked.connect(self._snooze)
        self.cancel_button.clicked.connect(self._cancel)
        self.source_button.clicked.connect(self._open_source)
        self.delete_button.clicked.connect(self._delete)
        self._sync_controls()

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    def set_writable(self, writable: bool) -> None:
        self._writable = bool(writable)
        self.new_button.setEnabled(self._writable)
        self._sync_controls()

    def set_snapshot(self, value: object) -> None:
        if not isinstance(value, ReminderListSnapshot):
            self.set_status("提醒数据格式无效。", error=True)
            return
        previous = self._selected_id
        self._rows = {item.commitment_id: item for item in value.commitments}
        self.table.setRowCount(0)
        for commitment in value.commitments:
            row = self.table.rowCount()
            self.table.insertRow(row)
            version = commitment.current_version
            due = (
                "待编辑"
                if version.due_at_utc is None
                else version.due_at_utc.astimezone().strftime("%Y-%m-%d %H:%M")
            )
            source = "手动创建"
            if commitment.source_kind.value == "chat":
                source = "原聊天已删除" if commitment.source_deleted else "聊天命令"
            values = (
                _KIND_LABELS[version.kind],
                _STATUS_LABELS[commitment.status],
                version.content,
                due,
                source,
                commitment.updated_at.astimezone().strftime("%Y-%m-%d %H:%M"),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, commitment.commitment_id)
                self.table.setItem(row, column, item)
            if commitment.commitment_id == previous:
                self.table.selectRow(row)
        self.table.resizeColumnsToContents()
        if self.table.currentRow() < 0 and self.table.rowCount():
            self.table.selectRow(0)
        if not self.table.rowCount():
            self._selected_id = None
            self._clear_detail()
        self.set_status(
            f"共 {len(value.commitments)} 项；其中 {value.outstanding_count} 项已到期待处理。"
        )

    def select_commitment(self, commitment_id: str) -> bool:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == commitment_id:
                self.table.selectRow(row)
                self.table.scrollToItem(item)
                return True
        return False

    def show_outstanding(self) -> None:
        index = self.filter.findData("outstanding")
        if index >= 0:
            self.filter.setCurrentIndex(index)

    def show_all(self) -> None:
        index = self.filter.findData("")
        if index >= 0:
            self.filter.setCurrentIndex(index)

    def set_status(self, text: str, *, error: bool = False) -> None:
        self.status.setText(str(text))
        self.status.setStyleSheet("color: #b91c1c;" if error else "color: #475569;")

    def _request_refresh(self, *_args: object) -> None:
        self.refresh_requested.emit(self.search.text(), str(self.filter.currentData() or ""))

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        item = None if row < 0 else self.table.item(row, 0)
        identifier = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        commitment = self._rows.get(str(identifier)) if identifier is not None else None
        if commitment is None:
            self._selected_id = None
            self._clear_detail()
            return
        self._selected_id = commitment.commitment_id
        version = commitment.current_version
        self.kind.setCurrentIndex(max(0, self.kind.findData(version.kind.value)))
        self.content.setPlainText(version.content)
        if version.due_at_utc is not None:
            local = version.due_at_utc.astimezone()
            self.due.setDateTime(
                QDateTime(
                    QDate(local.year, local.month, local.day),
                    QTime(local.hour, local.minute),
                )
            )
        else:
            self.due.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self.show_content.setChecked(version.show_content)
        self.original_time.setText(
            "尚未确定"
            if version.original_local_time is None
            else f"{version.original_local_time} · {version.timezone_name} "
            f"(UTC{_offset_label(version.utc_offset_minutes)})"
        )
        if commitment.source_kind.value == "manual":
            self.source.setText("手动创建")
        elif commitment.source_deleted:
            self.source.setText("聊天来源已删除；提醒独立保留")
        else:
            self.source.setText("聊天命令，可跳转到原消息")
        self._sync_controls()

    def _current_spec(self) -> TemporalDraftSpec:
        qvalue = self.due.dateTime()
        local_due = datetime(
            qvalue.date().year(),
            qvalue.date().month(),
            qvalue.date().day(),
            qvalue.time().hour(),
            qvalue.time().minute(),
        ).astimezone()
        offset = local_due.utcoffset()
        return TemporalDraftSpec(
            TemporalCommitmentKind(str(self.kind.currentData())),
            self.content.toPlainText().strip(),
            local_due.astimezone(UTC),
            local_due.isoformat(timespec="minutes"),
            local_due.tzname() or "local",
            None if offset is None else round(offset.total_seconds() / 60),
            self.show_content.isChecked(),
        )

    def _save(self) -> None:
        if self._selected_id is None:
            return
        if not self.content.toPlainText().strip():
            self.set_status("提醒内容不能为空。", error=True)
            return
        if len(self.content.toPlainText().strip()) > 500:
            self.set_status("提醒内容不能超过 500 字。", error=True)
            return
        due = self._current_spec().due_at_utc
        if due is None or due <= datetime.now(UTC):
            self.set_status("请选择未来时间。", error=True)
            return
        self.save_requested.emit(self._selected_id, self._current_spec())

    def _complete(self) -> None:
        if self._selected_id is not None:
            self.complete_requested.emit(self._selected_id)

    def _snooze(self) -> None:
        if self._selected_id is not None:
            self.snooze_requested.emit(self._selected_id, 10)

    def _cancel(self) -> None:
        if self._selected_id is not None:
            self.cancel_requested.emit(self._selected_id)

    def _delete(self) -> None:
        if self._selected_id is not None:
            self.delete_requested.emit(self._selected_id)

    def _open_source(self) -> None:
        commitment = self._rows.get(self._selected_id or "")
        if (
            commitment is not None
            and commitment.live_source_conversation_id
            and commitment.live_source_message_id
        ):
            self.source_requested.emit(
                commitment.live_source_conversation_id,
                commitment.live_source_message_id,
            )

    def _clear_detail(self) -> None:
        self.content.clear()
        self.original_time.setText("—")
        self.source.setText("—")
        self._sync_controls()

    def _sync_controls(self) -> None:
        commitment = self._rows.get(self._selected_id or "")
        present = commitment is not None
        terminal = bool(
            commitment
            and commitment.status
            in {TemporalCommitmentStatus.COMPLETED, TemporalCommitmentStatus.CANCELLED}
        )
        editable = self._writable and present and not terminal
        for widget in (self.kind, self.content, self.due, self.show_content):
            widget.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.save_button.setText(
            "确认并安排"
            if commitment is None or commitment.status is TemporalCommitmentStatus.DRAFT
            else "保存并重新安排"
        )
        self.complete_button.setEnabled(
            bool(
                editable
                and commitment
                and commitment.status
                in {
                    TemporalCommitmentStatus.SCHEDULED,
                    TemporalCommitmentStatus.DUE,
                    TemporalCommitmentStatus.SURFACED,
                }
            )
        )
        self.cancel_button.setEnabled(editable)
        self.snooze_button.setEnabled(
            bool(
                editable
                and commitment
                and commitment.status
                in {
                    TemporalCommitmentStatus.SCHEDULED,
                    TemporalCommitmentStatus.DUE,
                    TemporalCommitmentStatus.SURFACED,
                }
            )
        )
        self.delete_button.setEnabled(self._writable and present)
        self.source_button.setEnabled(
            bool(
                commitment
                and commitment.live_source_conversation_id
                and commitment.live_source_message_id
            )
        )


def _offset_label(value: int | None) -> str:
    if value is None:
        return "?"
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"
