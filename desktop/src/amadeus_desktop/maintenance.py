"""Early, non-UI maintenance entry points reserved for the packaged installer."""

from __future__ import annotations

import ctypes
import os
import stat
import struct
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from amadeus_desktop.autostart import AutostartError, AutostartManager
from amadeus_desktop.credential_store import (
    CredentialStore,
    CredentialStoreError,
    delete_all_amadeus_credentials,
    enumerate_amadeus_profile_targets,
)
from amadeus_desktop.data_management import (
    DataManagementError,
    FactoryResetPlan,
    plan_factory_reset,
)
from amadeus_desktop.installation_mutex import (
    InstallationMutexError,
    is_installation_mutex_present,
)
from amadeus_desktop.paths import AppPaths

UNINSTALL_DELETE_DATA_ARGUMENT = "--uninstall-cleanup=delete-data"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_READONLY = 0x01
_FILE_ATTRIBUTE_NORMAL = 0x80

_DELETE = 0x00010000
_FILE_LIST_DIRECTORY = 0x0001
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_WRITE_ATTRIBUTES = 0x0100
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_BASIC_INFO_CLASS = 0
_FILE_DISPOSITION_INFO_CLASS = 4
_FILE_ID_INFO_CLASS = 18
_FILE_ID_EXTD_DIRECTORY_INFO_CLASS = 19
_FILE_ID_EXTD_DIRECTORY_RESTART_INFO_CLASS = 20
_ERROR_NO_MORE_FILES = 18
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_DIRECTORY_BUFFER_SIZE = 64 * 1024
_FILE_ID_EXTD_NAME_OFFSET = 88


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


class _FileBasicInformation(ctypes.Structure):
    _fields_ = [
        ("creation_time", ctypes.c_longlong),
        ("last_access_time", ctypes.c_longlong),
        ("last_write_time", ctypes.c_longlong),
        ("change_time", ctypes.c_longlong),
        ("file_attributes", wintypes.DWORD),
    ]


class _FileDispositionInformation(ctypes.Structure):
    _fields_ = [("delete_file", ctypes.c_ubyte)]


class _FileId128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FileIdInformation(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", _FileId128),
    ]


@dataclass(frozen=True)
class _WindowsFileIdentity:
    volume_serial_number: int
    file_id: bytes
    attributes: int


@dataclass(frozen=True)
class _WindowsDirectoryEntry:
    name: str
    file_id: bytes
    attributes: int


_KERNEL32: object | None = None


class MaintenanceError(RuntimeError):
    """Privacy-safe failure from a restricted installer maintenance action."""


def delete_all_local_data_for_uninstall(
    *,
    paths: AppPaths | None = None,
    autostart_manager: AutostartManager | None = None,
    credential_store: CredentialStore | None = None,
    remove_tree: Callable[[Path], None] | None = None,
    mutex_present: Callable[[], bool] = is_installation_mutex_present,
) -> None:
    """Delete only the P6 seven-region allowlist plus fixed HKCU/WinCred state.

    This function intentionally does not initialize paths, Qt, or logging.  It
    revalidates the complete allowlist immediately before each filesystem
    deletion and stops on the first unsafe or unverifiable condition.
    """

    try:
        if mutex_present():
            raise MaintenanceError("Amadeus is still running.")
    except InstallationMutexError:
        raise MaintenanceError("Application lifecycle state could not be verified.") from None

    selected_paths = paths or AppPaths.for_trusted_current_user()
    anchor_handles: list[int] = []
    filesystem_root_anchored = True
    if os.name == "nt":
        try:
            anchor_handles, filesystem_root_anchored = _open_windows_path_anchors(
                selected_paths.root
            )
        except OSError:
            raise MaintenanceError("Uninstall data path anchors could not be secured.") from None
    try:
        _delete_validated_local_data(
            selected_paths,
            autostart_manager=autostart_manager,
            credential_store=credential_store,
            remove_tree=remove_tree,
            mutex_present=mutex_present,
            filesystem_root_anchored=filesystem_root_anchored,
        )
    finally:
        for handle in reversed(anchor_handles):
            _close_windows_handle(handle)


