"""
Клиент мессенджера 'ВОЛНА' на базе pywebview + Pystray.
- Мгновенное открытие из трея за 0 мс (фоновый pre-warm рендеринг)
- Растягивание окна во все 8 направлений и перемещение за шапку
- Поддержка кастомных курсоров из папки web/cursors/
- Иконка в панели задач, неразрывный TCP-клиент и неоновые уведомления
"""

from __future__ import annotations

import base64
import ctypes
import html
from html.parser import HTMLParser
import json
import os
import queue
import random
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw
import webview

if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("volna.messenger.client.2026")
    except Exception:
        pass

PORT: int = 12345
UDP_PORT: int = 12346

if getattr(sys, "frozen", False):
    RESOURCE_DIR: Path = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA_DIR: Path = Path(sys.executable).parent
else:
    RESOURCE_DIR: Path = Path(__file__).parent.resolve()
    DATA_DIR: Path = RESOURCE_DIR

WEB_DIR: Path = RESOURCE_DIR / "web"
DOWNLOADS_DIR: Path = DATA_DIR / "downloads"
CURSORS_DIR: Path = WEB_DIR / "cursors"
CONFIG_FILE: Path = DATA_DIR / "client_config.json"
ICON_PNG_PATH: Path = WEB_DIR / "icon.png"
ICON_ICO_PATH: Path = WEB_DIR / "icon.ico"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
CURSORS_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR.mkdir(parents=True, exist_ok=True)


def ensure_app_icons() -> Image.Image:
    img = Image.new("RGBA", (256, 256), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((12, 12, 244, 244), fill="#0a1322", outline="#35e0c8", width=12)
    draw.arc((50, 75, 206, 175), start=0, end=180, fill="#35e0c8", width=16)
    draw.arc((50, 105, 206, 205), start=180, end=360, fill="#4dc6ff", width=16)
    try:
        img.save(ICON_PNG_PATH, format="PNG")
        img.save(
            ICON_ICO_PATH,
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
    except Exception:
        pass
    return img


APP_ICON_IMAGE = ensure_app_icons()


class NeonToastEngine:
    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, str, bool]] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.root: tk.Tk | None = None

    def _loop(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self._check_queue()
        self.root.mainloop()

    def _check_queue(self) -> None:
        try:
            while not self._queue.empty():
                title, msg, is_file = self._queue.get_nowait()
                self._render_toast(title, msg, is_file)
        except Exception:
            pass
        if self.root:
            self.root.after(100, self._check_queue)

    def show(self, title: str, message: str, is_file: bool = False) -> None:
        self._queue.put((title, message, is_file))

    def _render_toast(self, title: str, message: str, is_file: bool) -> None:
        if not self.root:
            return
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        toast.configure(bg="#35e0c8")

        frame = tk.Frame(toast, bg="#0c1322", padx=14, pady=12)
        frame.pack(padx=1, pady=1, fill="both", expand=True)

        header_frame = tk.Frame(frame, bg="#0c1322")
        header_frame.pack(fill="x")

        icon_text = "📁 " if is_file else "🌊 "
        lbl_icon = tk.Label(header_frame, text=icon_text, font=("Segoe UI", 12), bg="#0c1322", fg="#35e0c8")
        lbl_icon.pack(side="left")

        lbl_title = tk.Label(header_frame, text=f"ВОЛНА · {title}", font=("Segoe UI", 10, "bold"), bg="#0c1322", fg="#35e0c8")
        lbl_title.pack(side="left", padx=4)

        lbl_close = tk.Label(header_frame, text="✕", font=("Segoe UI", 9, "bold"), bg="#0c1322", fg="#57637d", cursor="hand2")
        lbl_close.pack(side="right")
        lbl_close.bind("<Button-1>", lambda e: toast.destroy())

        clean_text = message if len(message) <= 95 else message[:92] + "..."
        lbl_msg = tk.Label(frame, text=clean_text, font=("Segoe UI", 9), bg="#0c1322", fg="#eef4fc", wraplength=280, justify="left")
        lbl_msg.pack(anchor="w", pady=(6, 0))

        def on_click(e: Any) -> None:
            restore_window()
            toast.destroy()

        for widget in (frame, lbl_icon, lbl_title, lbl_msg):
            widget.bind("<Button-1>", on_click)
            widget.config(cursor="hand2")

        toast.update_idletasks()
        sw = toast.winfo_screenwidth()
        sh = toast.winfo_screenheight()
        w = 320
        h = toast.winfo_reqheight()
        x = sw - w - 24
        y = sh - h - 56
        toast.geometry(f"{w}x{h}+{x}+{y}")

        toast.after(4500, lambda: self._safe_destroy(toast))

    def _safe_destroy(self, toast: tk.Toplevel) -> None:
        try:
            toast.destroy()
        except Exception:
            pass


toast_engine = NeonToastEngine()


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title: bool = False
        self.title: str = ""
        self.description: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            prop = attr_dict.get("property", "") or attr_dict.get("name", "")
            if prop.lower() in ("og:description", "description") and not self.description:
                self.description = attr_dict.get("content", "")
            elif prop.lower() == "og:title" and not self.title:
                self.title = attr_dict.get("content", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and not self.title:
            self.title = data.strip()


@dataclass(slots=True)
class AppConfig:
    nickname: str = field(default_factory=lambda: f"Волна_{random.randint(100, 999)}")

    @classmethod
    def load(cls) -> AppConfig:
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    return cls(nickname=json.load(f).get("nickname", cls().nickname))
            except Exception:
                pass
        config = cls()
        config.save()
        return config

    def save(self) -> None:
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"nickname": self.nickname}, f, ensure_ascii=False, indent=4)
        except Exception:
            pass


