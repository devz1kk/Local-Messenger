"""
Клиент мессенджера 'ВОЛНА' на базе pywebview.
Модуль самообновления через GitHub Contents API с валидацией хешей и логированием.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import logging
import os
import random
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pystray
from PIL import Image, ImageDraw
import webview

# =====================================================================
# КОНФИГУРАЦИЯ И ПУТИ ФАЙЛОВОЙ СИСТЕМЫ
# =====================================================================
DEBUG: bool = False

SW_HIDE: int = 0
SW_RESTORE: int = 9
SW_SHOWNOACTIVATE: int = 4

HWND_TOPMOST: int = -1
SWP_NOACTIVATE: int = 0x0010
SWP_SHOWWINDOW: int = 0x0040

if sys.platform == "win32":
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("volna.messenger.client.2026")
    except Exception:
        pass

PORT: int = 12345
UDP_PORT: int = 12346
GITHUB_API_EXE_URL: str = "https://api.github.com/repos/devz1kk/Local-Messenger/contents/dist/main.exe"

# Определение корневых директорий
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
PENDING_UPDATE_FILE: Path = DATA_DIR / "update_pending.exe"
UPDATE_LOG_FILE: Path = DATA_DIR / "volna_update.log"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
CURSORS_DIR.mkdir(parents=True, exist_ok=True)
WEB_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================================
# СИСТЕМА ЛОГИРОВАНИЯ ОБНОВЛЕНИЙ (ТОЛЬКО ПРОЦЕСС АПДЕЙТА)
# =====================================================================
def setup_update_logger() -> logging.Logger:
    """Инициализирует выделенный логгер для подсистемы обновления (1 сессия = 1 файл)."""
    logger = logging.getLogger("VolnaUpdater")
    logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    logger.handlers.clear()

    # Перезапись файла лога при каждом запуске приложения
    file_handler = logging.FileHandler(UPDATE_LOG_FILE, mode="w", encoding="utf-8")
    formatter = logging.Formatter(
        fmt="[%(asctime)s.%(msecs)03d] [%(levelname)-8s] [%(threadName)-16s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if DEBUG:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


update_log = setup_update_logger()


# =====================================================================
# ПОДСИСТЕМА АВТООБНОВЛЕНИЯ
# =====================================================================
def calculate_git_blob_sha(filepath: Path) -> str:
    """
    Вычисляет Git Object SHA-1 хеш: sha1("blob " + filesize + "\0" + content).
    Именно этот формат возвращает GitHub Contents API в поле 'sha'.
    """
    if not filepath.is_file():
        update_log.warning(f"Файл для расчета хеша не найден: {filepath}")
        return ""
    try:
        size = filepath.stat().st_size
        h = hashlib.sha1()
        h.update(f"blob {size}\0".encode("utf-8"))
        
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
                
        calculated_sha = h.hexdigest()
        update_log.debug(f"Хеш Git Blob для '{filepath.name}' ({size} байт): {calculated_sha}")
        return calculated_sha
    except Exception as exc:
        update_log.error(f"Сбой при расчете Git Blob хеша файла {filepath}: {exc}", exc_info=True)
        return ""


def apply_update_and_restart(new_exe_path: Path) -> None:
    """
    Генерирует и запускает batch-скрипт, замещающий текущий бинарник новым файлом.
    Осуществляет мониторинг завершения процесса по PID и очистку окружения PyInstaller.
    """
    update_log.info("Запуск процедуры применения обновления и перезапуска...")
    current_exe = Path(sys.executable)

    if not getattr(sys, "frozen", False):
        update_log.info(
            f"[DEV MODE] Приложение запущено из исходников (python main.py). "
            f"Физическая замена '{current_exe.name}' отменена для защиты среды разработчика."
        )
        return

    pid = os.getpid()
    updater_bat = DATA_DIR / "_apply_update.bat"
    update_log.info(f"Формирование служебного скрипта обновления: {updater_bat.name} (Целевой PID: {pid})")

    # Скрипт ожидает завершения текущего процесса, сбрасывает PyInstaller ENV и перезапускает .exe
    bat_script = f"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
set TARGET_PID={pid}

:wait_loop
tasklist /fi "PID eq %TARGET_PID%" | findstr "%TARGET_PID%" >nul
if not errorlevel 1 (
    timeout /t 1 /nobreak >nul
    goto wait_loop
)

:: Сброс служебных дескрипторов загрузчика PyInstaller
set _MEIPASS2=
set _PYI_ARCHIVE_FILE=
set _PYI_APPLICATION_HOME_DIR=
set _PYI_SPLASH_IPC=

:: Замена бинарника с повторными попытками при файловых блокировках
move /y "{new_exe_path.name}" "{current_exe.name}" >nul
if errorlevel 1 (
    timeout /t 1 /nobreak >nul
    copy /y "{new_exe_path.name}" "{current_exe.name}" >nul
    del /f /q "{new_exe_path.name}" >nul
)

:: Запуск обновленного приложения
start "" "{current_exe.name}"

:: Удаление самого bat-файла
del "%~f0"
"""

    try:
        with open(updater_bat, "w", encoding="utf-8") as f:
            f.write(bat_script)
        update_log.info("Служебный batch-скрипт успешно записан на диск.")

        # Очистка текущих переменных окружения для дочернего интерпретатора cmd
        clean_env = {
            k: v for k, v in os.environ.items()
            if not k.startswith("_PYI") and k != "_MEIPASS2"
        }

        update_log.info("Старт внешнего процесса cmd.exe с флагом CREATE_NO_WINDOW...")
        subprocess.Popen(
            ["cmd.exe", "/c", str(updater_bat)],
            env=clean_env,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
            close_fds=True
        )
        update_log.info("Основной процесс завершает работу для выполнения горячей замены (os._exit).")
        os._exit(0)
    except Exception as exc:
        update_log.critical(f"Критическая ошибка при запуске batch-обновления: {exc}", exc_info=True)