def _delete_validated_local_data(
    selected_paths: AppPaths,
    *,
    autostart_manager: AutostartManager | None,
    credential_store: CredentialStore | None,
    remove_tree: Callable[[Path], None] | None,
    mutex_present: Callable[[], bool],
    filesystem_root_anchored: bool,
) -> None:
    try:
        expected_plan = plan_factory_reset(selected_paths)
        _require_unchanged_plan(selected_paths, expected_plan)
        if filesystem_root_anchored:
            windows_snapshots = _preflight_regions(expected_plan)
        else:
            _require_path_absent(selected_paths.root)
            windows_snapshots = {}
    except DataManagementError:
        raise MaintenanceError("Uninstall data paths failed safety validation.") from None

    try:
        if mutex_present():
            raise MaintenanceError("Amadeus started during uninstall cleanup validation.")
    except InstallationMutexError:
        raise MaintenanceError("Application lifecycle state could not be verified.") from None

    try:
        selected_autostart = autostart_manager or AutostartManager()
        selected_autostart.set_enabled(False)
        if selected_autostart.registered_command() is not None:
            raise AutostartError("Launch-at-login removal could not be verified.")
    except AutostartError:
        raise MaintenanceError("Launch-at-login state could not be removed safely.") from None

    try:
        if credential_store is not None:
            credential_store.delete_secret()
            if credential_store.has_secret():
                raise CredentialStoreError("Credential removal could not be verified.")
        else:
            delete_all_amadeus_credentials()
            if enumerate_amadeus_profile_targets():
                raise CredentialStoreError("Dynamic credential removal could not be verified.")
    except CredentialStoreError:
        raise MaintenanceError("Credential state could not be removed safely.") from None

    if not filesystem_root_anchored:
        _require_path_absent(selected_paths.root)
        return

    for target in expected_plan.targets:
        if remove_tree is not None:
            selected_remove_tree = remove_tree
        elif os.name == "nt":
            snapshot = windows_snapshots.get(target, {})

            def selected_remove_tree(
                path: Path,
                expected: dict[tuple[str, ...], _WindowsFileIdentity] = snapshot,
            ) -> None:
                _remove_windows_tree(path, expected_snapshot=expected)

        else:
            selected_remove_tree = _remove_tree_without_reparse
        _delete_verified_region(
            selected_paths,
            expected_plan,
            target,
            remove_tree=selected_remove_tree,
        )


def _require_unchanged_plan(paths: AppPaths, expected: FactoryResetPlan) -> None:
    if plan_factory_reset(paths) != expected:
        raise MaintenanceError("Uninstall data paths changed during validation.")


