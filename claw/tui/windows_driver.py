"""Windows Textual driver fixes for modifier-aware composer input."""

from __future__ import annotations

import sys
import threading
from asyncio import AbstractEventLoop, get_running_loop, run_coroutine_threadsafe
from ctypes import byref, wintypes
from typing import Callable

from textual import constants
from textual._xterm_parser import XTermParser
from textual.drivers import win32
from textual.drivers._writer_thread import WriterThread
from textual.drivers.windows_driver import WindowsDriver
from textual.events import Event, Resize
from textual.geometry import Size


SHIFT_PRESSED = 0x0010
VK_RETURN = 0x000D
KEY_EVENT = 0x0001
WINDOW_BUFFER_SIZE_EVENT = 0x0004


def translate_windows_key(
    character: str,
    control_state: int,
    virtual_key: int,
) -> str | None:
    """Preserve Shift+Enter before Windows VT input drops the modifier."""
    if control_state & SHIFT_PRESSED and (
        character in {"\r", "\n"} or virtual_key == VK_RETURN
    ):
        # Kitty keyboard protocol encoding for Shift+Enter. Textual's parser
        # turns this into a single ``shift+enter`` event.
        return "\x1b[13;2u"
    if control_state and virtual_key == 0:
        return None
    return character


class ModifierAwareEventMonitor(threading.Thread):
    """Textual's Windows monitor with Shift+Enter preservation."""

    def __init__(
        self,
        loop: AbstractEventLoop,
        app,
        exit_event: threading.Event,
        process_event: Callable[[Event], None],
    ) -> None:
        self.loop = loop
        self.app = app
        self.exit_event = exit_event
        self.process_event = process_event
        super().__init__(name="sjtuclaw-textual-input")

    def run(self) -> None:
        exit_requested = self.exit_event.is_set
        parser = XTermParser(debug=constants.DEBUG)

        try:
            read_count = wintypes.DWORD(0)
            input_handle = win32.GetStdHandle(win32.STD_INPUT_HANDLE)
            input_records = (win32.INPUT_RECORD * 1024)()
            read_console_input = win32.KERNEL32.ReadConsoleInputW

            while not exit_requested():
                for event in parser.tick():
                    self.process_event(event)

                if win32.wait_for_handles([input_handle], 100) is None:
                    continue

                read_console_input(
                    input_handle,
                    byref(input_records),
                    1024,
                    byref(read_count),
                )
                keys: list[str] = []
                new_size: tuple[int, int] | None = None

                for input_record in input_records[: read_count.value]:
                    if input_record.EventType == KEY_EVENT:
                        key_event = input_record.Event.KeyEvent
                        if not key_event.bKeyDown:
                            continue
                        translated = translate_windows_key(
                            key_event.uChar.UnicodeChar,
                            int(key_event.dwControlKeyState),
                            int(key_event.wVirtualKeyCode),
                        )
                        if translated is not None:
                            keys.append(translated)
                    elif input_record.EventType == WINDOW_BUFFER_SIZE_EVENT:
                        size = input_record.Event.WindowBufferSizeEvent.dwSize
                        new_size = (size.X, size.Y)

                if keys:
                    sequence = (
                        "".join(keys)
                        .encode("utf-16", "surrogatepass")
                        .decode("utf-16")
                    )
                    for event in parser.feed(sequence):
                        self.process_event(event)
                if new_size is not None:
                    self.on_size_change(*new_size)
        except Exception as error:
            self.app.log.error("SJTUCLAW INPUT MONITOR ERROR", error)

    def on_size_change(self, width: int, height: int) -> None:
        size = Size(width, height)
        event = Resize(size, size)
        run_coroutine_threadsafe(self.app._post_message(event), self.loop)


class SJTUClawWindowsDriver(WindowsDriver):
    """Windows driver that keeps Shift+Enter distinct from Enter."""

    def start_application_mode(self) -> None:
        loop = get_running_loop()
        self._restore_console = win32.enable_application_mode()
        self._writer_thread = WriterThread(sys.__stdout__)
        self._writer_thread.start()

        self.write("\x1b[?1049h")
        self._enable_mouse_support()
        self.write("\x1b[?25l")
        self.write("\x1b[?1004h")
        self.write("\x1b[>1u")
        self.flush()
        self._enable_bracketed_paste()

        self._event_thread = ModifierAwareEventMonitor(
            loop,
            self._app,
            self.exit_event,
            self.process_message,
        )
        self._event_thread.start()