def check_and_apply_pending_update() -> bool:
    """
    Проверяет наличие отложенного обновления при старте клиента.
    Если файл найден и валиден — мгновенно применяет его до загрузки UI.
    """
    update_log.info("Проверка наличия отложенного обновления (update_pending.exe)...")
    if not PENDING_UPDATE_FILE.exists():
        update_log.info("Отложенных обновлений не обнаружено. Продолжение штатного запуска.")
        return False

    update_log.info("Обнаружен файл отложенного обновления. Проверка размера и доступности...")
    size = PENDING_UPDATE_FILE.stat().st_size
    if size < 1024 * 1024:  # Полноценный бинарник не может весить меньше 1 МБ
        update_log.warning(f"Файл {PENDING_UPDATE_FILE.name} поврежден или имеет подозрительный размер ({size} байт). Удаление.")
        PENDING_UPDATE_FILE.unlink(missing_ok=True)
        return False

    if not getattr(sys, "frozen", False):
        update_log.info("[DEV MODE] Отложенное обновление обнаружено, но замена пропущена (режим разработки).")
        return False

    update_log.info("Отложенное обновление валидно. Переход к тихой установке...")
    apply_update_and_restart(PENDING_UPDATE_FILE)
    return True


class AutoUpdater(threading.Thread):
    """Фоновый воркер опроса GitHub API, скачивания и верификации релизов."""
    
    def __init__(self) -> None:
        super().__init__(name="UpdaterThread", daemon=True)
        self.remote_sha: str = ""
        self.download_url: str = ""

    def run(self) -> None:
        update_log.info("Поток фонового обновления запущен. Задержка 3.5 сек перед первым запросом...")
        time.sleep(3.5)

        try:
            current_exe = Path(sys.executable) if getattr(sys, "frozen", False) else (DATA_DIR / "dist" / "main.exe")
            update_log.info(f"Локальный исполняемый файл: {current_exe}")

            local_sha = calculate_git_blob_sha(current_exe) if current_exe.exists() else ""
            update_log.info(f"Текущий локальный хеш:  {local_sha or 'ОТСУТСТВУЕТ'}")

            update_log.info(f"Запрос метаданных к GitHub API: {GITHUB_API_EXE_URL}")
            ctx = ssl.create_default_context()
            req = urllib.request.Request(
                GITHUB_API_EXE_URL,
                headers={
                    "User-Agent": "Volna-Client-Updater",
                    "Accept": "application/vnd.github.v3+json"
                }
            )

            with urllib.request.urlopen(req, timeout=10.0, context=ctx) as resp:
                status = resp.status
                raw_body = resp.read().decode("utf-8")
                update_log.info(f"Ответ GitHub API получен. HTTP Status: {status}")
                meta = json.loads(raw_body)

            self.remote_sha = meta.get("sha", "")
            self.download_url = meta.get("download_url", "")
            file_size = meta.get("size", 0)

            update_log.info(f"Удаленный Git SHA:      {self.remote_sha}")
            update_log.info(f"Размер файла на сервере: {file_size} байт")
            update_log.info(f"URL прямой загрузки:    {self.download_url}")

            if not self.remote_sha or not self.download_url:
                update_log.warning("В ответе GitHub API отсутствует 'sha' или 'download_url'. Обновление остановлено.")
                return

            if local_sha == self.remote_sha:
                update_log.info("Хеши полностью совпадают. Установлена актуальная версия приложения.")
                return

            update_log.info("ОБНАРУЖЕНА НОВАЯ ВЕРСИЯ! Локальный и удаленный хеши различаются. Начинается загрузка...")
            self._download_and_verify()

        except urllib.error.HTTPError as http_err:
            if http_err.code == 403:
                update_log.error("Превышен лимит запросов GitHub API (Rate Limit Exceeded: 60 запр/час).")
            else:
                update_log.error(f"Сетевая ошибка HTTP при проверке обновления: {http_err.code} {http_err.reason}")
        except Exception as exc:
            update_log.error(f"Непредвиденная ошибка при проверке обновления: {exc}", exc_info=True)

    def _download_and_verify(self) -> None:
        """Потоковая загрузка бинарника с валидацией контрольной суммы."""
        temp_dest = PENDING_UPDATE_FILE
        update_log.info(f"Целевой файл для скачивания: {temp_dest}")

        try:
            req = urllib.request.Request(self.download_url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            start_time = time.time()
            downloaded_bytes = 0

            with urllib.request.urlopen(req, timeout=90.0) as resp, open(temp_dest, "wb") as out_file:
                while chunk := resp.read(65536):
                    out_file.write(chunk)
                    downloaded_bytes += len(chunk)

            duration = time.time() - start_time
            update_log.info(
                f"Загрузка завершена: получено {downloaded_bytes} байт за {duration:.2f} сек. "
                f"({downloaded_bytes / 1024 / 1024 / max(duration, 0.001):.2f} МБ/с)"
            )

            # Валидация скачанного файла
            update_log.info("Запуск валидации целостности полученного файла...")
            downloaded_sha = calculate_git_blob_sha(temp_dest)
            update_log.info(f"Хеш скачанного файла:  {downloaded_sha}")
            update_log.info(f"Ожидаемый удаленный:   {self.remote_sha}")

            if downloaded_sha != self.remote_sha:
                update_log.critical("НЕСООТВЕТСТВИЕ ХЕШЕЙ! Скачанный файл поврежден. Удаление файла.")
                temp_dest.unlink(missing_ok=True)
                return

            update_log.info("Валидация успешна! Хеши идентичны. Отображение модального окна обновления (update.html)...")
            show_update_window()

        except Exception as exc:
            update_log.error(f"Сбой в процессе загрузки бинарного обновления: {exc}", exc_info=True)
            if temp_dest.exists():
                temp_dest.unlink(missing_ok=True)


# =====================================================================
# WIN32 ГРАФИЧЕСКАЯ МАСКА И ОКНА PYWEBVIEW
# =====================================================================
TOAST_W, TOAST_H = 340, 110
MODAL_W, MODAL_H = 430, 160

main_window: webview.Window | None = None
toast_window: webview.Window | None = None
update_window: webview.Window | None = None

toast_timer: threading.Timer | None = None
last_toast_payload: dict[str, Any] = {}


def apply_pixel_perfect_mask(hwnd: int, radius_css: int = 20) -> None:
    """Аппаратно отсекает острые углы у окна и дочерних слоев WebView2."""
    if sys.platform != "win32" or not hwnd:
        return

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return

    dpi = user32.GetDpiForWindow(hwnd) if hasattr(user32, "GetDpiForWindow") else 96
    scale = dpi / 96.0 if dpi else 1.0
    diameter = int(radius_css * scale * 2)

    rgn_parent = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, diameter, diameter)
    user32.SetWindowRgn(hwnd, rgn_parent, True)

    def _enum_children(child_hwnd: int, _: int) -> bool:
        c_rect = ctypes.wintypes.RECT()
        user32.GetClientRect(child_hwnd, ctypes.byref(c_rect))
        cw = c_rect.right - c_rect.left
        ch = c_rect.bottom - c_rect.top
        if cw > 0 and ch > 0:
            c_rgn = gdi32.CreateRoundRectRgn(0, 0, cw + 1, ch + 1, diameter, diameter)
            user32.SetWindowRgn(child_hwnd, c_rgn, True)
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumChildWindows(hwnd, WNDENUMPROC(_enum_children), 0)


