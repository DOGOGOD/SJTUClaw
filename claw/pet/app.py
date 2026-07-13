"""Tk desktop window that renders a Codex-compatible pet atlas."""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageTk

from claw.pet.catalog import PetCatalog


CELL_WIDTH = 192
CELL_HEIGHT = 208
WINDOW_BASE_WIDTH = 330
WINDOW_BASE_HEIGHT = 235
# Codex's floating mascot layout uses a 121 logical-pixel-high pet box.
PET_BASE_SCALE = 121 / CELL_HEIGHT
PET_BASE_CENTER_Y = 160
TRANSPARENT_COLOR = "#010203"
IDLE_DURATION_MULTIPLIER = 6
NON_IDLE_REPEAT_COUNT = 3

ANIMATIONS: dict[str, tuple[int, list[int]]] = {
    "idle": (0, [280, 110, 110, 140, 140, 320]),
    "running-right": (1, [120, 120, 120, 120, 120, 120, 120, 220]),
    "running-left": (2, [120, 120, 120, 120, 120, 120, 120, 220]),
    "waving": (3, [140, 140, 140, 280]),
    "jumping": (4, [140, 140, 140, 140, 280]),
    "failed": (5, [140, 140, 140, 140, 140, 140, 140, 240]),
    "waiting": (6, [150, 150, 150, 150, 150, 260]),
    "running": (7, [120, 120, 120, 120, 120, 220]),
    "review": (8, [150, 150, 150, 150, 150, 280]),
}


def _rounded_rectangle_points(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
) -> tuple[float, ...]:
    """Return control points for a smooth Canvas rounded rectangle."""
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    return (
        x1 + radius, y1,
        x1 + radius, y1,
        x2 - radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1 + radius,
        x1, y1,
    )


def _make_color_key_safe(image: Image.Image, alpha_cutoff: int = 128) -> Image.Image:
    """Remove translucent pixels that become dark fringes in Tk color-key windows."""
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A").point(
        lambda value: 255 if value >= alpha_cutoff else 0
    )
    rgba.putalpha(alpha)
    return rgba


class GatewayClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def get_state(self) -> dict[str, Any]:
        return self._request("GET", "/pet/state")

    def approve(self, approval_id: str) -> None:
        self._request("POST", f"/approvals/{approval_id}/approve")

    def reject(self, approval_id: str) -> None:
        self._request(
            "POST",
            f"/approvals/{approval_id}/reject",
            {"reason": "用户通过桌面宠物拒绝"},
        )

    def save_position(self, x: int, y: int) -> None:
        self._request("POST", "/pet/runtime/position", {"x": x, "y": y})

    def notify_closed(self) -> None:
        try:
            self._request("POST", "/pet/runtime/closed")
        except Exception:
            pass

    def _request(self, method: str, path: str, body: dict | None = None) -> dict[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "X-SJTUClaw-Internal": "desktop-pet",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(request, timeout=2.5) as response:
            return json.loads(response.read().decode("utf-8"))


class DesktopPet:
    def __init__(self, gateway_url: str, data_dir: Path):
        self.client = GatewayClient(gateway_url)
        self.catalog = PetCatalog(data_dir)
        self.settings = self.catalog.load_settings()
        self.pet = self.catalog.get_pet(self.settings.selected_pet_id)
        if self.pet is None:
            raise RuntimeError("没有可用的宠物资源")

        self._dpi_scale = _enable_dpi_awareness()
        self.window_width = self._px(WINDOW_BASE_WIDTH)
        self.window_height = self._px(WINDOW_BASE_HEIGHT)
        self.pet_center_y = self._px(PET_BASE_CENTER_Y)

        self.root = tk.Tk(className="SJTUClawPet")
        self.root.title(self.pet["displayName"])
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=TRANSPARENT_COLOR)
        if self.root.tk.call("tk", "windowingsystem") == "win32":
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
        else:
            self.root.attributes("-alpha", 0.98)

        self.canvas = tk.Canvas(
            self.root,
            width=self.window_width,
            height=self.window_height,
            bg=TRANSPARENT_COLOR,
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self._status_font = tkfont.Font(
            root=self.root,
            family="Microsoft YaHei UI",
            size=10,
            weight="bold",
        )
        self._task_font = tkfont.Font(
            root=self.root,
            family="Microsoft YaHei UI",
            size=8,
        )
        self._bubble = self.canvas.create_polygon(
            *_rounded_rectangle_points(
                self._px(8),
                self._px(8),
                self.window_width - self._px(8),
                self._px(72),
                self._px(14),
            ),
            fill="#FFFDF8",
            outline="#D9D4CA",
            width=self._px(1),
            smooth=True,
            splinesteps=24,
        )
        self._status_text = self.canvas.create_text(
            self._px(22), self._px(22), anchor="nw", fill="#27241F",
            font=self._status_font, width=self._px(285),
            text="月薪喵 · 待命中",
        )
        self._task_text = self.canvas.create_text(
            self._px(22), self._px(45), anchor="nw", fill="#6C655B",
            font=self._task_font, width=self._px(285),
            text="",
        )
        self._pet_image_id = self.canvas.create_image(
            self.window_width // 2, self.pet_center_y, anchor="center"
        )

        self._frames: dict[tuple[int, int], ImageTk.PhotoImage] = {}
        self._atlas_rows = 9
        self._load_atlas(Path(self.pet["spritesheetPath"]))
        self._animation = "idle"
        self._requested_animation = "idle"
        self._frame_index = 0
        self._completed_cycles = 0
        self._continuous_animation = False
        self._animation_job: str | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._remote_state: dict[str, Any] = {}
        self._approvals: list[dict[str, Any]] = []
        self._drag_origin: tuple[int, int, int, int] | None = None
        self._dragging = False
        self._hovering_pet = False
        self._closed = threading.Event()
        self._updates: queue.Queue[dict[str, Any]] = queue.Queue()

        self.menu = tk.Menu(self.root, tearoff=False, font=("Microsoft YaHei UI", 9))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_pointer_motion)
        self.canvas.bind("<Leave>", self._on_pointer_leave)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._place_initially()
        self._start_animation("idle")
        self._update_bubble("", "", visible=False)
        self.root.after(100, self._drain_updates)
        threading.Thread(target=self._poll_gateway, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()

    def close(self, *, notify: bool = True) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._animation_job is not None:
            try:
                self.root.after_cancel(self._animation_job)
            except tk.TclError:
                pass
            self._animation_job = None
        if notify:
            threading.Thread(target=self.client.notify_closed, daemon=True).start()
        self.root.destroy()

    def _load_atlas(self, path: Path) -> None:
        with Image.open(path) as source:
            atlas = source.convert("RGBA")
        self._atlas_rows = atlas.height // CELL_HEIGHT
        display_scale = PET_BASE_SCALE * self._dpi_scale
        display_size = (
            round(CELL_WIDTH * display_scale),
            round(CELL_HEIGHT * display_scale),
        )
        self._pet_display_size = display_size
        for row in range(self._atlas_rows):
            for column in range(8):
                cell = atlas.crop((
                    column * CELL_WIDTH,
                    row * CELL_HEIGHT,
                    (column + 1) * CELL_WIDTH,
                    (row + 1) * CELL_HEIGHT,
                ))
                # Render directly from the native atlas cell to the final
                # physical-pixel size. The old path first shrank to 72% and
                # Windows then enlarged the whole non-DPI-aware window, which
                # caused two resampling passes and visibly soft edges.
                if cell.size != display_size:
                    cell = cell.resize(display_size, Image.Resampling.LANCZOS)
                # Windows implements Tk's transparent window through a color
                # key. Partially transparent pixels are blended against that
                # dark key and remain visible as a duplicate outer outline.
                # A binary mask keeps the original opaque artwork while making
                # its exterior fully transparent.
                cell = _make_color_key_safe(cell)
                self._frames[(row, column)] = ImageTk.PhotoImage(cell)

    def _place_initially(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        default_x = screen_w - self.window_width - self._px(36)
        default_y = screen_h - self.window_height - self._px(70)
        x = self.settings.position_x if self.settings.position_x is not None else default_x
        y = self.settings.position_y if self.settings.position_y is not None else default_y
        x = max(0, min(x, screen_w - self.window_width))
        y = max(0, min(y, screen_h - self.window_height))
        self.root.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")

    def _advance_animation(self) -> None:
        self._animation_job = None
        if self._closed.is_set():
            return
        if (
            self._animation == "idle"
            and self._atlas_rows >= 11
            and self._show_look_frame()
        ):
            self._animation_job = self.root.after(120, self._advance_animation)
            return

        row, durations = ANIMATIONS[self._animation]
        self._frame_index += 1
        if self._frame_index >= len(durations):
            self._frame_index = 0
            self._completed_cycles += 1
            if (
                self._animation != "idle"
                and not self._continuous_animation
                and self._completed_cycles >= NON_IDLE_REPEAT_COUNT
            ):
                # Codex plays a transient state three times, then settles into
                # its deliberately slow idle loop until the state prop changes.
                self._start_animation("idle")
                return
        self._show_frame(row, self._frame_index)
        self._schedule_next_frame()

    def _show_look_frame(self) -> bool:
        pointer_x = self.root.winfo_pointerx()
        pointer_y = self.root.winfo_pointery()
        pet_x = self.root.winfo_rootx() + self.window_width // 2
        pet_y = self.root.winfo_rooty() + self.pet_center_y
        dx, dy = pointer_x - pet_x, pointer_y - pet_y
        if math.hypot(dx, dy) < self._px(70):
            return False
        degrees = math.degrees(math.atan2(dx, -dy)) % 360
        direction = int((degrees + 11.25) // 22.5) % 16
        row = 9 if direction < 8 else 10
        self._show_frame(row, direction % 8)
        return True

    def _start_animation(self, animation: str, *, continuous: bool = False) -> None:
        if animation not in ANIMATIONS:
            animation = "idle"
        if self._animation_job is not None:
            self.root.after_cancel(self._animation_job)
            self._animation_job = None
        self._animation = animation
        self._frame_index = 0
        self._completed_cycles = 0
        self._continuous_animation = continuous
        self._show_frame(ANIMATIONS[animation][0], 0)
        self._schedule_next_frame()

    def _schedule_next_frame(self) -> None:
        _row, durations = ANIMATIONS[self._animation]
        delay = durations[self._frame_index]
        if self._animation == "idle":
            delay *= IDLE_DURATION_MULTIPLIER
        self._animation_job = self.root.after(delay, self._advance_animation)

    def _show_frame(self, row: int, column: int) -> None:
        photo = self._frames.get((row, column)) or self._frames[(0, 0)]
        self._photo = photo
        self.canvas.itemconfigure(self._pet_image_id, image=photo)

    def _on_press(self, event: tk.Event) -> None:
        if not self._point_in_pet(event.x, event.y):
            self._drag_origin = None
            return
        self._drag_origin = (
            event.x_root,
            event.y_root,
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        self._dragging = False

    def _on_drag(self, event: tk.Event) -> None:
        if self._drag_origin is None:
            return
        start_x, start_y, window_x, window_y = self._drag_origin
        dx, dy = event.x_root - start_x, event.y_root - start_y
        if abs(dx) + abs(dy) > 4:
            self._dragging = True
        drag_animation = "running-right" if dx >= 0 else "running-left"
        if self._animation != drag_animation or not self._continuous_animation:
            self._start_animation(drag_animation, continuous=True)
        self.root.geometry(f"+{window_x + dx}+{window_y + dy}")

    def _on_release(self, _event: tk.Event) -> None:
        was_dragging = self._dragging
        self._drag_origin = None
        self._dragging = False
        self._start_animation(
            "jumping" if self._hovering_pet else self._requested_animation
        )
        if was_dragging:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            threading.Thread(
                target=lambda: self._safe_save_position(x, y), daemon=True
            ).start()

    def _on_pointer_motion(self, event: tk.Event) -> None:
        if self._dragging:
            return
        hovering = self._point_in_pet(event.x, event.y)
        if hovering == self._hovering_pet:
            return
        self._hovering_pet = hovering
        self._start_animation("jumping" if hovering else self._requested_animation)

    def _on_pointer_leave(self, _event: tk.Event) -> None:
        if self._dragging or not self._hovering_pet:
            return
        self._hovering_pet = False
        self._start_animation(self._requested_animation)

    def _point_in_pet(self, x: int, y: int) -> bool:
        width, height = self._pet_display_size
        center_x = self.window_width // 2
        return (
            center_x - width // 2 <= x <= center_x + width // 2
            and self.pet_center_y - height // 2 <= y <= self.pet_center_y + height // 2
        )

    def _safe_save_position(self, x: int, y: int) -> None:
        try:
            self.client.save_position(x, y)
        except Exception:
            pass

    def _show_menu(self, event: tk.Event) -> None:
        self.menu.delete(0, "end")
        pending = self._approvals[0] if self._approvals else None
        if pending:
            tool = pending.get("toolName", "命令")
            self.menu.add_command(
                label=f"批准：{tool}",
                command=lambda: self._decide(pending["approvalId"], True),
            )
            self.menu.add_command(
                label=f"拒绝：{tool}",
                command=lambda: self._decide(pending["approvalId"], False),
            )
            self.menu.add_separator()
        self.menu.add_command(label="关闭宠物", command=self.close)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _decide(self, approval_id: str, approve: bool) -> None:
        def worker() -> None:
            try:
                if approve:
                    self.client.approve(approval_id)
                else:
                    self.client.reject(approval_id)
            except Exception:
                return
        threading.Thread(target=worker, daemon=True).start()

    def _poll_gateway(self) -> None:
        while not self._closed.wait(1.0):
            try:
                self._updates.put(self.client.get_state())
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                continue

    def _drain_updates(self) -> None:
        if self._closed.is_set():
            return
        latest = None
        try:
            while True:
                latest = self._updates.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            self._remote_state = latest.get("state") or {}
            self._approvals = latest.get("approvals") or []
            selected = latest.get("selectedPet") or {}
            if selected.get("id") and selected.get("id") != self.pet.get("id"):
                # A settings change takes effect through a process restart. The
                # server performs that restart, so this instance exits quietly.
                self.close(notify=False)
                return
            self._apply_remote_state()
        self.root.after(100, self._drain_updates)

    def _apply_remote_state(self) -> None:
        if self._dragging:
            return
        state = self._remote_state
        animation = state.get("animation", "idle")
        if animation not in ANIMATIONS:
            animation = "idle"
        if animation != self._requested_animation:
            self._requested_animation = animation
            if not self._dragging and not self._hovering_pet:
                self._start_animation(animation)
        display_name = self.pet.get("displayName", "宠物")
        message = state.get("message") or "待命中"
        task = state.get("task") or ""
        if self._approvals:
            approval = self._approvals[0]
            message = f"等待审批：{approval.get('toolName', '命令')}（右键处理）"
            animation = "waiting"
            if animation != self._requested_animation:
                self._requested_animation = animation
                if not self._dragging and not self._hovering_pet:
                    self._start_animation(animation)
        visible = should_show_bubble(state, self._approvals)
        self._update_bubble(f"{display_name} · {message}", task, visible=visible)

    def _update_bubble(self, status: str, task: str, *, visible: bool) -> None:
        """Update bubble content and vertically center its text block."""
        item_state = "normal" if visible else "hidden"
        for item in (self._bubble, self._status_text, self._task_text):
            self.canvas.itemconfigure(item, state=item_state)
        if not visible:
            return

        self.canvas.itemconfigure(self._status_text, text=status)
        self.canvas.itemconfigure(self._task_text, text=task)
        self.canvas.itemconfigure(
            self._task_text,
            state="normal" if task else "hidden",
        )

        left = self._px(22)
        top = self._px(8)
        base_bottom = self._px(72)
        padding = self._px(13)
        gap = self._px(4)
        status_height = self._status_font.metrics("linespace")
        task_height = self._task_font.metrics("linespace") if task else 0
        if task:
            task_bbox = self.canvas.bbox(self._task_text)
            if task_bbox is not None:
                task_height = task_bbox[3] - task_bbox[1]
        text_height = status_height + (gap + task_height if task else 0)
        bubble_bottom = max(base_bottom, top + text_height + 2 * padding)
        start_y = top + (bubble_bottom - top - text_height) / 2

        self.canvas.coords(
            self._bubble,
            *_rounded_rectangle_points(
                self._px(8),
                top,
                self.window_width - self._px(8),
                bubble_bottom,
                self._px(14),
            ),
        )
        self.canvas.coords(self._status_text, left, start_y)
        if task:
            self.canvas.coords(self._task_text, left, start_y + status_height + gap)

    def _px(self, logical_pixels: float) -> int:
        return max(1, round(logical_pixels * self._dpi_scale))


def should_show_bubble(state: dict[str, Any], approvals: list[dict[str, Any]]) -> bool:
    """Only show task context while work or approval is active."""
    if approvals:
        return True
    return str(state.get("phase") or "idle") != "idle"


def _enable_dpi_awareness() -> float:
    """Use physical pixels on Windows and return the current DPI scale."""
    if os.name != "nt":
        return 1.0
    try:
        import ctypes

        user32 = ctypes.windll.user32
        try:
            # PER_MONITOR_AWARE_V2. This must run before the first Tk window.
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()
        dpi = int(user32.GetDpiForSystem())
        return max(1.0, dpi / 96.0)
    except (AttributeError, OSError, ValueError):
        return 1.0


def run_desktop_pet(gateway_url: str, data_dir: Path) -> int:
    lock_path = Path(data_dir) / "pet" / "desktop.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _single_instance_lock(lock_path) as acquired:
        if acquired:
            pet = DesktopPet(gateway_url, data_dir)
            pet.run()
    return 0


@contextmanager
def _single_instance_lock(path: Path):
    """Hold a non-blocking, process-wide lock using only the stdlib."""
    handle = path.open("a+b")
    handle.seek(0)
    acquired = False
    try:
        if __import__("os").name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                pass
        yield acquired
    finally:
        if acquired:
            try:
                handle.seek(0)
                if __import__("os").name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
