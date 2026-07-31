"""Single-instance coordination over Qt local IPC."""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

ACTIVATE_COMMAND = b"activate\n"
DEFAULT_SERVER_NAME = "amadeus-desktop-mvp-v1"


class SingleInstanceError(RuntimeError):
    """Raised when the application cannot safely determine instance ownership."""


class SingleInstance(QObject):
    """Own a local server or notify the process that already owns it."""

    activation_requested = Signal()

    def __init__(self, server_name: str = DEFAULT_SERVER_NAME) -> None:
        super().__init__()
        self.server_name = server_name
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._buffers: dict[QLocalSocket, bytearray] = {}
        self._is_primary = False

    @property
    def is_primary(self) -> bool:
        return self._is_primary

    def acquire(self) -> bool:
        """Return true for the primary instance; notify and return false otherwise."""

        if self._notify_existing():
            return False
        if self._server.listen(self.server_name):
            self._is_primary = True
            return True

        # A peer may have won the startup race after our first connection attempt.
        if self._notify_existing(timeout_ms=1000):
            return False

        # Windows named pipes disappear with their owning process. On Unix only,
        # remove a stale socket path after two failed connection attempts.
        if os.name != "nt":
            QLocalServer.removeServer(self.server_name)
            if self._server.listen(self.server_name):
                self._is_primary = True
                return True

        raise SingleInstanceError("Unable to acquire or contact the local instance endpoint.")

    def close(self) -> None:
        for socket in list(self._buffers):
            socket.abort()
        self._buffers.clear()
        if self._server.isListening():
            self._server.close()
        if self._is_primary:
            QLocalServer.removeServer(self.server_name)
        self._is_primary = False

    def _notify_existing(self, timeout_ms: int = 300) -> bool:
        socket = QLocalSocket(self)
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(timeout_ms):
            socket.abort()
            socket.deleteLater()
            return False
        socket.write(ACTIVATE_COMMAND)
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        socket.deleteLater()
        return True

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                continue
            self._buffers[socket] = bytearray()
            socket.readyRead.connect(lambda current=socket: self._read_socket(current))
            socket.disconnected.connect(lambda current=socket: self._discard_socket(current))
            self._read_socket(socket)

    def _read_socket(self, socket: QLocalSocket) -> None:
        if socket not in self._buffers:
            return
        self._buffers[socket].extend(bytes(socket.readAll()))
        buffer = self._buffers[socket]
        while b"\n" in buffer:
            command, _, remainder = buffer.partition(b"\n")
            self._buffers[socket] = bytearray(remainder)
            buffer = self._buffers[socket]
            if command == ACTIVATE_COMMAND.rstrip(b"\n"):
                self.activation_requested.emit()

    def _discard_socket(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()
