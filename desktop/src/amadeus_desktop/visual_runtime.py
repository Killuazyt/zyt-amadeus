"""Explicit Qt/Win32/camera visual capture with one 1 FPS latest-frame slot."""

from __future__ import annotations

import ctypes
import io
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass

from PIL import Image
from PySide6.QtCore import (
    QBuffer,
    QByteArray,
    QCameraPermission,
    QCoreApplication,
    QIODevice,
    QObject,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QImage, QScreen
from PySide6.QtMultimedia import (
    QCamera,
    QCameraDevice,
    QMediaCaptureSession,
    QMediaDevices,
    QVideoSink,
)

from amadeus_desktop.visual import (
    LatestFrameSlot,
    VisualFrame,
    VisualSourceKind,
    visual_timestamp,
)

MAX_VISUAL_EDGE = 2_048


@dataclass(frozen=True, slots=True)
class WindowInfo:
    window_id: str
    title: str


@dataclass(frozen=True, slots=True)
class CameraInfo:
    camera_id: str
    description: str
    is_default: bool


class _LatestImageEncoder(QObject):
    encoded = Signal(object)
    failed = Signal(str)

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="visual-png")
        self._lock = threading.RLock()
        self._future: Future[bytes] | None = None
        self._pending: tuple[QImage, tuple[object, ...], int] | None = None
        self._generation = 0
        self._closed = False

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._future is not None or self._pending is not None

    def submit(self, image: QImage, metadata: tuple[object, ...]) -> None:
        if image.isNull():
            self.failed.emit("视觉来源返回了空画面。")
            return
        with self._lock:
            if self._closed:
                return
            item = (image.copy(), metadata, self._generation)
            if self._future is not None:
                self._pending = item
                return
        self._start(item)

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._pending = None
            future = self._future
        if future is not None:
            future.cancel()

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _start(self, item: tuple[QImage, tuple[object, ...], int]) -> None:
        image, metadata, generation = item
        future = self._executor.submit(_encode_qimage_png, image)
        with self._lock:
            if self._closed:
                future.cancel()
                return
            self._future = future
        future.add_done_callback(lambda completed: self._complete(completed, metadata, generation))

    def _complete(
        self,
        future: Future[bytes],
        metadata: tuple[object, ...],
        generation: int,
    ) -> None:
        with self._lock:
            if self._future is future:
                self._future = None
            relevant = generation == self._generation and not self._closed
            pending = self._pending
            self._pending = None
        if relevant and not future.cancelled():
            try:
                payload = future.result()
            except Exception:
                self.failed.emit("画面无法安全编码。")
            else:
                self.encoded.emit((*metadata, payload))
        if pending is not None:
            self._start(pending)


class ScreenCaptureSource(QObject):
    kind = VisualSourceKind.SCREEN
    frame_ready = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool, str)

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._screen: QScreen | None = None
        self._source_id = ""
        self._source_name = ""
        self._sequence = 0
        self._generation = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1_000)
        self._timer.timeout.connect(self._capture)
        self._encoder = _LatestImageEncoder(parent=self)
        self._encoder.encoded.connect(self._on_encoded)
        self._encoder.failed.connect(self.failed.emit)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    @property
    def source_name(self) -> str:
        return self._source_name

    def start(self, source_id: str = "") -> bool:
        screens = QGuiApplication.screens()
        screen = next((item for item in screens if item.name() == source_id), None)
        if screen is None:
            screen = QGuiApplication.primaryScreen() if not source_id else None
        if screen is None:
            self.failed.emit("所选屏幕不可用。")
            return False
        self.stop()
        self._generation += 1
        self._screen = screen
        self._source_id = screen.name()
        self._source_name = f"屏幕：{screen.name()}"
        self._timer.start()
        self.active_changed.emit(True, self._source_name)
        self._capture()
        return True

    def stop(self) -> None:
        was_active = self.active
        self._timer.stop()
        self._generation += 1
        self._encoder.cancel()
        self._screen = None
        self._source_id = ""
        self._source_name = ""
        if was_active:
            self.active_changed.emit(False, "")

    def shutdown(self) -> None:
        self.stop()
        self._encoder.shutdown()

    def _capture(self) -> None:
        screen = self._screen
        if screen is None:
            return
        pixmap = screen.grabWindow(0)
        if pixmap.isNull():
            self.failed.emit("屏幕当前无法捕获。")
            self.stop()
            return
        self._sequence += 1
        self._encoder.submit(
            pixmap.toImage(),
            (
                self._generation,
                self._source_id,
                self._source_name,
                self._sequence,
            ),
        )

    def _on_encoded(self, result: object) -> None:
        try:
            generation, source_id, source_name, sequence, payload = tuple(result)
        except (TypeError, ValueError):
            return
        if generation != self._generation or not self.active or not isinstance(payload, bytes):
            return
        self.frame_ready.emit(
            VisualFrame(
                VisualSourceKind.SCREEN,
                str(source_id),
                str(source_name),
                payload,
                visual_timestamp(),
                int(sequence),
            )
        )


