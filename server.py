"""
Сервер локального мессенджера 'ВОЛНА' с синхронизацией истории и реакций.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

PORT: int = 12345
UDP_PORT: int = 12346


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent.resolve()
    return Path(__file__).parent.resolve()


BASE_DIR: Path = get_base_dir()
UPLOAD_DIR: Path = BASE_DIR / "server_uploads"
HISTORY_FILE: Path = BASE_DIR / "history.json"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

clients: dict[socket.socket, str] = {}
clients_lock = threading.Lock()
history_lock = threading.Lock()

# Загрузка существующей истории
history: list[dict[str, Any]] = []
if HISTORY_FILE.exists():
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        history = []


def get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def udp_broadcast_presence() -> None:
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({"app": "local_messenger", "port": PORT}).encode("utf-8")
    while True:
        try:
            udp_sock.sendto(payload, ("255.255.255.255", UDP_PORT))
        except Exception:
            pass
        time.sleep(2)


def send_msg(sock: socket.socket, data_dict: dict[str, Any]) -> bool:
    try:
        payload = json.dumps(data_dict).encode("utf-8")
        header = struct.pack("!I", len(payload))
        sock.sendall(header + payload)
        return True
    except Exception:
        return False


def recv_msg(sock: socket.socket) -> dict[str, Any] | None:
    try:
        header = sock.recv(4)
        if not header:
            return None
        length = struct.unpack("!I", header)[0]
        data = bytearray()
        while len(data) < length:
            packet = sock.recv(length - len(data))
            if not packet:
                return None
            data.extend(packet)
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def save_history() -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[!] Ошибка записи истории: {e}", file=sys.stderr)


def broadcast(data: dict[str, Any], exclude_sock: socket.socket | None = None) -> None:
    payload = json.dumps(data).encode("utf-8")
    header = struct.pack("!I", len(payload))
    packet = header + payload

    with clients_lock:
        to_remove = []
        for client_sock in clients:
            if client_sock == exclude_sock:
                continue
            try:
                client_sock.sendall(packet)
            except Exception:
                to_remove.append(client_sock)

        for client_sock in to_remove:
            if client_sock in clients:
                del clients[client_sock]
                try:
                    client_sock.close()
                except Exception:
                    pass


def handle_client(client_sock: socket.socket, client_addr: tuple[str, int]) -> None:
    nickname = "Аноним"

    while True:
        msg = recv_msg(client_sock)
        if msg is None:
            break

        action = msg.get("action")

        if action == "ping":
            send_msg(client_sock, {"action": "pong"})
            break

        elif action == "register":
            nickname = msg.get("nickname", "Аноним")
            with clients_lock:
                clients[client_sock] = nickname
            print(f"[*] {client_addr} зашел под ником {nickname}")

        elif action == "get_history":
            with history_lock:
                send_msg(client_sock, {"action": "history", "messages": history})

        elif action == "msg":
            text = msg.get("text", "")
            time_str = msg.get("time", "")
            msg_id = msg.get("id") or uuid.uuid4().hex
            msg_data = {
                "id": msg_id,
                "action": "msg",
                "sender": nickname,
                "text": text,
                "time": time_str,
                "reactions": {}  # emoji -> list of user nicknames
            }
            with history_lock:
                history.append(msg_data)
                save_history()
            broadcast(msg_data)

        elif action == "file_upload":
            filename = msg.get("filename", "file")
            content_b64 = msg.get("content", "")
            time_str = msg.get("time", "")
            file_id = uuid.uuid4().hex
            safe_filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            server_filename = f"{file_id}_{safe_filename}"
            filepath = UPLOAD_DIR / server_filename

            try:
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(content_b64))

                msg_data = {
                    "id": file_id,
                    "action": "file",
                    "sender": nickname,
                    "filename": safe_filename,
                    "file_id": file_id,
                    "time": time_str,
                    "reactions": {}
                }
                with history_lock:
                    history.append(msg_data)
                    save_history()
                broadcast(msg_data)
            except Exception as e:
                print(f"[!] Ошибка сохранения файла: {e}", file=sys.stderr)

        elif action == "file_download":
            file_id = msg.get("file_id")
            target_file = None
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith(f"{file_id}_"):
                    target_file = fname
                    break

            if target_file:
                filepath = UPLOAD_DIR / target_file
                original_filename = target_file.split("_", 1)[1]
                try:
                    with open(filepath, "rb") as f:
                        file_data = f.read()
                    b64_content = base64.b64encode(file_data).decode("utf-8")
                    send_msg(client_sock, {
                        "action": "file_download_response",
                        "file_id": file_id,
                        "filename": original_filename,
                        "content": b64_content
                    })
                except Exception as e:
                    print(f"[!] Ошибка отправки файла: {e}", file=sys.stderr)

        elif action == "reaction":
            msg_id = msg.get("msg_id")
            emoji = msg.get("emoji")
            user = nickname

            with history_lock:
                target_msg = next((m for m in history if m.get("id") == msg_id or m.get("file_id") == msg_id), None)
                if target_msg:
                    reactions = target_msg.setdefault("reactions", {})
                    user_list = reactions.setdefault(emoji, [])
                    if user in user_list:
                        user_list.remove(user)
                        if not user_list:
                            del reactions[emoji]
                    else:
                        user_list.append(user)
                    save_history()

                    # Оповещаем всех клиентов
                    broadcast({
                        "action": "reaction_update",
                        "msg_id": msg_id,
                        "reactions": reactions
                    })

    with clients_lock:
        if client_sock in clients:
            del clients[client_sock]
    client_sock.close()


def start_server() -> None:
    threading.Thread(target=udp_broadcast_presence, daemon=True).start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", PORT))
        server.listen()
    except Exception as e:
        print(f"[!] Ошибка старта TCP сервера: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Сервер 'ВОЛНА' запущен на IP: {get_local_ip()} | Порт: {PORT}")

    try:
        while True:
            client_sock, client_addr = server.accept()
            threading.Thread(target=handle_client, args=(client_sock, client_addr), daemon=True).start()
    except KeyboardInterrupt:
        print("\n[*] Завершение работы...")
    finally:
        server.close()


if __name__ == "__main__":
    start_server()