def get_screen_resolution() -> tuple[int, int]:
    if sys.platform == "win32":
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    return 1920, 1080


def set_taskbar_visibility(visible: bool) -> None:
    if sys.platform != "win32":
        return
    hwnd = ctypes.windll.user32.FindWindowW(None, "ВОЛНА — Мессенджер")
    if hwnd and ctypes.windll.user32.IsWindow(hwnd):
        ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE if visible else SW_HIDE)


def show_toast_window(msg_payload: dict[str, Any]) -> None:
    """Отображает уведомление в правом нижнем углу над панелью задач."""
    global toast_timer, last_toast_payload
    if not toast_window:
        return

    last_toast_payload = msg_payload
    if toast_timer:
        toast_timer.cancel()

    sw, sh = get_screen_resolution()
    target_x = sw - TOAST_W - 20
    target_y = sh - TOAST_H - 50

    toast_window.move(target_x, target_y)
    toast_window.show()

    try:
        toast_window.evaluate_js(f"setToastPayload({json.dumps(msg_payload)});")
    except Exception:
        pass

    if sys.platform == "win32":
        hwnd = ctypes.windll.user32.FindWindowW(None, "VolnaToastWidget")
        if hwnd:
            apply_pixel_perfect_mask(hwnd, radius_css=20)
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, target_x, target_y, TOAST_W, TOAST_H, SWP_NOACTIVATE | SWP_SHOWWINDOW
            )
            threading.Timer(0.04, lambda: apply_pixel_perfect_mask(hwnd, radius_css=20)).start()
            threading.Timer(0.12, lambda: apply_pixel_perfect_mask(hwnd, radius_css=20)).start()

    toast_timer = threading.Timer(5.5, hide_toast_window)
    toast_timer.daemon = True
    toast_timer.start()


