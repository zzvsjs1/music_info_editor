"""Read the Windows filesystem change timestamp from an already opened file."""

import ctypes
import msvcrt
from ctypes import wintypes


class _FileBasicInfo(ctypes.Structure):
    _fields_ = (
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("file_attributes", wintypes.DWORD),
    )


_FILE_BASIC_INFO = 0
_WINDOWS_TO_UNIX_EPOCH_TICKS = 116_444_736_000_000_000
_get_file_information = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
_get_file_information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_get_file_information.restype = wintypes.BOOL


def change_time_ns(descriptor: int) -> int:
    """Return FILE_BASIC_INFO.ChangeTime in nanoseconds since the Unix epoch."""
    info = _FileBasicInfo()
    handle = msvcrt.get_osfhandle(descriptor)
    if not _get_file_information(handle, _FILE_BASIC_INFO, ctypes.byref(info), ctypes.sizeof(info)):
        raise ctypes.WinError(ctypes.get_last_error())

    return (int(info.change_time) - _WINDOWS_TO_UNIX_EPOCH_TICKS) * 100
