"""User-facing desktop-pet package and playback settings."""

from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from amadeus_desktop.pet_assets import BUILTIN_PET_ID, LoadedPetAsset


class PetSettingsPage(QWidget):
    active_pet_changed = Signal(str)
    import_requested = Signal(str)
    remove_requested = Signal(str)
    scale_changed = Signal(int)
    animation_speed_changed = Signal(int)
    reset_position_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._assets: tuple[LoadedPetAsset, ...] = ()
        title = QLabel("桌宠")
        title.setStyleSheet("font-size: 18px; font-weight: 650;")
        self.pet_combo = QComboBox()
        self.pet_combo.setObjectName("activePetCombo")
        self.metadata = QLabel()
        self.metadata.setWordWrap(True)

        self.import_button = QToolButton()
        self.import_button.setText("导入…")
        self.import_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.import_button)
        self.import_directory_action = menu.addAction("导入 .codex-pet 目录…")
        self.import_archive_action = menu.addAction("导入 .codex-pet 压缩包…")
        self.import_button.setMenu(menu)
        self.remove_button = QPushButton("移除")
        asset_actions = QHBoxLayout()
        asset_actions.addWidget(self.import_button)
        asset_actions.addWidget(self.remove_button)
        asset_actions.addStretch(1)

        self.scale = QSpinBox()
        self.scale.setRange(50, 200)
        self.scale.setSuffix("%")
        self.speed = QSpinBox()
        self.speed.setRange(50, 200)
        self.speed.setSuffix("%")
        self.reset_position_button = QPushButton("恢复默认位置")

        form = QFormLayout()
        form.addRow("当前资源包", self.pet_combo)
        form.addRow("资源信息", self.metadata)
        form.addRow("", asset_actions)
        form.addRow("显示缩放", self.scale)
        form.addRow("动画速度", self.speed)
        form.addRow("", self.reset_position_button)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addStretch(1)

        self.pet_combo.currentIndexChanged.connect(self._pet_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.import_directory_action.triggered.connect(self._choose_directory)
        self.import_archive_action.triggered.connect(self._choose_archive)
        self.scale.valueChanged.connect(self.scale_changed.emit)
        self.speed.valueChanged.connect(self.animation_speed_changed.emit)
        self.reset_position_button.clicked.connect(self.reset_position_requested.emit)

    @property
    def active_pet_id(self) -> str:
        return str(self.pet_combo.currentData() or BUILTIN_PET_ID)

    def set_assets(self, assets: tuple[LoadedPetAsset, ...], active_pet_id: str) -> None:
        self._assets = assets
        with QSignalBlocker(self.pet_combo):
            self.pet_combo.clear()
            for asset in assets:
                self.pet_combo.addItem(asset.manifest.display_name, asset.manifest.pet_id)
            index = self.pet_combo.findData(active_pet_id)
            self.pet_combo.setCurrentIndex(max(0, index))
        self._sync_metadata()

    def apply_settings(self, *, scale_percent: int, animation_speed_percent: int) -> None:
        with QSignalBlocker(self.scale):
            self.scale.setValue(scale_percent)
        with QSignalBlocker(self.speed):
            self.speed.setValue(animation_speed_percent)

    def _pet_selected(self) -> None:
        self._sync_metadata()
        self.remove_button.setEnabled(self.active_pet_id != BUILTIN_PET_ID)
        self.active_pet_changed.emit(self.active_pet_id)

    def _remove_selected(self) -> None:
        if self.active_pet_id != BUILTIN_PET_ID:
            self.remove_requested.emit(self.active_pet_id)

    def _choose_directory(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 .codex-pet 目录")
        if path:
            self.import_requested.emit(path)

    def _choose_archive(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择桌宠压缩包",
            "",
            "Codex Pet (*.codex-pet *.codex-pet.zip *.zip)",
        )
        if path:
            self.import_requested.emit(path)

    def _sync_metadata(self) -> None:
        selected = next(
            (asset for asset in self._assets if asset.manifest.pet_id == self.active_pet_id),
            None,
        )
        if selected is None:
            self.metadata.setText("资源不可用，将使用内置默认宠物。")
        else:
            author = selected.manifest.author or "未声明"
            license_name = selected.manifest.license_name or "未声明"
            self.metadata.setText(f"作者：{author}；许可证：{license_name}")
        self.remove_button.setEnabled(self.active_pet_id != BUILTIN_PET_ID)