class WindowCaptureSource(QObject):
    kind = VisualSourceKind.WINDOW
    frame_ready = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool, str)
    _captured = Signal(object)

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="visual-window")
        self._timer = QTimer(self)
        self._timer.setInterval(1_000)
        self._timer.timeout.connect(self._capture)
        self._future: Future[bytes] | None = None
        self._generation = 0
        self._sequence = 0
        self._window_id = ""
        self._source_name = ""
        self._captured.connect(self._on_captured)
        self._fallback_encoder = _LatestImageEncoder(parent=self)
        self._fallback_encoder.encoded.connect(self._on_fallback_encoded)
        self._fallback_encoder.failed.connect(self.failed.emit)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    @property
    def source_name(self) -> str:
        return self._source_name

    @staticmethod
    def windows() -> tuple[WindowInfo, ...]:
        return enumerate_windows()

    def start(self, source_id: str = "") -> bool:
        windows = {item.window_id: item for item in enumerate_windows()}
        selected = windows.get(str(source_id))
        if selected is None:
            self.failed.emit("所选窗口不存在、已最小化或不可捕获。")
            return False
        self.stop()
        self._generation += 1
        self._window_id = selected.window_id
        self._source_name = f"窗口：{selected.title}"
        self._timer.start()
        self.active_changed.emit(True, self._source_name)
        self._capture()
        return True

    def stop(self) -> None:
        was_active = self.active
        self._timer.stop()
        self._generation += 1
        future = self._future
        self._future = None
        if future is not None:
            future.cancel()
        self._fallback_encoder.cancel()
        self._window_id = ""
        self._source_name = ""
        if was_active:
            self.active_changed.emit(False, "")

    def shutdown(self) -> None:
        self.stop()
        self._fallback_encoder.shutdown()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _capture(self) -> None:
        if not self.active or self._future is not None:
            return
        generation = self._generation
        sequence = self._sequence + 1
        window_id = self._window_id
        future = self._executor.submit(_capture_window_png, int(window_id))
        self._future = future

        def completed(value: Future[bytes]) -> None:
            try:
                payload: object = value.result()
            except _WindowCaptureError as exc:
                payload = exc.code
            except Exception:
                payload = "unavailable"
            self._captured.emit((generation, sequence, payload))

        future.add_done_callback(completed)

    def _on_captured(self, result: object) -> None:
        self._future = None
        try:
            generation, sequence, payload = tuple(result)
        except (TypeError, ValueError):
            return
        if generation != self._generation or not self.active:
            return
        if payload == "fallback":
            self._visible_fallback(int(sequence))
            return
        if not isinstance(payload, bytes):
            self.failed.emit("窗口已最小化、受保护或无法捕获。")
            self.stop()
            return
        self._sequence = int(sequence)
        self.frame_ready.emit(
            VisualFrame(
                VisualSourceKind.WINDOW,
                self._window_id,
                self._source_name,
                payload,
                visual_timestamp(),
                self._sequence,
            )
        )

    def _visible_fallback(self, sequence: int) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.failed.emit("窗口无法捕获。")
            self.stop()
            return
        pixmap = screen.grabWindow(int(self._window_id))
        if pixmap.isNull():
            self.failed.emit("窗口已最小化、受保护或不可见。")
            self.stop()
            return
        self._fallback_encoder.submit(
            pixmap.toImage(),
            (
                self._generation,
                self._window_id,
                self._source_name,
                sequence,
            ),
        )

    def _on_fallback_encoded(self, result: object) -> None:
        try:
            generation, window_id, source_name, sequence, payload = tuple(result)
        except (TypeError, ValueError):
            return
        if generation != self._generation or not self.active or not isinstance(payload, bytes):
            return
        self._sequence = int(sequence)
        self.frame_ready.emit(
            VisualFrame(
                VisualSourceKind.WINDOW,
                str(window_id),
                str(source_name),
                payload,
                visual_timestamp(),
                int(sequence),
            )
        )


