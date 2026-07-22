"""Tk desktop window that renders a Codex-compatible pet atlas."""

from __future__ import annotations

import io
import json
import math
import os
import queue
import random
import re
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageGrab, ImageTk

from claw.pet.catalog import PetCatalog


CELL_WIDTH = 192
CELL_HEIGHT = 208
WINDOW_BASE_WIDTH = 330
# 窗口高度需为气泡预留足够顶部空间（200字气泡约需 220px）：
#   bubble_bottom = PET_BASE_CENTER_Y - _BUBBLE_BOTTOM_OFFSET = 310 - 76 = 234
#   200字 bubble_height ≈ 225 → bubble_top ≈ 9 ≥ 0（圆角不被裁切）
WINDOW_BASE_HEIGHT = 385
# Codex's floating mascot layout uses a 121 logical-pixel-high pet box.
PET_BASE_SCALE = 121 / CELL_HEIGHT
# 宠物中心Y坐标随窗口高度同步增大，保持距窗口底部 75px 不变
PET_BASE_CENTER_Y = 310
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

# 点击桌宠时随机显示的俏皮回复
_PLAYFUL_REPLIES: tuple[str, ...] = (
    "喵～戳我干嘛？",
    "在呢在呢，别戳啦！",
    "想我了吗？",
    "今天也要加油哦～",
    "点击有惊喜？并没有～",
    "哎呀，别闹！",
    "我可是很忙的喵！",
    "嘿嘿，又被你发现了～",
    "要不要给我起个名字？",
    "戳一下，开心一整天～",
    "我在看着你哦～",
    "月薪喵，随时待命！",
    "再戳我就生气了喵！",
    "你今天看起来不错呢～",
    "嗨，有什么吩咐？",
    "呜哇，轻点戳，脑袋要扁啦喵！",
    "咕噜咕噜，找本喵何事呀？",
    "偷偷探头，不会只有你在戳我吧？",
    "再戳就要蹭你手手咯～",
    "本喵在线营业，欢迎投喂！",
    "等等等等，让我伸个懒腰先！",
    "眼光真好，居然选中我啦喵",
    "戳多了要收小鱼干手续费哦",
    "发呆被你逮住啦，完蛋！",
    "软软小脑袋专供你戳一下",
    "有事说事，没事陪我摸鱼喵",
    "哇，又来找我玩啦，好开心",
    "别一直戳，我会害羞躲起来",
    "小鱼干准备好了就听你安排",
    "探头！捕捉一只正在戳我的你",
    "揉一揉小耳朵，有话慢慢说",
    "警告警告，连续戳击触发撒娇模式",
    "本喵摸鱼中，小声一点哦",
    "见到你心情瞬间变好啦喵",
    "要是戳够十下，我就跟你贴贴"
)

# 本地消息显示时长（秒）
_LOCAL_MESSAGE_TTL = 4.0
# 回复消息显示时长（秒）——比俏皮回复更长，给用户阅读时间
_REPLY_MESSAGE_TTL = 15.0

# 气泡底部距桌宠中心点的向上偏移（逻辑像素），用于 _update_bubble 和输入框对齐
_BUBBLE_BOTTOM_OFFSET = 76
# 气泡文字显示上限（字符数），超过则不显示原文，仅显示占位提示
_BUBBLE_DISPLAY_LIMIT = 200
# 超过字数限制时的占位提示
_BUBBLE_OVERLIMIT_HINT = "回复过长，请在 WebUI 查看"


def _strip_markdown(text: str) -> str:
    """移除 Markdown 格式符号，返回适合 Tkinter 气泡显示的纯文本。"""
    # 代码块 ```lang ... ```
    text = re.sub(r"```[^\n]*\n?", "", text)
    # 行内代码 `code`
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # 粗体 **text** 或 __text__
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    # 斜体 *text* 或 _text_（避免误伤单词内下划线）
    text = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", text)
    text = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", text)
    # 删除线 ~~text~~
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    # 标题 # / ## / ###
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # 引用 >
    text = re.sub(r"^>\s*", "", text, flags=re.MULTILINE)
    # 无序列表 - / * / + 开头 → •
    text = re.sub(r"^[\-\*\+]\s+", "• ", text, flags=re.MULTILINE)
    # 有序列表 1. / 2. 开头 → 去掉序号
    text = re.sub(r"^\d+\.\s+", "", text, flags=re.MULTILINE)
    # 图片 ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # 链接 [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # 水平分割线 --- / ***
    text = re.sub(r"^[\-\*_]{3,}$", "", text, flags=re.MULTILINE)
    # 多余空行压缩
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def _point_in_bbox(
    x: float,
    y: float,
    bbox: tuple[int, int, int, int] | None,
    padding: int = 0,
) -> bool:
    """Return whether a canvas point is inside a possibly padded item bbox."""
    if bbox is None:
        return False
    left, top, right, bottom = bbox
    return (
        left - padding <= x <= right + padding
        and top - padding <= y <= bottom + padding
    )