def hide_toast_window() -> None:
    if toast_window:
        toast_window.hide()


def show_update_window() -> None:
    """Отображает центрированное окно предложения обновления."""
    update_log.info("Отображение модального окна обновления пользователю.")
    if not update_window:
        update_log.warning("Окно update_window не инициализировано.")
        return

    sw, sh = get_screen_resolution()
    target_x = (sw - MODAL_W) // 2
    target_y = (sh - MODAL_H) // 2

    update_window.move(target_x, target_y)
    update_window.show()

    if sys.platform == "win32":
        hwnd = ctypes.windll.user32.FindWindowW(None, "VolnaUpdateModal")
        if hwnd:
            apply_pixel_perfect_mask(hwnd, radius_css=20)
            ctypes.windll.user32.SetWindowPos(
                hwnd, HWND_TOPMOST, target_x, target_y, MODAL_W, MODAL_H, SWP_SHOWWINDOW
            )
            ctypes.windll.user32.ShowWindow(hwnd, SW_RESTORE)
            threading.Timer(0.04, lambda: apply_pixel_perfect_mask(hwnd, radius_css=20)).start()
            threading.Timer(0.12, lambda: apply_pixel_perfect_mask(hwnd, radius_css=20)).start()


def hide_update_window() -> None:
    if update_window:
        update_window.hide()