class CameraCaptureSource(QObject):
    kind = VisualSourceKind.CAMERA
    frame_ready = Signal(object)
    failed = Signal(str)
    active_changed = Signal(bool, str)

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._camera: QCamera | None = None
        self._session: QMediaCaptureSession | None = None
        self._sink: QVideoSink | None = None
        self._camera_id = ""
        self._source_name = ""
        self._sequence = 0
        self._generation = 0
        self._last_frame_at = 0.0
        self._pending_camera_id = ""
        self._permission_pending = False
        self._encoder = _LatestImageEncoder(parent=self)
        self._encoder.encoded.connect(self._on_encoded)
        self._encoder.failed.connect(self.failed.emit)

    @property
    def active(self) -> bool:
        return self._camera is not None and self._camera.isActive()

    @property
    def source_name(self) -> str:
        return self._source_name

    @staticmethod
    def cameras() -> tuple[CameraInfo, ...]:
        default_id = _camera_id(QMediaDevices.defaultVideoInput())
        return tuple(
            CameraInfo(
                _camera_id(device),
                device.description(),
                _camera_id(device) == default_id,
            )
            for device in QMediaDevices.videoInputs()
        )

    def start(self, source_id: str = "") -> bool:
        if self.active or self._permission_pending:
            return False
        application = QCoreApplication.instance()
        if application is None:
            self.failed.emit("相机运行环境不可用。")
            return False
        permission = QCameraPermission()
        status = application.checkPermission(permission)
        if status is Qt.PermissionStatus.Denied:
            self.failed.emit("没有相机权限。")
            return False
        if status is Qt.PermissionStatus.Undetermined:
            self._permission_pending = True
            self._pending_camera_id = source_id
            self._source_name = "相机：等待权限确认"
            application.requestPermission(permission, self, self._permission_result)
            return True
        return self._start_granted(source_id)

    def stop(self) -> None:
        was_active = self.active
        self._permission_pending = False
        self._pending_camera_id = ""
        self._generation += 1
        self._encoder.cancel()
        camera = self._camera
        self._camera = None
        self._session = None
        self._sink = None
        self._camera_id = ""
        self._source_name = ""
        if camera is not None:
            camera.stop()
            camera.deleteLater()
        if was_active:
            self.active_changed.emit(False, "")

    def shutdown(self) -> None:
        self.stop()
        self._encoder.shutdown()

    def _permission_result(self, permission: QCameraPermission) -> None:
        self._permission_pending = False
        application = QCoreApplication.instance()
        status = (
            Qt.PermissionStatus.Denied
            if application is None
            else application.checkPermission(permission)
        )
        source_id = self._pending_camera_id
        self._pending_camera_id = ""
        if status is not Qt.PermissionStatus.Granted:
            self.failed.emit("没有相机权限。")
            return
        self._start_granted(source_id)

    def _start_granted(self, source_id: str) -> bool:
        devices = QMediaDevices.videoInputs()
        device = next((item for item in devices if _camera_id(item) == source_id), None)
        if device is None:
            device = QMediaDevices.defaultVideoInput() if not source_id else None
        if device is None or device.isNull():
            self.failed.emit("所选相机不可用。")
            return False
        self.stop()
        self._generation += 1
        camera = QCamera(device, self)
        session = QMediaCaptureSession(self)
        sink = QVideoSink(self)
        session.setCamera(camera)
        session.setVideoSink(sink)
        sink.videoFrameChanged.connect(self._on_video_frame)
        camera.errorOccurred.connect(
            lambda _error, _message: self.failed.emit("相机不可用或已断开。")
        )
        self._camera = camera
        self._session = session
        self._sink = sink
        self._camera_id = _camera_id(device)
        self._source_name = f"相机：{device.description()}"
        self._last_frame_at = 0.0
        camera.start()
        self.active_changed.emit(True, self._source_name)
        return True

    def _on_video_frame(self, frame) -> None:
        now = time.monotonic()
        if not self.active or now - self._last_frame_at < 1.0:
            return
        image = frame.toImage()
        if image.isNull():
            return
        self._last_frame_at = now
        self._sequence += 1
        self._encoder.submit(
            image,
            (
                self._generation,
                self._camera_id,
                self._source_name,
                self._sequence,
            ),
        )

    def _on_encoded(self, result: object) -> None:
        try:
            generation, source_id, source_name, sequence, payload = tuple(result)
        except (TypeError, ValueError):
            return
        if generation != self._generation or not self.active or not isinstance(payload, bytes):
            return
        self.frame_ready.emit(
            VisualFrame(
                VisualSourceKind.CAMERA,
                str(source_id),
                str(source_name),
                payload,
                visual_timestamp(),
                int(sequence),
            )
        )


