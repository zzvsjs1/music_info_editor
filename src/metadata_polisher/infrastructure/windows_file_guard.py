"""Windows handles for protecting a published file and deleting a checked entry."""

import ctypes
import msvcrt
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from ctypes import wintypes
from pathlib import Path

_GENERIC_READ = 0x80000000
_FILE_WRITE_DATA = 0x0002
_DELETE = 0x00010000
_SHARE_READ = 1
_SHARE_WRITE = 2
_SHARE_DELETE = 4
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_create_file = _kernel.CreateFileW
_create_file.argtypes = (
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
)
_create_file.restype = wintypes.HANDLE
_set_file_information = _kernel.SetFileInformationByHandle
_set_file_information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_set_file_information.restype = wintypes.BOOL
_close_handle = _kernel.CloseHandle
_close_handle.argtypes = (wintypes.HANDLE,)
_close_handle.restype = wintypes.BOOL
_replace_file = _kernel.ReplaceFileW
_replace_file.argtypes = (
    wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
)
_replace_file.restype = wintypes.BOOL


def replace_with_recovery(temporary: Path, destination: Path, recovery: Path) -> None:
    """Retain the file actually superseded, including when the destination name raced."""
    if not _replace_file(str(destination), str(temporary), str(recovery), 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _open_file_handle(path: Path, access: int, share: int) -> Iterator[int]:
    # Open the directory entry itself. Following a symlink here could protect
    # an unrelated target and FILE_DISPOSITION_INFO would then delete that target.
    handle = _create_file(str(path), access, share, None, _OPEN_EXISTING, _FILE_FLAG_OPEN_REPARSE_POINT, None)
    if handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        _close_handle(handle)
        raise

    try:
        if os.fstat(descriptor).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError(f"Cannot protect the reparse point at {path}.")
        yield descriptor
    finally:
        # open_osfhandle transfers ownership to the CRT descriptor.
        os.close(descriptor)


def open_guard(path: Path, *, allow_rename: bool = False, for_deletion: bool = False) -> AbstractContextManager[int]:
    """Deny writers and optionally allow the transaction's own publication move."""
    access = _GENERIC_READ | (_DELETE if for_deletion else 0)
    share = _SHARE_READ | (_SHARE_DELETE if allow_rename else 0)
    return _open_file_handle(path, access, share)


def open_file_identity(path: Path) -> AbstractContextManager[int]:
    """Keep an ID alive across ReplaceFileW's exclusive open of the edited file."""
    return _open_file_handle(path, 0, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE)


def open_write_probe(path: Path) -> AbstractContextManager[int]:
    """Check the source DACL and sharing rules without truncating or writing the file."""
    return _open_file_handle(path, _FILE_WRITE_DATA, _SHARE_READ | _SHARE_WRITE | _SHARE_DELETE)


def delete_on_close(descriptor: int) -> None:
    """Apply FILE_DISPOSITION_INFO to the already checked object and directory entry."""
    # FILE_DISPOSITION_INFO contains one BOOLEAN (one byte, unlike Win32 BOOL).
    disposition = ctypes.c_ubyte(1)
    if not _set_file_information(
        msvcrt.get_osfhandle(descriptor), _FILE_DISPOSITION_INFO, ctypes.byref(disposition), ctypes.sizeof(disposition)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