# =====================================================================
# JS-API ДЛЯ ВСПЛЫВАЮЩИХ И МОДАЛЬНЫХ ОКОН
# =====================================================================
class ToastJsApi:
    def py_get_toast_payload(self) -> dict[str, Any]:
        return last_toast_payload

    def py_on_toast_clicked(self) -> None:
        hide_toast_window()
        restore_window()

    def py_on_toast_close(self) -> None:
        hide_toast_window()


class UpdateJsApi:
    def py_apply_update(self) -> None:
        """Пользователь нажал 'Установить сейчас'."""
        update_log.info("Пользователь выбрал 'Установить сейчас' в модальном окне.")
        hide_update_window()
        apply_update_and_restart(PENDING_UPDATE_FILE)

    def py_dismiss_update(self) -> None:
        """Пользователь нажал 'Позже'."""
        update_log.info(
            "Пользователь нажал 'Позже'. Файл обновления сохранен как 'update_pending.exe'. "
            "Замена выполнится автоматически при следующем старте программы."
        )
        hide_update_window()


# =====================================================================
# РЕСУРСЫ, ИКОНКИ И КОНФИГУРАЦИЯ МЕССЕНДЖЕРА
# =====================================================================
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


# =====================================================================
# СЕТЕВОЙ КЛИЕНТ
# =====================================================================
class NetworkWorker(threading.Thread):
    def __init__(self, nickname: str) -> None:
        super().__init__(name="NetworkWorker", daemon=True)
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

                match msg.get("action"):
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
                        if not is_window_focused and msg.get("sender") != self.nickname:
                            show_toast_window(msg)
                    case "file":
                        cached_history.append(msg)
                        payload = json.dumps(msg)
                        run_js(f"window.js_add_message({payload});")
                        self._auto_download_if_needed(msg.get("file_id"), msg.get("filename"))
                        if not is_window_focused and msg.get("sender") != self.nickname:
                            local_p = DOWNLOADS_DIR / f"{msg.get('file_id')}_{msg.get('filename')}"
                            if local_p.exists():
                                msg["content_url"] = str(local_p).replace("\\", "/")
                            show_toast_window(msg)
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
        except Exception:
            pass

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


# =====================================================================
# JS-API ГЛАВНОГО ОКНА
# =====================================================================
class JsApi:
    def py_frontend_ready(self) -> dict[str, Any]:
        global saved_window_x, saved_window_y
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            saved_window_x = max(40, (sw - WIN_WIDTH) // 2)
            saved_window_y = max(40, (sh - WIN_HEIGHT) // 2)

        if not is_window_focused:
            set_taskbar_visibility(False)

        if worker.connected:
            worker.send({"action": "get_history"})

        return {
            "nickname": config.nickname,
            "connected": worker.connected,
            "history": cached_history
        }

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
        set_taskbar_visibility(False)

    def py_minimize_window(self) -> None:
        global is_window_focused
        is_window_focused = False
        if main_window:
            main_window.minimize()

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
        worker.send({"action": "msg", "text": text, "time": time.strftime("%H:%M")})

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

            worker.send({
                "action": "file_upload",
                "filename": filename,
                "content": b64_data,
                "time": time.strftime("%H:%M")
            })

        threading.Thread(target=_picker, daemon=True).start()

    def py_upload_base64_file(self, filename: str, content_b64: str) -> None:
        if not filename or not content_b64:
            return
        worker.send({
            "action": "file_upload",
            "filename": filename,
            "content": content_b64,
            "time": time.strftime("%H:%M")
        })

    def py_open_file(self, filepath: str) -> None:
        path = os.path.abspath(filepath)
        if os.path.exists(path):
            if sys.platform == "win32":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])

    def py_open_folder(self, filepath: str) -> None:
        path = os.path.abspath(filepath)
        if os.path.exists(path):
            if sys.platform == "win32":
                subprocess.Popen(f'explorer /select,"{path}"')
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def py_open_link(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)

    def py_set_window_focus(self, focused: bool) -> None:
        global is_window_focused
        is_window_focused = focused