config = AppConfig.load()
tray_icon: pystray.Icon | None = None
main_window: webview.Window | None = None
is_window_focused: bool = False
cached_history: list[dict[str, Any]] = []

WIN_WIDTH: int = 1100
WIN_HEIGHT: int = 800
saved_window_x: int = 100
saved_window_y: int = 100


def run_js(js_code: str) -> None:
    if main_window:
        try:
            main_window.evaluate_js(js_code)
        except Exception:
            pass


def apply_taskbar_icon() -> None:
    if sys.platform != "win32":
        return
    for _ in range(25):
        hwnd = ctypes.windll.user32.FindWindowW(None, "ВОЛНА — Мессенджер")
        if hwnd and ctypes.windll.user32.IsWindow(hwnd):
            if ICON_ICO_PATH.exists():
                hicon_big = ctypes.windll.user32.LoadImageW(0, str(ICON_ICO_PATH), 1, 32, 32, 0x00000010)
                hicon_small = ctypes.windll.user32.LoadImageW(0, str(ICON_ICO_PATH), 1, 16, 16, 0x00000010)
                if hicon_big:
                    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)
                if hicon_small:
                    ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)
            break
        time.sleep(0.15)


class NetworkWorker(threading.Thread):
    def __init__(self, nickname: str) -> None:
        super().__init__(daemon=True)
        self.nickname: str = nickname
        self.running: bool = True
        self.connected: bool = False
        self.sock: socket.socket | None = None
        self._lock: threading.Lock = threading.Lock()
        self.last_known_server: tuple[str, int] | None = None

    def run(self) -> None:
        while self.running:
            if not self.connected:
                run_js("window.js_set_connection_status(false);")
                server_info = self.last_known_server or self._discover_server()
                if not server_info:
                    time.sleep(1.5)
                    continue

                host, port = server_info
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(3.0)
                    s.connect((host, port))
                    s.settimeout(None)

                    with self._lock:
                        self.sock = s
                        self.connected = True
                        self.last_known_server = (host, port)

                    print(f"[TCP] Подключено к {host}:{port}")
                    run_js("window.js_set_connection_status(true);")

                    self.send({"action": "register", "nickname": self.nickname})
                    self.send({"action": "get_history"})
                except Exception:
                    self.connected = False
                    self.last_known_server = None
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                    time.sleep(2)
                    continue

            try:
                msg = self._recv_msg()
                if msg is None:
                    raise ConnectionResetError()

                action = msg.get("action")
                match action:
                    case "file_download_response":
                        self._save_incoming_file(
                            msg.get("file_id", ""),
                            msg.get("filename", "file"),
                            msg.get("content", "")
                        )
                    case "history":
                        global cached_history
                        cached_history = msg.get("messages", [])
                        payload = json.dumps(cached_history)
                        run_js(f"window.js_load_history({payload});")
                        for m in cached_history:
                            if m.get("action") == "file":
                                self._auto_download_if_needed(m.get("file_id"), m.get("filename"))
                    case "msg":
                        cached_history.append(msg)
                        payload = json.dumps(msg)
                        run_js(f"window.js_add_message({payload});")
                        self._handle_incoming_notification(msg)
                    case "file":
                        cached_history.append(msg)
                        payload = json.dumps(msg)
                        run_js(f"window.js_add_message({payload});")
                        self._auto_download_if_needed(msg.get("file_id"), msg.get("filename"))
                        self._handle_incoming_notification(msg, is_file=True)
                    case "reaction_update":
                        msg_id = msg.get("msg_id")
                        reactions = msg.get("reactions", {})
                        for h_msg in cached_history:
                            if h_msg.get("id") == msg_id or h_msg.get("file_id") == msg_id:
                                h_msg["reactions"] = reactions
                                break
                        payload_rx = json.dumps(reactions)
                        run_js(f"window.js_update_reactions('{msg_id}', {payload_rx});")
            except Exception:
                self.connected = False
                run_js("window.js_set_connection_status(false);")
                with self._lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                        self.sock = None
                time.sleep(2)

    def _handle_incoming_notification(self, msg: dict[str, Any], is_file: bool = False) -> None:
        sender = msg.get("sender", "Аноним")
        if sender == self.nickname:
            return

        if not main_window or not is_window_focused:
            if is_file:
                toast_engine.show(sender, msg.get("filename", "Новый файл"), is_file=True)
            else:
                toast_engine.show(sender, msg.get("text", ""))

    def _auto_download_if_needed(self, file_id: str | None, filename: str | None) -> None:
        if not file_id or not filename:
            return
        local_path = DOWNLOADS_DIR / f"{file_id}_{filename}"
        if not local_path.exists():
            self.send({"action": "file_download", "file_id": file_id})
        else:
            rel_url = f"downloads/{file_id}_{filename}"
            clean_path = str(local_path).replace("\\", "/")
            run_js(f"window.js_on_file_ready('{file_id}', '{filename}', '{clean_path}', '{rel_url}');")

    def _save_incoming_file(self, file_id: str, filename: str, content_b64: str) -> None:
        try:
            local_path = DOWNLOADS_DIR / f"{file_id}_{filename}"
            with open(local_path, "wb") as f:
                f.write(base64.b64decode(content_b64))
            rel_url = f"downloads/{file_id}_{filename}"
            clean_path = str(local_path).replace("\\", "/")
            run_js(f"window.js_on_file_ready('{file_id}', '{filename}', '{clean_path}', '{rel_url}');")
        except Exception as e:
            print(f"[!] Ошибка сохранения: {e}", file=sys.stderr)

    def send(self, data_dict: dict[str, Any]) -> bool:
        if not self.connected or not self.sock:
            return False
        try:
            with self._lock:
                payload = json.dumps(data_dict).encode("utf-8")
                header = struct.pack("!I", len(payload))
                self.sock.sendall(header + payload)
            return True
        except Exception:
            return False

    def _recv_msg(self) -> dict[str, Any] | None:
        if not self.sock:
            return None
        header = self.sock.recv(4)
        if not header:
            return None
        length = struct.unpack("!I", header)[0]
        data = bytearray()
        while len(data) < length:
            packet = self.sock.recv(length - len(data))
            if not packet:
                return None
            data.extend(packet)
        return json.loads(data.decode("utf-8"))

    def _discover_server(self) -> tuple[str, int] | None:
        if self._ping_tcp("127.0.0.1"):
            return "127.0.0.1", PORT

        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if hasattr(socket, "SO_REUSEADDR"):
            udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            udp_sock.bind(("", UDP_PORT))
            udp_sock.settimeout(1.2)
            data, addr = udp_sock.recvfrom(1024)
            if json.loads(data.decode("utf-8")).get("app") == "local_messenger":
                return addr[0], PORT
        except Exception:
            pass
        finally:
            udp_sock.close()

        return None

    def _ping_tcp(self, ip: str) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                s.connect((ip, PORT))
                payload = json.dumps({"action": "ping"}).encode("utf-8")
                s.sendall(struct.pack("!I", len(payload)) + payload)
                s.settimeout(0.5)
                header = s.recv(4)
                if header:
                    resp_len = struct.unpack("!I", header)[0]
                    return json.loads(s.recv(resp_len).decode("utf-8")).get("action") == "pong"
        except Exception:
            pass
        return False


