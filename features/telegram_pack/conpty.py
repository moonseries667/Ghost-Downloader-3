from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys
from contextlib import suppress

from app.models.task import TaskError

QR_PIXEL_MAP = {
    " ": (True, True),
    "\u2580": (False, True),
    "\u2584": (True, False),
    "\u2588": (False, False),
}

OSC_PATTERN = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CSI_PATTERN = re.compile(r"\x1b\[([0-9;?]*)([A-Za-z])")
TOKEN_PATTERN = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b[@-Z\\-_]")

PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
STILL_ACTIVE = 259


class TerminalScreen:
    """轻量 VT100 字符网格，准确还原 ConPTY 光标重绘后的二维码与提示行。"""

    def __init__(self, cols: int = 120, rows: int = 60) -> None:
        self.cols = cols
        self.rows = rows
        self._grid: list[list[str]] = [[" "] * cols for _ in range(rows)]
        self._row = 0
        self._col = 0
        self._pending = ""

    def feed(self, text: str) -> None:
        data = self._pending + text
        self._pending = ""
        index = 0
        length = len(data)
        while index < length:
            ch = data[index]
            if ch == "\x1b":
                match = TOKEN_PATTERN.match(data, index)
                if match is None:
                    # 末尾可能截断了不完整的转义序列，留待下一批字节拼接
                    if length - index < 32:
                        self._pending = data[index:]
                        break
                    index += 1
                    continue
                self._applyEscape(match.group(0))
                index = match.end()
            elif ch == "\r":
                self._col = 0
                index += 1
            elif ch == "\n":
                self._lineFeed()
                index += 1
            elif ch == "\b":
                self._col = max(0, self._col - 1)
                index += 1
            elif ord(ch) >= 32:
                if self._col >= self.cols:
                    self._col = 0
                    self._lineFeed()
                self._grid[self._row][self._col] = ch
                self._col += 1
                index += 1
            else:
                index += 1

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self._grid]

    def text(self) -> str:
        return "\n".join(line for line in self.lines() if line)

    def _lineFeed(self) -> None:
        if self._row + 1 < self.rows:
            self._row += 1
            return
        self._grid.pop(0)
        self._grid.append([" "] * self.cols)

    def _applyEscape(self, sequence: str) -> None:
        if sequence.startswith("\x1b]"):
            return
        match = CSI_PATTERN.fullmatch(sequence)
        if match is None:
            return
        rawParams, command = match.group(1), match.group(2)
        if rawParams.startswith("?"):
            return
        params = [int(p) if p.isdigit() else 0 for p in rawParams.split(";")] if rawParams else []
        first = params[0] if params else 0
        if command in ("H", "f"):
            row = (params[0] if len(params) >= 1 and params[0] > 0 else 1) - 1
            col = (params[1] if len(params) >= 2 and params[1] > 0 else 1) - 1
            self._row = min(self.rows - 1, max(0, row))
            self._col = min(self.cols - 1, max(0, col))
        elif command == "A":
            self._row = max(0, self._row - max(1, first))
        elif command == "B":
            self._row = min(self.rows - 1, self._row + max(1, first))
        elif command == "C":
            self._col = min(self.cols - 1, self._col + max(1, first))
        elif command == "D":
            self._col = max(0, self._col - max(1, first))
        elif command == "J":
            if first in (0, 2, 3):
                if first == 0:
                    for col in range(self._col, self.cols):
                        self._grid[self._row][col] = " "
                    for row in range(self._row + 1, self.rows):
                        self._grid[row] = [" "] * self.cols
                else:
                    self._grid = [[" "] * self.cols for _ in range(self.rows)]
        elif command == "K":
            if first == 0:
                for col in range(self._col, self.cols):
                    self._grid[self._row][col] = " "
            elif first == 1:
                for col in range(0, min(self._col + 1, self.cols)):
                    self._grid[self._row][col] = " "
            elif first == 2:
                self._grid[self._row] = [" "] * self.cols