def restore_window() -> None:
    global is_window_focused
    is_window_focused = True
    set_taskbar_visibility(True)
    if main_window:
        main_window.move(saved_window_x, saved_window_y)
        main_window.restore()
        if sys.platform == "win32":
            hwnd = ctypes.windll.user32.FindWindowW(None, "ВОЛНА — Мессенджер")
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)


def quit_application() -> None:
    global tray_icon
    update_log.info("Завершение работы приложения пользователем.")
    worker.running = False
    if tray_icon:
        tray_icon.stop()
    if main_window:
        main_window.destroy()
    if toast_window:
        toast_window.destroy()
    if update_window:
        update_window.destroy()
    os._exit(0)


def setup_tray() -> None:
    global tray_icon
    menu = pystray.Menu(
        pystray.MenuItem("Открыть ВОЛНУ", restore_window, default=True),
        pystray.MenuItem("Выход", quit_application)
    )
    tray_icon = pystray.Icon("WaveMessenger", APP_ICON_IMAGE, "ВОЛНА — Мессенджер", menu)
    tray_icon.run()


# =====================================================================
# ТОЧКА ВХОДА (ENTRYPOINT)
# =====================================================================
if __name__ == "__main__":
    update_log.info("=" * 70)
    update_log.info(f"СТАРТ СЕССИИ МЕССЕНДЖЕРА 'ВОЛНА' (PID: {os.getpid()})")
    update_log.info(f"Платформа: {sys.platform} | Python: {sys.version.split()[0]}")
    update_log.info(f"Режим сборки (frozen): {getattr(sys, 'frozen', False)}")
    update_log.info(f"Рабочая директория (DATA_DIR): {DATA_DIR}")
    update_log.info("=" * 70)

    # 1. Проверка отложенного обновления от прошлого запуска (если юзер нажимал "Позже")
    if check_and_apply_pending_update():
        sys.exit(0)

    # 2. Запуск фоновых сервисов
    worker.start()
    threading.Thread(target=setup_tray, daemon=True, name="TrayThread").start()
    AutoUpdater().start()

    sw, sh = get_screen_resolution()

    # 3. Инициализация окон PyWebView
    main_window = webview.create_window(
        title="ВОЛНА — Мессенджер",
        url=str((WEB_DIR / "index.html").resolve()),
        js_api=JsApi(),
        width=WIN_WIDTH,
        height=WIN_HEIGHT,
        x=max(40, (sw - WIN_WIDTH) // 2),
        y=max(40, (sh - WIN_HEIGHT) // 2),
        min_size=(680, 520),
        frameless=True,
        resizable=True,
        easy_drag=False,
        hidden=True
    )

    toast_window = webview.create_window(
        title="VolnaToastWidget",
        url=str((WEB_DIR / "toast.html").resolve()),
        js_api=ToastJsApi(),
        width=TOAST_W,
        height=TOAST_H,
        x=sw - TOAST_W - 20,
        y=sh - TOAST_H - 50,
        frameless=True,
        easy_drag=False,
        on_top=True,
        hidden=True
    )

    update_window = webview.create_window(
        title="VolnaUpdateModal",
        url=str((WEB_DIR / "update.html").resolve()),
        js_api=UpdateJsApi(),
        width=MODAL_W,
        height=MODAL_H,
        x=(sw - MODAL_W) // 2,
        y=(sh - MODAL_H) // 2,
        frameless=True,
        easy_drag=False,
        on_top=True,
        hidden=True
    )

    def _welcome() -> None:
        time.sleep(1.2)
        show_toast_window({
            "sender": "ВОЛНА",
            "text": "Приложение запущено и работает в трее.",
            "time": time.strftime("%H:%M")
        })

    threading.Thread(target=_welcome, daemon=True, name="WelcomeToast").start()

    # 4. Запуск цикла событий графического интерфейса
    webview.start(debug=DEBUG)