worker = NetworkWorker(config.nickname)


class JsApi:
    def py_frontend_ready(self) -> dict[str, Any]:
        global saved_window_x, saved_window_y
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            saved_window_x = max(40, (sw - WIN_WIDTH) // 2)
            saved_window_y = max(40, (sh - WIN_HEIGHT) // 2)

        threading.Thread(target=apply_taskbar_icon, daemon=True).start()
        if worker.connected:
            worker.send({"action": "get_history"})

        return {
            "nickname": config.nickname,
            "connected": worker.connected,
            "history": cached_history,
            "custom_cursors": self.py_get_custom_cursors()
        }

    def py_get_custom_cursors(self) -> dict[str, str]:
        cursors = {}
        if not CURSORS_DIR.exists():
            return cursors

        mapping = {
            "default": ["default.png", "default.cur", "cursor.png", "cursor.cur"],
            "pointer": ["pointer.png", "pointer.cur", "hand.png", "hand.cur"],
            "text": ["text.png", "text.cur", "ibeam.png", "beam.png"],
            "res_n": ["res-n.png", "n-resize.png", "res_n.png"],
            "res_s": ["res-s.png", "s-resize.png", "res_s.png"],
            "res_e": ["res-e.png", "e-resize.png", "res_e.png"],
            "res_w": ["res-w.png", "w-resize.png", "res_w.png"],
            "res_nw": ["res-nw.png", "nw-resize.png", "res_nw.png"],
            "res_ne": ["res-ne.png", "ne-resize.png", "res_ne.png"],
            "res_sw": ["res-sw.png", "sw-resize.png", "res_sw.png"],
            "res_se": ["res-se.png", "se-resize.png", "res_se.png"],
        }

        for role, filenames in mapping.items():
            for fn in filenames:
                p = CURSORS_DIR / fn
                if p.exists():
                    cursors[role] = f"cursors/{fn}"
                    break

        return cursors

    def py_get_window_bounds(self) -> dict[str, int]:
        if main_window:
            return {
                "x": int(main_window.x),
                "y": int(main_window.y),
                "w": int(main_window.width),
                "h": int(main_window.height)
            }
        return {"x": saved_window_x, "y": saved_window_y, "w": WIN_WIDTH, "h": WIN_HEIGHT}

    def py_move_window(self, x: int, y: int) -> None:
        global saved_window_x, saved_window_y
        if main_window:
            saved_window_x = int(x)
            saved_window_y = int(y)
            main_window.move(saved_window_x, saved_window_y)

    def py_resize_window(self, x: int, y: int, w: int, h: int) -> None:
        global saved_window_x, saved_window_y
        if main_window:
            saved_window_x = int(x)
            saved_window_y = int(y)
            main_window.move(saved_window_x, saved_window_y)
            main_window.resize(int(w), int(h))

    def py_hide_window(self) -> None:
        global is_window_focused
        is_window_focused = False
        if main_window:
            main_window.move(-15000, -15000)

    def py_minimize_window(self) -> None:
        global is_window_focused
        is_window_focused = False
        if main_window:
            main_window.minimize()

    def py_set_window_focus(self, focused: bool) -> None:
        global is_window_focused
        is_window_focused = focused

    def py_set_nickname(self, new_nickname: str) -> None:
        nick = new_nickname.strip() or "User"
        config.nickname = nick
        config.save()
        worker.nickname = nick
        if worker.connected:
            worker.send({"action": "register", "nickname": nick})

    def py_send_text(self, text: str) -> None:
        if not text.strip():
            return
        worker.send({
            "action": "msg",
            "text": text,
            "time": time.strftime("%H:%M")
        })

    def py_upload_base64_file(self, filename: str, content_b64: str) -> None:
        worker.send({
            "action": "file_upload",
            "filename": filename,
            "content": content_b64,
            "time": time.strftime("%H:%M")
        })

    def py_select_and_send_file(self) -> None:
        def _picker() -> None:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            filepath = filedialog.askopenfilename()
            root.destroy()

            if not filepath or not os.path.isfile(filepath):
                return

            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                b64_data = base64.b64encode(f.read()).decode("utf-8")

            self.py_upload_base64_file(filename, b64_data)

        threading.Thread(target=_picker, daemon=True).start()

    def py_toggle_reaction(self, msg_id: str, emoji: str) -> None:
        worker.send({
            "action": "reaction",
            "msg_id": msg_id,
            "emoji": emoji
        })

    def py_open_link(self, url: str) -> None:
        """Открытие веб-ссылки в браузере по умолчанию."""
        target_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
        try:
            webbrowser.open(target_url, new=2)
        except Exception as e:
            run_js(f"window.js_show_toast('Не удалось открыть ссылку: {e}');")

    def py_open_file(self, filepath: str) -> None:
        if filepath.startswith(("http://", "https://")):
            self.py_open_link(filepath)
            return

        path = os.path.abspath(filepath)
        if not os.path.exists(path):
            run_js("window.js_show_toast('Файл ещё загружается...');")
            return

        try:
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            run_js(f"window.js_show_toast('Ошибка открытия: {e}');")

    def py_open_folder(self, filepath: str) -> None:
        path = os.path.abspath(filepath)
        try:
            if sys.platform == "win32":
                subprocess.Popen(f'explorer /select,"{path}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception as e:
            run_js(f"window.js_show_toast('Ошибка открытия папки: {e}');")

    def py_fetch_link_preview(self, url: str) -> None:
        def _fetch() -> None:
            target_url = url if url.startswith("http") else f"https://{url}"
            try:
                req = urllib.request.Request(
                    target_url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    charset = resp.headers.get_content_charset() or "utf-8"
                    content = resp.read(65536).decode(charset, errors="replace")

                parser = MetadataParser()
                parser.feed(content)
                domain = urllib.parse.urlparse(target_url).netloc
                title = parser.title or domain
                desc = parser.description or "Нажмите, чтобы открыть ссылку"

                payload = json.dumps({
                    "title": html.unescape(title),
                    "domain": domain,
                    "desc": html.unescape(desc)
                })
                run_js(f"window.js_on_link_preview_ready('{url}', {payload});")
            except Exception:
                domain = urllib.parse.urlparse(target_url).netloc
                payload = json.dumps({
                    "title": domain or url,
                    "domain": domain,
                    "desc": "Внешний веб-ресурс"
                })
                run_js(f"window.js_on_link_preview_ready('{url}', {payload});")

        threading.Thread(target=_fetch, daemon=True).start()


def restore_window() -> None:
    global is_window_focused
    is_window_focused = True
    if main_window:
        main_window.move(saved_window_x, saved_window_y)
        main_window.restore()
        if sys.platform == "win32":
            hwnd = ctypes.windll.user32.FindWindowW(None, "ВОЛНА — Мессенджер")
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)


def quit_application() -> None:
    global tray_icon
    worker.running = False
    if tray_icon:
        tray_icon.stop()
    if main_window:
        main_window.destroy()
    os._exit(0)


def setup_tray() -> None:
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Открыть ВОЛНУ", restore_window, default=True),
        pystray.MenuItem("Выход", quit_application)
    )
    tray_icon = pystray.Icon("WaveMessenger", APP_ICON_IMAGE, "ВОЛНА — Мессенджер", menu)
    tray_icon.run()


if __name__ == "__main__":
    worker.start()
    threading.Thread(target=setup_tray, daemon=True).start()

    api = JsApi()
    html_path = WEB_DIR / "index.html"

    main_window = webview.create_window(
        title="ВОЛНА — Мессенджер",
        url=str(html_path),
        js_api=api,
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        x=-15000,
        y=-15000,
        min_size=(680, 520),
        frameless=True,
        resizable=True,
        easy_drag=False
    )

    toast_engine.show("Приложение запущено", "ВОЛНА работает в трее. Кликните для открытия.")

    webview.start(debug=False)