def extractQrMatrix(lines: list[str]) -> tuple[tuple[bool, ...], ...] | None:
    """从虚拟终端文本行提取 go-qrcode ToSmallString(false) 的正方形黑白模块矩阵。"""
    best: tuple[tuple[bool, ...], ...] | None = None
    index = 0
    totalLines = len(lines)
    while index < totalLines:
        line = lines[index]
        width = len(line)
        if width < 21 or set(line) != {"\u2588"}:
            index += 1
            continue
        expectedRows = (width + 1) // 2
        if index + expectedRows > totalLines:
            index += 1
            continue
        block = lines[index:index + expectedRows]
        if not all(len(row) == width and all(ch in QR_PIXEL_MAP for ch in row) for row in block):
            index += 1
            continue
        pixels: list[tuple[bool, ...]] = []
        for row in block:
            topRow = tuple(QR_PIXEL_MAP[ch][0] for ch in row)
            bottomRow = tuple(QR_PIXEL_MAP[ch][1] for ch in row)
            pixels.append(topRow)
            if len(pixels) < width:
                pixels.append(bottomRow)
        if len(pixels) == width and isValidQrMatrix(pixels):
            best = tuple(pixels)
            index += expectedRows
            continue
        index += 1
    return best


def isValidQrMatrix(pixels: list[tuple[bool, ...]]) -> bool:
    size = len(pixels)
    if size < 21:
        return False
    # 顶边与底边均应为白色静音区，且内部包含合理的黑白模块密度
    if any(pixels[0]) or any(pixels[-1]):
        return False
    blackCount = sum(1 for row in pixels for cell in row if cell)
    ratio = blackCount / (size * size)
    return 0.12 <= ratio <= 0.85


if sys.platform == "win32":
    class COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR),
            ("lpTitle", wt.LPWSTR), ("dwX", wt.DWORD), ("dwY", wt.DWORD),
            ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD), ("dwXCountChars", wt.DWORD),
            ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
            ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD), ("lpReserved2", ctypes.POINTER(wt.BYTE)),
            ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE), ("hStdError", wt.HANDLE),
        ]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
            ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD),
        ]

    HPCON = ctypes.c_void_p
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreatePipe.argtypes = [ctypes.POINTER(wt.HANDLE), ctypes.POINTER(wt.HANDLE), ctypes.c_void_p, wt.DWORD]
    _kernel32.CreatePseudoConsole.argtypes = [COORD, wt.HANDLE, wt.HANDLE, wt.DWORD, ctypes.POINTER(HPCON)]
    _kernel32.CreatePseudoConsole.restype = ctypes.c_long
    _kernel32.ClosePseudoConsole.argtypes = [HPCON]
    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p,
    ]
    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.CreateProcessW.argtypes = [
        wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
        ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION),
    ]
    _kernel32.ReadFile.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    _kernel32.WriteFile.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD), ctypes.c_void_p,
    ]
    _kernel32.PeekNamedPipe.argtypes = [
        wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
        ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
    ]
    _kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    _kernel32.WaitForSingleObject.restype = wt.DWORD
    _kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    _kernel32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]