def _clear_pending_image(
    pending_image: dict[str, Image.Image | None],
    popup_canvas: tk.Canvas,
    image_badge: int,
    resize_popup: Callable[[], None],
    entry: tk.Entry,
) -> None:
    """Clear the desktop pet's pending image and restore its input layout."""
    pending_image["value"] = None
    popup_canvas.itemconfigure(image_badge, text="")
    resize_popup()
    entry.focus_set()


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

    def create_session(self) -> dict[str, Any]:
        return self._request("POST", "/sessions", {})

    def fetch_sessions(self) -> list[dict[str, Any]]:
        """获取会话列表（按最近活跃排序）。"""
        data = self._request("GET", "/sessions")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("sessions") or data.get("items") or []
        return []

    def send_message(
        self,
        session_id: str,
        message: str,
        attachment_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """发送消息到指定 session（同步阻塞，应在后台线程调用）。

        /chat 端点会等待 Agent 完整执行后才返回，需要较长超时。
        注意：字段名必须用 camelCase 的 sessionId，与服务端 ChatRequest
        的 alias 保持一致，否则 session_id 会被解析为 None 导致新建 session。
        """
        return self._request(
            "POST", "/chat",
            {
                "sessionId": session_id,
                "message": message,
                "attachmentIds": attachment_ids or [],
            },
            timeout=180.0,
        )

    def upload_image(
        self, session_id: str, image: Image.Image, filename: str
    ) -> dict[str, Any]:
        """Upload a clipboard image through the gateway attachment endpoint."""
        output = io.BytesIO()
        image.convert("RGBA").save(output, format="PNG")
        boundary = f"----SJTUClawPet{time.time_ns():x}"
        disposition_name = filename.replace('"', "")
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{disposition_name}"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode("utf-8")
        body = prefix + output.getvalue() + f"\r\n--{boundary}--\r\n".encode("ascii")
        request = urllib.request.Request(
            self.base_url + f"/sessions/{session_id}/attachments?persistMessage=false",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-SJTUClaw-Internal": "desktop-pet",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=60.0) as response:
            return json.loads(response.read().decode("utf-8"))

    def _request(
        self, method: str, path: str, body: dict | None = None, timeout: float = 5.0
    ) -> dict[str, Any]:
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
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
            size=9,
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
            self._px(22), self._px(22), anchor="nw", fill="#1A1814",
            font=self._status_font, width=self._px(285),
            text="月薪喵 · 待命中",
        )
        self._task_text = self.canvas.create_text(
            self._px(22), self._px(45), anchor="nw", fill="#4A453E",
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
        # 本地消息（点击俏皮回复等）优先于远程状态显示
        self._local_message: str | None = None
        self._local_message_until: float = 0.0
        # 延迟俏皮回复，用于区分单击/双击
        self._pending_reply_job: str | None = None
        self._suppress_reply = False
        # 双击输入框相关
        self._input_popup: tk.Toplevel | None = None

        self.menu = tk.Menu(self.root, tearoff=False, font=("Microsoft YaHei UI", 9))
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-1>", self._on_double_click)
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
        if self._input_popup is not None:
            try:
                self._input_popup.destroy()
            except tk.TclError:
                pass
            self._input_popup = None
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
        if not was_dragging and not self._hovering_pet:
            self._start_animation(self._requested_animation)
        else:
            self._start_animation(
                "jumping" if self._hovering_pet else self._requested_animation
            )
        if was_dragging:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            threading.Thread(
                target=lambda: self._safe_save_position(x, y), daemon=True
            ).start()
        elif self._hovering_pet:
            # 双击的第二次 release：已被 _on_double_click 抑制，直接跳过
            if self._suppress_reply:
                self._suppress_reply = False
                return
            # 延迟显示俏皮回复，若在延迟期内双击则取消（避免双击触发单击）
            if self._pending_reply_job is not None:
                try:
                    self.root.after_cancel(self._pending_reply_job)
                except tk.TclError:
                    pass
            self._pending_reply_job = self.root.after(
                400, self._show_playful_reply
            )

    def _show_playful_reply(self) -> None:
        """点击桌宠时随机显示一条俏皮回复气泡。"""
        self._pending_reply_job = None
        reply = random.choice(_PLAYFUL_REPLIES)
        self._set_local_message(reply)

    def _set_local_message(self, message: str, ttl: float = _LOCAL_MESSAGE_TTL) -> None:
        """设置本地消息，在 TTL 内优先于远程状态显示。"""
        self._local_message = _strip_markdown(message)
        self._local_message_until = time.time() + ttl
        self._refresh_bubble()

    def _on_double_click(self, event: tk.Event) -> None:
        """双击桌宠弹出输入框，可发送消息给 SJTUClaw。

        气泡显示时（有任务/审批/本地消息）禁止双击，避免输入框与气泡重叠。
        """
        if not self._point_in_pet(event.x, event.y):
            return
        # 气泡可见时禁止双击
        now = time.time()
        bubble_visible = (
            (self._local_message is not None and now < self._local_message_until)
            or should_show_bubble(self._remote_state, self._approvals)
        )
        if bubble_visible:
            return
        # 取消 pending 的俏皮回复（第一次 release 设置的延迟）
        if self._pending_reply_job is not None:
            try:
                self.root.after_cancel(self._pending_reply_job)
            except tk.TclError:
                pass
            self._pending_reply_job = None
        # 抑制第二次 release 的俏皮回复
        self._suppress_reply = True
        # 阻止拖拽
        self._drag_origin = None
        self._dragging = False
        self._open_input_popup()

    def _open_input_popup(self) -> None:
        """打开消息输入气泡框。"""
        if self._input_popup is not None and self._input_popup.winfo_exists():
            self._input_popup.focus_set()
            return

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=TRANSPARENT_COLOR)
        if popup.tk.call("tk", "windowingsystem") == "win32":
            popup.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)

        entry_font = tkfont.Font(
            root=self.root, family="Microsoft YaHei UI", size=10,
        )
        btn_radius = self._px(11)

        # 定位：输入框底部与状态气泡底部对齐（同一水平高度，误差 ±2px）
        pet_x = self.root.winfo_rootx()
        pet_y = self.root.winfo_rooty()
        popup_width = self._px(300)
        popup_height = self._px(36)
        popup_x = pet_x + (self.window_width - popup_width) // 2
        # 气泡底部在窗口坐标系 Y = pet_center_y - offset，转屏幕坐标后对齐输入框底部
        bubble_bottom_screen_y = pet_y + self.pet_center_y - self._px(_BUBBLE_BOTTOM_OFFSET)
        popup_y = max(0, bubble_bottom_screen_y - popup_height)
        popup.geometry(f"{popup_width}x{popup_height}+{popup_x}+{popup_y}")

        popup_canvas = tk.Canvas(
            popup, width=popup_width, height=popup_height,
            bg=TRANSPARENT_COLOR, highlightthickness=0, bd=0,
        )
        popup_canvas.pack(fill="both", expand=True)

        bubble_id = popup_canvas.create_polygon(
            *_rounded_rectangle_points(
                self._px(4), self._px(4),
                popup_width - self._px(4), popup_height - self._px(4),
                self._px(12),
            ),
            fill="#FFFDF8", outline="#D9D4CA", width=self._px(1),
            smooth=True, splinesteps=24,
        )

        entry = tk.Entry(
            popup_canvas, font=entry_font, bd=0, relief="flat",
            highlightthickness=0, bg="#FFFDF8", fg="#27241F",
            insertbackground="#27241F",
        )
        entry_window = popup_canvas.create_window(
            self._px(16), popup_height // 2, anchor="w", window=entry,
        )
        image_badge = popup_canvas.create_text(
            self._px(16), popup_height // 2,
            anchor="w", text="", fill="#D66A00", font=entry_font,
        )
        pending_image: dict[str, Image.Image | None] = {"value": None}

        # 圆形发送按钮（橙色背景 + 白色箭头）
        btn_cx = popup_width - self._px(24)
        btn_cy = popup_height // 2
        btn_circle = popup_canvas.create_oval(
            btn_cx - btn_radius, btn_cy - btn_radius,
            btn_cx + btn_radius, btn_cy + btn_radius,
            fill="#FF8C00", outline="#E07800", width=self._px(1),
        )
        # 白色向上箭头（三角形）
        arrow_size = self._px(5)
        btn_arrow = popup_canvas.create_polygon(
            btn_cx - arrow_size, btn_cy + arrow_size * 0.8,
            btn_cx + arrow_size, btn_cy + arrow_size * 0.8,
            btn_cx, btn_cy - arrow_size * 1.2,
            fill="white", outline="white", smooth=False,
        )

        def _on_send_click(_event=None):
            self._send_input_message(entry, popup, pending_image["value"])

        # 用 Canvas 级别绑定 + 位置检查，避免重叠 item 重复触发
        btn_pos = {"x": btn_cx, "y": btn_cy, "r": btn_radius}

        def _on_canvas_click(event: tk.Event):
            badge_bbox = popup_canvas.bbox(image_badge)
            if (
                pending_image["value"] is not None
                and _point_in_bbox(
                    event.x, event.y, badge_bbox, padding=self._px(5)
                )
            ):
                _clear_pending_image(
                    pending_image, popup_canvas, image_badge, _resize_popup, entry
                )
                return "break"

            dx = event.x - btn_pos["x"]
            dy = event.y - btn_pos["y"]
            if dx * dx + dy * dy <= btn_pos["r"] * btn_pos["r"]:
                _on_send_click()

        popup_canvas.bind("<Button-1>", _on_canvas_click)

        def _resize_popup():
            """根据输入文字长度自适应气泡框大小。"""
            text = entry.get()
            char_width = entry_font.measure("测")
            needed_text_width = max(
                self._px(160), len(text) * char_width + self._px(30)
            )
            max_width = self._px(420)
            content_width = min(needed_text_width, max_width)
            total_width = content_width + self._px(52)  # 圆形按钮空间
            height = popup_height
            pet_x = self.root.winfo_rootx()
            pet_y = self.root.winfo_rooty()
            popup_x = pet_x + (self.window_width - total_width) // 2
            # 与状态气泡底部保持同一水平高度（误差 ±2px）
            bubble_bottom_screen_y = pet_y + self.pet_center_y - self._px(_BUBBLE_BOTTOM_OFFSET)
            popup_y = max(0, bubble_bottom_screen_y - height)
            popup.geometry(f"{total_width}x{height}+{popup_x}+{popup_y}")
            popup_canvas.configure(width=total_width, height=height)
            popup_canvas.coords(
                bubble_id,
                *_rounded_rectangle_points(
                    self._px(4), self._px(4),
                    total_width - self._px(4), height - self._px(4),
                    self._px(12),
                ),
            )
            badge_width = (
                entry_font.measure("图片 ×") + self._px(8)
                if pending_image["value"] is not None
                else 0
            )
            popup_canvas.coords(image_badge, self._px(16), height // 2)
            popup_canvas.coords(entry_window, self._px(16) + badge_width, height // 2)
            popup_canvas.itemconfigure(
                entry_window, width=content_width - self._px(16) - badge_width
            )
            # 重新定位圆形按钮
            new_cx = total_width - self._px(24)
            new_cy = height // 2
            btn_pos["x"] = new_cx
            btn_pos["y"] = new_cy
            popup_canvas.coords(
                btn_circle,
                new_cx - btn_radius, new_cy - btn_radius,
                new_cx + btn_radius, new_cy + btn_radius,
            )
            popup_canvas.coords(
                btn_arrow,
                new_cx - arrow_size, new_cy + arrow_size * 0.8,
                new_cx + arrow_size, new_cy + arrow_size * 0.8,
                new_cx, new_cy - arrow_size * 1.2,
            )

        def _on_key(event: tk.Event):
            if event.keysym == "Return":
                _on_send_click()
            else:
                popup.after_idle(_resize_popup)

        def _on_paste(_event: tk.Event):
            """Use the native text paste unless the clipboard contains an image."""
            try:
                clipboard = ImageGrab.grabclipboard()
            except (OSError, NotImplementedError, tk.TclError):
                return None
            image: Image.Image | None = None
            if isinstance(clipboard, Image.Image):
                image = clipboard.copy()
            elif isinstance(clipboard, list):
                for candidate in clipboard:
                    try:
                        with Image.open(candidate) as opened:
                            image = opened.copy()
                        break
                    except (OSError, TypeError):
                        continue
            if image is None:
                return None
            pending_image["value"] = image
            popup_canvas.itemconfigure(image_badge, text="图片 ×")
            _resize_popup()
            entry.focus_set()
            return "break"

        entry.bind("<KeyRelease>", _on_key)
        entry.bind("<KeyPress>", lambda e: popup.after_idle(_resize_popup))
        entry.bind("<<Paste>>", _on_paste)
        popup.bind("<FocusOut>", lambda e: self._close_input_popup(popup))
        popup.bind("<Escape>", lambda e: self._close_input_popup(popup))

        self._input_popup = popup
        _resize_popup()
        entry.focus_set()

    def _close_input_popup(self, popup: tk.Toplevel) -> None:
        """关闭输入气泡框。"""
        try:
            popup.destroy()
        except tk.TclError:
            pass
        if self._input_popup is popup:
            self._input_popup = None

    def _send_input_message(
        self,
        entry: tk.Entry,
        popup: tk.Toplevel,
        image: Image.Image | None = None,
    ) -> None:
        """发送输入框中的消息给 SJTUClaw。"""
        # 防重入：popup 已关闭则跳过（Enter + 点击可能同时触发）
        try:
            if not popup.winfo_exists():
                return
        except tk.TclError:
            return
        text = entry.get().strip()
        if not text and image is None:
            return
        self._close_input_popup(popup)
        self._set_local_message("消息已发送，执行中…")
        threading.Thread(
            target=self._send_message_worker, args=(text, image), daemon=True
        ).start()

    def _recent_or_new_session_id(self) -> str | None:
        """Resolve the most recent session, creating one only when none exist."""
        sessions = self.client.fetch_sessions()
        if sessions:
            recent = sessions[0]
            return recent.get("sessionId") or recent.get("session_id") or recent.get("id")
        created = self.client.create_session()
        return created.get("sessionId") or created.get("session_id")

    def _send_message_worker(
        self, text: str, image: Image.Image | None = None
    ) -> None:
        """在后台线程中发送消息（/chat 是同步阻塞的）。

        始终使用最近活跃的 session；fetch_sessions 失败时不新建 session。
        只有 fetch 成功且返回空列表（系统中无任何 session）时才创建新的。
        """
        try:
            sid = self._recent_or_new_session_id()
            if not sid:
                raise RuntimeError("无法创建会话")
            attachment_ids: list[str] = []
            if image is not None:
                filename = time.strftime("clipboard-%Y%m%d-%H%M%S.png")
                upload = self.client.upload_image(sid, image, filename)
                attachment = upload.get("attachment") or {}
                attachment_id = attachment.get("id")
                if not upload.get("ok") or not attachment_id:
                    raise RuntimeError("图片上传失败")
                attachment_ids.append(str(attachment_id))
            result = self.client.send_message(sid, text, attachment_ids)
            self._show_reply(result)
        except Exception:
            self.root.after(0, lambda: self._set_local_message("消息发送失败，请重试"))

    def _show_reply(self, result: dict | None) -> None:
        """从 /chat 响应中提取 reply，在气泡中显示。

        直接使用 HTTP 响应中的 reply 而非依赖轮询 /pet/state 的 notify
        状态，避免因本地消息遮蔽、轮询延迟或 notify TTL 过期导致显示失效。
        超过 200 字时不显示（由 _refresh_bubble 的远程状态显示占位提示）。
        """
        if not isinstance(result, dict):
            return
        reply = (result.get("reply") or "").strip()
        if reply and len(reply) <= _BUBBLE_DISPLAY_LIMIT:
            # Tkinter 非线程安全，需在主线程中更新 UI
            self.root.after(
                0, lambda r=reply: self._set_local_message(r, ttl=_REPLY_MESSAGE_TTL)
            )

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
        elif self._local_message is not None and time.time() >= self._local_message_until:
            # 本地消息刚过期，刷新气泡恢复远程状态
            self._local_message = None
            self._refresh_bubble()
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
        self._refresh_bubble()

    def _refresh_bubble(self) -> None:
        """根据本地消息（优先）或远程状态刷新气泡内容。

        保证同一时刻只显示一层文字，不会重叠。
        """
        now = time.time()
        # 本地消息未过期时优先显示
        if self._local_message and now < self._local_message_until:
            self._update_bubble(self._local_message, "", visible=True)
            return
        if self._local_message is not None and now >= self._local_message_until:
            self._local_message = None

        state = self._remote_state
        display_name = self.pet.get("displayName", "宠物")
        message = _strip_markdown(state.get("message") or "待命中")
        # 严格 200 字限制：超限时不显示原文，仅显示占位提示
        if len(message) > _BUBBLE_DISPLAY_LIMIT:
            message = _BUBBLE_OVERLIMIT_HINT
        task = state.get("task") or ""
        animation = state.get("animation", "idle")
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
        """更新气泡内容，根据文字量自适应宽度和高度，避免文字超出或重叠。

        - 宽度：用 font.measure 量算自然文字宽度，气泡宽度包裹文字，
          右侧无多余空白；超过 max_bubble_width 时自动换行。
        - 高度：底部固定在桌宠上方，文字增多时顶部向上延伸。
        - 排版：适当行间距(5px)、颜色对比度优化、左对齐顶部排列。
        - 通过 itemconfigure 原地更新，不创建新 Canvas item，避免重叠。
        """
        # 气泡不可见时隐藏所有元素
        item_state = "normal" if visible else "hidden"
        for item in (self._bubble, self._status_text, self._task_text):
            self.canvas.itemconfigure(item, state=item_state)
        if not visible:
            return

        # 更新文字内容
        self.canvas.itemconfigure(self._status_text, text=status)
        self.canvas.itemconfigure(self._task_text, text=task)
        self.canvas.itemconfigure(
            self._task_text,
            state="normal" if task else "hidden",
        )

        padding = self._px(10)
        gap = self._px(5)
        margin = self._px(10)

        # 1. 量算自然文字宽度（单行，不考虑换行）
        status_w = self._status_font.measure(status)
        task_w = self._task_font.measure(task) if task else 0
        max_text_w = max(status_w, task_w)

        # 2. 计算自适应气泡宽度
        max_bubble_w = self.window_width - 2 * margin
        min_bubble_w = self._px(50)
        bubble_w = min(max_bubble_w, max(min_bubble_w, max_text_w + 2 * padding))

        # 3. 设置文字换行宽度（超过气泡内宽时自动换行）
        text_wrap_w = bubble_w - 2 * padding
        self.canvas.itemconfigure(self._status_text, width=text_wrap_w)
        self.canvas.itemconfigure(self._task_text, width=text_wrap_w)

        # 4. 量算换行后的实际渲染高度
        self.canvas.update_idletasks()
        status_bbox = self.canvas.bbox(self._status_text)
        status_height = (
            status_bbox[3] - status_bbox[1] if status_bbox
            else self._status_font.metrics("linespace")
        )
        task_height = 0
        if task:
            task_bbox = self.canvas.bbox(self._task_text)
            task_height = (
                task_bbox[3] - task_bbox[1] if task_bbox
                else self._task_font.metrics("linespace")
            )

        # 文字总高度（status + gap + task）
        text_height = status_height
        if task:
            text_height += gap + task_height

        # 5. 气泡位置：底部固定在桌宠上方（间距 ≥12px），向上延伸
        bubble_bottom = self.pet_center_y - self._px(_BUBBLE_BOTTOM_OFFSET)
        min_height = self._px(32)
        bubble_height = max(min_height, text_height + 2 * padding)
        bubble_top = bubble_bottom - bubble_height
        # 安全约束：气泡顶部不得超出窗口顶部，否则圆角会被裁切成直角
        min_top = self._px(2)
        if bubble_top < min_top:
            bubble_top = min_top
            bubble_height = bubble_bottom - bubble_top

        # 6. 水平居中
        bubble_left = (self.window_width - bubble_w) // 2
        bubble_right = bubble_left + bubble_w

        self.canvas.coords(
            self._bubble,
            *_rounded_rectangle_points(
                bubble_left, bubble_top, bubble_right, bubble_bottom,
                self._px(14),
            ),
        )

        # 7. 文字定位（左对齐 + 顶部排列，自然向下延伸）
        text_left = bubble_left + padding
        start_y = bubble_top + padding
        self.canvas.coords(self._status_text, text_left, start_y)
        if task:
            self.canvas.coords(self._task_text, text_left, start_y + status_height + gap)

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
