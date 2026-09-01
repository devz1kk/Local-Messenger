import os
import sys
import socket
import json
import struct
import threading
import uuid
import base64
import time

PORT = 12345
UDP_PORT = 12346

# Определяем абсолютные пути относительно папки запуска EXE
def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable)) [4]
    return os.path.dirname(os.path.abspath(__file__))

BASE_DIR = get_base_dir()
UPLOAD_DIR = os.path.join(BASE_DIR, "server_uploads")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

clients = {}
clients_lock = threading.Lock()
history_lock = threading.Lock()

try:
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
except Exception:
    history = []

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

def udp_broadcast_presence():
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({"app": "local_messenger", "port": PORT}).encode('utf-8')
    while True:
        try:
            udp_sock.sendto(payload, ('255.255.255.255', UDP_PORT))
        except Exception:
            pass
        time.sleep(2)

def send_msg(sock, data_dict):
    try:
        payload = json.dumps(data_dict).encode('utf-8')
        header = struct.pack('!I', len(payload))
        sock.sendall(header + payload)
        return True
    except Exception:
        return False

def recv_msg(sock):
    try:
        header = sock.recv(4)
        if not header:
            return None
        length = struct.unpack('!I', header)[0]
        data = b''
        while len(data) < length:
            packet = sock.recv(length - len(data))
            if not packet:
                return None
            data += packet
        return json.loads(data.decode('utf-8'))
    except Exception:
        return None

def append_history(msg_data):
    with history_lock:
        history.append(msg_data)
        try:
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=4)
        except Exception as e:
            print(f"[!] Ошибка записи истории: {e}")

def broadcast(data, exclude_sock=None):
    payload = json.dumps(data).encode('utf-8')
    header = struct.pack('!I', len(payload))
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
                print(f"[-] Клиент {clients[client_sock]} отключился")
                del clients[client_sock]
                try:
                    client_sock.close()
                except:
                    pass

def handle_client(client_sock, client_addr):
    nickname = "Аноним"
    
    while True:
        msg = recv_msg(client_sock)
        if msg is None:
            break
        
        action = msg.get("action")
        
        # Ответ на пинг от сканирующего клиента
        if action == "ping":
            send_msg(client_sock, {"action": "pong"})
            break # Сканер сам закроет соединение
            
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
            msg_data = {
                "action": "msg",
                "sender": nickname,
                "text": text,
                "time": time_str
            }
            append_history(msg_data)
            broadcast(msg_data)
            
        elif action == "file_upload":
            filename = msg.get("filename", "file")
            content_b64 = msg.get("content", "")
            time_str = msg.get("time", "")
            
            file_id = uuid.uuid4().hex
            safe_filename = "".join(c for c in filename if c.isalnum() or c in "._- ")
            server_filename = f"{file_id}_{safe_filename}"
            filepath = os.path.join(UPLOAD_DIR, server_filename)
            
            try:
                with open(filepath, "wb") as f:
                    f.write(base64.b64decode(content_b64))
                
                msg_data = {
                    "action": "file",
                    "sender": nickname,
                    "filename": safe_filename,
                    "file_id": file_id,
                    "time": time_str
                }
                append_history(msg_data)
                broadcast(msg_data)
            except Exception as e:
                print(f"[!] Ошибка сохранения файла: {e}")
                
        elif action == "file_download":
            file_id = msg.get("file_id")
            target_file = None
            for fname in os.listdir(UPLOAD_DIR):
                if fname.startswith(f"{file_id}_"):
                    target_file = fname
                    break
            
            if target_file:
                filepath = os.path.join(UPLOAD_DIR, target_file)
                original_filename = target_file.split("_", 1)[1]
                try:
                    with open(filepath, "rb") as f:
                        file_data = f.read()
                    b64_content = base64.b64encode(file_data).decode('utf-8')
                    send_msg(client_sock, {
                        "action": "file_download_response",
                        "file_id": file_id,
                        "filename": original_filename,
                        "content": b64_content
                    })
                except Exception as e:
                    print(f"[!] Ошибка отправки файла: {e}")

    with clients_lock:
        if client_sock in clients:
            del clients[client_sock]
    client_sock.close()

def start_server():
    t_broadcast = threading.Thread(target=udp_broadcast_presence, daemon=True)
    t_broadcast.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("0.0.0.0", PORT))
        server.listen()
    except Exception as e:
        print(f"[!] Ошибка старта TCP сервера: {e}")
        sys.exit(1)
        
    local_ip = get_local_ip()
    print(f"[*] Сервер запущен!")
    print(f"[*] Локальный IP: {local_ip} | Порт: {PORT}")
    print("[*] Важно: При первом запуске РАЗРЕШИ доступ в Брандмауэре Windows!")
    print("[*] Ждем подключения...")
    
    try:
        while True:
            client_sock, client_addr = server.accept()
            t = threading.Thread(target=handle_client, args=(client_sock, client_addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("\n[*] Выключаемся...")
    finally:
        server.close()

if __name__ == "__main__":
    start_server()