class ConPtyProcess:
    """Windows 无窗口 ConPTY 伪终端进程，支持读取二维码渲染与投递 2FA 密码。"""

    def __init__(self, args: list[str], cols: int = 120, rows: int = 60) -> None:
        if sys.platform != "win32":
            raise TaskError("内置扫码会话当前仅支持 Windows 平台")
        self._closed = False
        self._returncode: int | None = None
        self._hpc = HPCON()
        self._inWrite = wt.HANDLE()
        self._outRead = wt.HANDLE()
        self._attrBuf = None
        self._pi = PROCESS_INFORMATION()

        inRead, outWrite = wt.HANDLE(), wt.HANDLE()
        if not _kernel32.CreatePipe(ctypes.byref(inRead), ctypes.byref(self._inWrite), None, 0):
            raise TaskError("无法创建 ConPTY 输入管道")
        if not _kernel32.CreatePipe(ctypes.byref(self._outRead), ctypes.byref(outWrite), None, 0):
            _kernel32.CloseHandle(inRead)
            _kernel32.CloseHandle(self._inWrite)
            raise TaskError("无法创建 ConPTY 输出管道")

        hr = _kernel32.CreatePseudoConsole(COORD(cols, rows), inRead, outWrite, 0, ctypes.byref(self._hpc))
        _kernel32.CloseHandle(inRead)
        _kernel32.CloseHandle(outWrite)
        if hr != 0:
            _kernel32.CloseHandle(self._inWrite)
            _kernel32.CloseHandle(self._outRead)
            raise TaskError("无法初始化 Windows ConPTY（错误码 {code}）", code=hr)

        attrSize = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attrSize))
        self._attrBuf = ctypes.create_string_buffer(attrSize.value)
        if not _kernel32.InitializeProcThreadAttributeList(self._attrBuf, 1, 0, ctypes.byref(attrSize)):
            self.close()
            raise TaskError("无法初始化 ConPTY 进程属性")
        if not _kernel32.UpdateProcThreadAttribute(
            self._attrBuf, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            self._hpc, ctypes.sizeof(HPCON), None, None,
        ):
            self.close()
            raise TaskError("无法绑定 ConPTY 进程属性")

        si = STARTUPINFOEXW()
        si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        si.lpAttributeList = ctypes.cast(self._attrBuf, ctypes.c_void_p)

        commandLine = subprocess.list2cmdline(args)
        ok = _kernel32.CreateProcessW(
            None,
            ctypes.create_unicode_buffer(commandLine),
            None,
            None,
            False,
            EXTENDED_STARTUPINFO_PRESENT,
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(self._pi),
        )
        if not ok:
            errorCode = ctypes.get_last_error()
            self.close()
            raise TaskError("无法在 ConPTY 中启动 tdl（错误码 {code}）", code=errorCode)

    @property
    def returncode(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if self._closed or not self._pi.hProcess:
            return self._returncode
        code = wt.DWORD(0)
        if _kernel32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code)) and code.value != STILL_ACTIVE:
            self._returncode = int(code.value)
        return self._returncode

    def readAvailable(self, maxBytes: int = 4096) -> bytes:
        if self._closed or not self._outRead:
            return b""
        available = wt.DWORD(0)
        if not _kernel32.PeekNamedPipe(self._outRead, None, 0, None, ctypes.byref(available), None):
            return b""
        if available.value == 0:
            return b""
        size = min(maxBytes, available.value)
        buffer = ctypes.create_string_buffer(size)
        read = wt.DWORD(0)
        if not _kernel32.ReadFile(self._outRead, buffer, size, ctypes.byref(read), None):
            return b""
        return buffer.raw[:read.value]

    async def writeInput(self, text: str) -> None:
        if self._closed or not self._inWrite:
            return
        payload = text.encode("utf-8")

        def writeAll() -> None:
            view = memoryview(payload)
            offset = 0
            while offset < len(payload):
                chunk = bytes(view[offset:])
                written = wt.DWORD(0)
                if not _kernel32.WriteFile(self._inWrite, chunk, len(chunk), ctypes.byref(written), None):
                    break
                if written.value <= 0:
                    break
                offset += written.value

        await asyncio.to_thread(writeAll)

    async def wait(self) -> int:
        if self._returncode is not None:
            return self._returncode
        if self._closed or not self._pi.hProcess:
            return -1

        def waitExit() -> int:
            _kernel32.WaitForSingleObject(self._pi.hProcess, 0xFFFFFFFF)
            code = wt.DWORD(0)
            if _kernel32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code)):
                return int(code.value)
            return -1

        self._returncode = await asyncio.to_thread(waitExit)
        return self._returncode

    def terminate(self) -> None:
        if not self._closed and self._pi.hProcess:
            with suppress(OSError):
                _kernel32.TerminateProcess(self._pi.hProcess, 1)

    def kill(self) -> None:
        self.terminate()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pi.hProcess and self._returncode is None:
            code = wt.DWORD(0)
            if _kernel32.GetExitCodeProcess(self._pi.hProcess, ctypes.byref(code)) and code.value != STILL_ACTIVE:
                self._returncode = int(code.value)
        if self._hpc:
            _kernel32.ClosePseudoConsole(self._hpc)
            self._hpc = HPCON()
        if self._attrBuf is not None:
            _kernel32.DeleteProcThreadAttributeList(self._attrBuf)
            self._attrBuf = None
        for handle in (self._pi.hThread, self._pi.hProcess, self._inWrite, self._outRead):
            if handle:
                _kernel32.CloseHandle(handle)
        self._pi = PROCESS_INFORMATION()
        self._inWrite = wt.HANDLE()
        self._outRead = wt.HANDLE()


def createConPtyProcess(args: list[str], cols: int = 120, rows: int = 60) -> ConPtyProcess:
    return ConPtyProcess(args, cols=cols, rows=rows)