class VisualSourceManager(QObject):
    """Allow exactly one live source and expose its capacity-one latest frame."""

    state_changed = Signal(bool, str, str)
    frame_updated = Signal(object)
    failed = Signal(str)

    def __init__(self, *, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.slot = LatestFrameSlot()
        self.screen = ScreenCaptureSource(parent=self)
        self.window = WindowCaptureSource(parent=self)
        self.camera = CameraCaptureSource(parent=self)
        self._active_kind: VisualSourceKind | None = None
        for source in (self.screen, self.window, self.camera):
            source.frame_ready.connect(self._on_frame)
            source.failed.connect(self._on_failure)
            source.active_changed.connect(
                lambda active, name, kind=source.kind: self._on_source_active(
                    kind,
                    active,
                    name,
                )
            )

    @property
    def active_kind(self) -> VisualSourceKind | None:
        return self._active_kind

    @property
    def active(self) -> bool:
        return self._active_kind is not None

    def start(self, kind: VisualSourceKind | str, source_id: str = "") -> bool:
        selected_kind = VisualSourceKind(kind)
        self.stop()
        source = self._source(selected_kind)
        if not source.start(source_id):
            self.slot.clear()
            return False
        self._active_kind = selected_kind
        self.state_changed.emit(True, selected_kind.value, source.source_name)
        return True

    def stop(self) -> None:
        previous = self._active_kind
        for source in (self.screen, self.window, self.camera):
            source.stop()
        self._active_kind = None
        self.slot.clear()
        if previous is not None:
            self.state_changed.emit(False, "", "")

    def privacy_stop(self) -> None:
        self.stop()

    def shutdown(self) -> None:
        self.stop()
        self.screen.shutdown()
        self.window.shutdown()
        self.camera.shutdown()

    def latest(self) -> VisualFrame | None:
        return self.slot.snapshot()[1]

    def _source(self, kind: VisualSourceKind):
        return {
            VisualSourceKind.SCREEN: self.screen,
            VisualSourceKind.WINDOW: self.window,
            VisualSourceKind.CAMERA: self.camera,
        }[kind]

    def _on_frame(self, frame_object: object) -> None:
        if not isinstance(frame_object, VisualFrame):
            return
        if frame_object.source_kind is not self._active_kind:
            return
        self.slot.put(frame_object)
        self.frame_updated.emit(frame_object)

    def _on_failure(self, message: str) -> None:
        self.failed.emit(message)
        self.stop()

    def _on_source_active(
        self,
        kind: VisualSourceKind,
        active: bool,
        source_name: str,
    ) -> None:
        if active and kind is self._active_kind:
            self.state_changed.emit(True, kind.value, source_name)


class _WindowCaptureError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def enumerate_windows() -> tuple[WindowInfo, ...]:
    if os.name != "nt":
        return ()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    results: list[WindowInfo] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int

    @callback_type
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0 or length > 512:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            results.append(WindowInfo(str(int(hwnd)), title))
        return True

    if not user32.EnumWindows(callback, 0):
        return ()
    return tuple(results)


def _capture_window_png(hwnd: int) -> bytes:
    if os.name != "nt":
        raise _WindowCaptureError("unavailable")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", ctypes.c_uint32),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", ctypes.c_uint16),
            ("biBitCount", ctypes.c_uint16),
            ("biCompression", ctypes.c_uint32),
            ("biSizeImage", ctypes.c_uint32),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", ctypes.c_uint32),
            ("biClrImportant", ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = (wintypes.HWND,)
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = (wintypes.HWND,)
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = (
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    )
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = (wintypes.HDC,)
    gdi32.DeleteDC.restype = wintypes.BOOL

    if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
        raise _WindowCaptureError("unavailable")
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise _WindowCaptureError("unavailable")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0 or width * height > 80_000_000:
        raise _WindowCaptureError("unavailable")
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc) if window_dc else 0
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height) if memory_dc else 0
    previous = gdi32.SelectObject(memory_dc, bitmap) if bitmap else 0
    try:
        if not window_dc or not memory_dc or not bitmap:
            raise _WindowCaptureError("fallback")
        if not user32.PrintWindow(hwnd, memory_dc, 2):
            raise _WindowCaptureError("fallback")
        header = BITMAPINFOHEADER(
            ctypes.sizeof(BITMAPINFOHEADER),
            width,
            -height,
            1,
            32,
            0,
            width * height * 4,
            0,
            0,
            0,
            0,
        )
        info = BITMAPINFO(header)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            buffer,
            ctypes.byref(info),
            0,
        ):
            raise _WindowCaptureError("fallback")
        image = Image.frombuffer(
            "RGBA",
            (width, height),
            buffer.raw,
            "raw",
            "BGRA",
            0,
            1,
        ).convert("RGB")
        extrema = image.getextrema()
        if all(low == high == 0 for low, high in extrema):
            raise _WindowCaptureError("unavailable")
        image.thumbnail((MAX_VISUAL_EDGE, MAX_VISUAL_EDGE), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        if previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        if window_dc:
            user32.ReleaseDC(hwnd, window_dc)


def _encode_qimage_png(image: QImage) -> bytes:
    clean = image.convertToFormat(QImage.Format.Format_RGB888)
    if max(clean.width(), clean.height()) > MAX_VISUAL_EDGE:
        clean = clean.scaled(
            MAX_VISUAL_EDGE,
            MAX_VISUAL_EDGE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    payload = QByteArray()
    buffer = QBuffer(payload)
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise ValueError("PNG buffer unavailable")
    try:
        if not clean.save(buffer, "PNG"):
            raise ValueError("PNG encoding failed")
    finally:
        buffer.close()
    result = bytes(payload)
    if not result.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("PNG encoding failed")
    return result


def _camera_id(device: QCameraDevice) -> str:
    return bytes(device.id()).hex()