def _require_path_absent(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    except OSError:
        raise MaintenanceError("Uninstall data root absence could not be verified.") from None
    raise MaintenanceError("Uninstall data root appeared during cleanup validation.")


def _preflight_regions(
    plan: FactoryResetPlan,
) -> dict[Path, dict[tuple[str, ...], _WindowsFileIdentity]]:
    windows_snapshots: dict[Path, dict[tuple[str, ...], _WindowsFileIdentity]] = {}
    for target in plan.targets:
        try:
            if os.name == "nt":
                windows_snapshots[target] = _snapshot_windows_tree(target)
            else:
                _preflight_tree(target)
        except FileNotFoundError:
            continue
        except OSError:
            raise MaintenanceError("Uninstall data path metadata could not be verified.") from None
    return windows_snapshots


def _delete_verified_region(
    paths: AppPaths,
    expected: FactoryResetPlan,
    target: Path,
    *,
    remove_tree: Callable[[Path], None],
) -> None:
    try:
        _require_unchanged_plan(paths, expected)
        _preflight_tree(target)
    except FileNotFoundError:
        return
    except (DataManagementError, MaintenanceError, OSError):
        raise MaintenanceError("Uninstall data path metadata could not be verified.") from None
    try:
        remove_tree(target)
        os.lstat(target)
    except FileNotFoundError:
        return
    except OSError:
        raise MaintenanceError("Uninstall data target could not be removed safely.") from None
    raise MaintenanceError("Uninstall data target removal could not be verified.")


def _preflight_tree(path: Path) -> None:
    if os.name == "nt":
        _preflight_windows_tree(path)
        return

    status = os.lstat(path)
    if _status_is_reparse(status):
        raise MaintenanceError("Uninstall data tree contains a reparse point.")
    if not stat.S_ISDIR(status.st_mode):
        raise MaintenanceError("Uninstall data target is not a safe directory.")
    try:
        with os.scandir(path) as iterator:
            entries = list(iterator)
    except OSError:
        raise MaintenanceError("Uninstall data path metadata could not be verified.") from None
    for entry in entries:
        try:
            entry_status = entry.stat(follow_symlinks=False)
        except OSError:
            raise MaintenanceError("Uninstall data path metadata could not be verified.") from None
        if _status_is_reparse(entry_status) or stat.S_ISLNK(entry_status.st_mode):
            raise MaintenanceError("Uninstall data tree contains a reparse point.")
        if stat.S_ISDIR(entry_status.st_mode):
            _preflight_tree(Path(entry.path))


def _remove_tree_without_reparse(path: Path) -> None:
    """Delete a verified directory without ever intentionally following a reparse point."""

    if os.name == "nt":
        _remove_windows_tree(path)
        return

    _preflight_tree(path)
    _remove_verified_directory(path)


def _remove_verified_directory(path: Path) -> None:
    status = os.lstat(path)
    if _status_is_reparse(status):
        raise MaintenanceError("Uninstall data tree changed to a reparse point.")
    if not stat.S_ISDIR(status.st_mode):
        raise MaintenanceError("Uninstall data tree changed before deletion.")
    try:
        with os.scandir(path) as iterator:
            entries = list(iterator)
    except OSError:
        raise MaintenanceError("Uninstall data tree could not be inspected safely.") from None
    for entry in entries:
        child = Path(entry.path)
        try:
            entry_status = entry.stat(follow_symlinks=False)
        except OSError:
            raise MaintenanceError("Uninstall data tree changed before deletion.") from None
        if _status_is_reparse(entry_status) or stat.S_ISLNK(entry_status.st_mode):
            raise MaintenanceError("Uninstall data tree changed before deletion.")
        try:
            if stat.S_ISDIR(entry_status.st_mode):
                _remove_verified_directory(child)
            else:
                child.unlink()
        except OSError:
            raise MaintenanceError("Uninstall data tree could not be removed safely.") from None
    try:
        path.rmdir()
    except OSError:
        raise MaintenanceError("Uninstall data tree could not be removed safely.") from None


def _status_is_reparse(status: os.stat_result) -> bool:
    return bool(int(getattr(status, "st_file_attributes", 0)) & _FILE_ATTRIBUTE_REPARSE_POINT)


def _kernel32() -> object:
    global _KERNEL32
    if os.name != "nt":
        raise OSError("Win32 handle validation is unavailable on this platform.")
    if _KERNEL32 is not None:
        return _KERNEL32

    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    library.CreateFileW.restype = wintypes.HANDLE
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CloseHandle.restype = wintypes.BOOL
    library.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    library.GetFileInformationByHandle.restype = wintypes.BOOL
    library.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    library.GetFileInformationByHandleEx.restype = wintypes.BOOL
    library.SetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    library.SetFileInformationByHandle.restype = wintypes.BOOL
    _KERNEL32 = library
    return library


def _extended_windows_path(path: Path) -> str:
    absolute = os.path.abspath(os.fspath(path))
    if absolute.startswith("\\\\?\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _open_windows_handle(
    path: Path,
    *,
    directory_hint: bool,
    delete_access: bool = True,
) -> int:
    access = _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    if delete_access:
        access |= _DELETE | _FILE_WRITE_ATTRIBUTES
    if directory_hint:
        access |= _FILE_LIST_DIRECTORY
    handle = _kernel32().CreateFileW(
        _extended_windows_path(path),
        access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle in (None, _INVALID_HANDLE_VALUE):
        raise _windows_os_error("Win32 path handle could not be opened safely.")
    return int(handle)


def _open_windows_path_anchors(path: Path) -> tuple[list[int], bool]:
    """Hold every existing mutable ancestor so intermediate junction swaps fail."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    if not absolute.is_absolute() or not absolute.drive or absolute.drive.startswith("\\\\"):
        raise OSError("Uninstall data path anchor is not a local absolute path.")

    handles: list[int] = []
    complete = True
    current = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:]:
            current /= component
            try:
                try:
                    handle = _open_windows_handle(
                        current,
                        directory_hint=False,
                        delete_access=True,
                    )
                except OSError as error:
                    if error.errno != 5 or current == absolute:
                        raise
                    # Protected system ancestors such as C:\Users deny DELETE to
                    # this user. The same token therefore cannot rename them;
                    # retain a metadata handle and continue toward user-owned roots.
                    handle = _open_windows_handle(
                        current,
                        directory_hint=False,
                        delete_access=False,
                    )
            except FileNotFoundError:
                complete = False
                break
            try:
                identity = _windows_handle_identity(handle)
                if (
                    identity.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
                    or not identity.attributes & _FILE_ATTRIBUTE_DIRECTORY
                ):
                    raise MaintenanceError("Uninstall data path contains an unsafe anchor.")
            except BaseException:
                _close_windows_handle(handle)
                raise
            handles.append(handle)
    except BaseException:
        for handle in reversed(handles):
            _close_windows_handle(handle)
        raise
    return handles, complete


def _close_windows_handle(handle: int) -> None:
    _kernel32().CloseHandle(handle)


def _windows_handle_identity(handle: int) -> _WindowsFileIdentity:
    information = _ByHandleFileInformation()
    if not _kernel32().GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _windows_os_error("Win32 file identity could not be read.")
    identity = _FileIdInformation()
    if not _kernel32().GetFileInformationByHandleEx(
        handle,
        _FILE_ID_INFO_CLASS,
        ctypes.byref(identity),
        ctypes.sizeof(identity),
    ):
        raise _windows_os_error("Win32 extended file identity could not be read.")
    file_id = bytes(identity.file_id.identifier)
    if not any(file_id):
        raise OSError("Win32 file identity is unavailable.")
    return _WindowsFileIdentity(
        volume_serial_number=int(identity.volume_serial_number),
        file_id=file_id,
        attributes=int(information.file_attributes),
    )


def _enumerate_windows_directory(handle: int) -> list[_WindowsDirectoryEntry]:
    entries: list[_WindowsDirectoryEntry] = []
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(_DIRECTORY_BUFFER_SIZE)
        information_class = (
            _FILE_ID_EXTD_DIRECTORY_RESTART_INFO_CLASS
            if restart
            else _FILE_ID_EXTD_DIRECTORY_INFO_CLASS
        )
        ctypes.set_last_error(0)
        if not _kernel32().GetFileInformationByHandleEx(
            handle,
            information_class,
            buffer,
            len(buffer),
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NO_MORE_FILES:
                break
            raise _windows_os_error(
                "Win32 directory could not be enumerated safely.",
                error=error,
            )
        restart = False
        offset = 0
        while True:
            if offset + _FILE_ID_EXTD_NAME_OFFSET > len(buffer):
                raise OSError("Win32 directory metadata was malformed.")
            next_offset = struct.unpack_from("<I", buffer, offset)[0]
            attributes = struct.unpack_from("<I", buffer, offset + 56)[0]
            name_length = struct.unpack_from("<I", buffer, offset + 60)[0]
            file_id = ctypes.string_at(ctypes.addressof(buffer) + offset + 72, 16)
            name_end = offset + _FILE_ID_EXTD_NAME_OFFSET + name_length
            if name_length % 2 or name_end > len(buffer):
                raise OSError("Win32 directory name metadata was malformed.")
            name_bytes = ctypes.string_at(
                ctypes.addressof(buffer) + offset + _FILE_ID_EXTD_NAME_OFFSET,
                name_length,
            )
            name = name_bytes.decode("utf-16-le", errors="strict")
            if name not in {".", ".."}:
                if not name or not any(file_id) or "\\" in name or "/" in name or ":" in name:
                    raise OSError("Win32 directory entry metadata was unsafe.")
                entries.append(
                    _WindowsDirectoryEntry(
                        name=name,
                        file_id=file_id,
                        attributes=attributes,
                    )
                )
            if next_offset == 0:
                break
            if next_offset < _FILE_ID_EXTD_NAME_OFFSET or offset + next_offset >= len(buffer):
                raise OSError("Win32 directory metadata offset was malformed.")
            offset += next_offset
    return entries


def _windows_entry_is_reparse(entry: _WindowsDirectoryEntry) -> bool:
    return bool(entry.attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _require_matching_windows_entry(
    parent: _WindowsFileIdentity,
    entry: _WindowsDirectoryEntry,
    child: _WindowsFileIdentity,
) -> None:
    expected_directory = bool(entry.attributes & _FILE_ATTRIBUTE_DIRECTORY)
    actual_directory = bool(child.attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if (
        _windows_entry_is_reparse(entry)
        or child.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or child.volume_serial_number != parent.volume_serial_number
        or child.file_id != entry.file_id
        or actual_directory != expected_directory
    ):
        raise MaintenanceError("Uninstall data tree changed during handle validation.")


def _preflight_windows_tree(path: Path) -> None:
    _snapshot_windows_tree(path)


def _snapshot_windows_tree(
    path: Path,
) -> dict[tuple[str, ...], _WindowsFileIdentity]:
    snapshot: dict[tuple[str, ...], _WindowsFileIdentity] = {}
    handle = _open_windows_handle(path, directory_hint=True)
    try:
        identity = _windows_handle_identity(handle)
        if (
            identity.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not identity.attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise MaintenanceError("Uninstall data target is not a safe directory.")
        snapshot[()] = identity
        _preflight_open_windows_directory(
            path,
            handle,
            identity,
            relative=(),
            snapshot=snapshot,
        )
    finally:
        _close_windows_handle(handle)
    return snapshot


def _preflight_open_windows_directory(
    path: Path,
    handle: int,
    parent_identity: _WindowsFileIdentity,
    *,
    relative: tuple[str, ...],
    snapshot: dict[tuple[str, ...], _WindowsFileIdentity],
) -> None:
    entries = _enumerate_windows_directory(handle)
    for entry in entries:
        if _windows_entry_is_reparse(entry):
            raise MaintenanceError("Uninstall data tree contains a reparse point.")
        is_directory = bool(entry.attributes & _FILE_ATTRIBUTE_DIRECTORY)
        child_path = path / entry.name
        child_handle = _open_windows_handle(child_path, directory_hint=is_directory)
        try:
            child_identity = _windows_handle_identity(child_handle)
            _require_matching_windows_entry(parent_identity, entry, child_identity)
            child_relative = (*relative, entry.name)
            snapshot[child_relative] = child_identity
            if is_directory:
                _preflight_open_windows_directory(
                    child_path,
                    child_handle,
                    child_identity,
                    relative=child_relative,
                    snapshot=snapshot,
                )
        finally:
            _close_windows_handle(child_handle)
    if _enumerate_windows_directory(handle) != entries:
        raise MaintenanceError("Uninstall data tree changed during handle validation.")


def _remove_windows_tree(
    path: Path,
    *,
    expected_snapshot: dict[tuple[str, ...], _WindowsFileIdentity] | None = None,
) -> None:
    handle = _open_windows_handle(path, directory_hint=True)
    try:
        identity = _windows_handle_identity(handle)
        if (
            identity.attributes & _FILE_ATTRIBUTE_REPARSE_POINT
            or not identity.attributes & _FILE_ATTRIBUTE_DIRECTORY
        ):
            raise MaintenanceError("Uninstall data target is not a safe directory.")
        if expected_snapshot is not None and expected_snapshot.get(()) != identity:
            raise MaintenanceError("Uninstall data root changed after preflight validation.")
        _remove_open_windows_directory(
            path,
            handle,
            identity,
            relative=(),
            expected_snapshot=expected_snapshot,
        )
        _mark_windows_handle_for_deletion(handle, identity)
    finally:
        _close_windows_handle(handle)


def _remove_open_windows_directory(
    path: Path,
    handle: int,
    parent_identity: _WindowsFileIdentity,
    *,
    relative: tuple[str, ...],
    expected_snapshot: dict[tuple[str, ...], _WindowsFileIdentity] | None,
) -> None:
    entries = _enumerate_windows_directory(handle)
    if expected_snapshot is not None:
        expected_names = {
            key[-1]
            for key in expected_snapshot
            if len(key) == len(relative) + 1 and key[:-1] == relative
        }
        if {entry.name for entry in entries} != expected_names:
            raise MaintenanceError("Uninstall data tree changed after preflight validation.")
    for entry in entries:
        if _windows_entry_is_reparse(entry):
            raise MaintenanceError("Uninstall data tree contains a reparse point.")
        is_directory = bool(entry.attributes & _FILE_ATTRIBUTE_DIRECTORY)
        child_path = path / entry.name
        child_handle = _open_windows_handle(child_path, directory_hint=is_directory)
        try:
            child_identity = _windows_handle_identity(child_handle)
            _require_matching_windows_entry(parent_identity, entry, child_identity)
            child_relative = (*relative, entry.name)
            if (
                expected_snapshot is not None
                and expected_snapshot.get(child_relative) != child_identity
            ):
                raise MaintenanceError("Uninstall data tree changed after preflight validation.")
            if is_directory:
                _remove_open_windows_directory(
                    child_path,
                    child_handle,
                    child_identity,
                    relative=child_relative,
                    expected_snapshot=expected_snapshot,
                )
            _mark_windows_handle_for_deletion(child_handle, child_identity)
        finally:
            _close_windows_handle(child_handle)
    if _enumerate_windows_directory(handle):
        raise MaintenanceError("Uninstall data tree changed during handle deletion.")


def _mark_windows_handle_for_deletion(
    handle: int,
    identity: _WindowsFileIdentity,
) -> None:
    if identity.attributes & _FILE_ATTRIBUTE_READONLY:
        basic = _FileBasicInformation()
        if not _kernel32().GetFileInformationByHandleEx(
            handle,
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise _windows_os_error("Win32 file attributes could not be read.")
        basic.file_attributes &= ~_FILE_ATTRIBUTE_READONLY
        if basic.file_attributes == 0:
            basic.file_attributes = _FILE_ATTRIBUTE_NORMAL
        if not _kernel32().SetFileInformationByHandle(
            handle,
            _FILE_BASIC_INFO_CLASS,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise _windows_os_error("Win32 file attributes could not be updated.")

    disposition = _FileDispositionInformation(delete_file=1)
    if not _kernel32().SetFileInformationByHandle(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        raise _windows_os_error("Win32 file could not be deleted by handle.")


def _windows_os_error(message: str, *, error: int | None = None) -> OSError:
    code = ctypes.get_last_error() if error is None else error
    if code in {2, 3}:
        return FileNotFoundError(message)
    return OSError(code, message)
