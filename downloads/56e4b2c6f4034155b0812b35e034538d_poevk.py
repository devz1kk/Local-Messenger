import requests
import time
import random
import hashlib
from datetime import datetime

# --- НАСТРОЙКИ ---
ACCOUNTS =["ttreasuree00#1682", "AnnyLoVe#1582"]
LEAGUE = "Mirage"
VK_TOKEN = "vk1.a.c-P9ZpJDJcfTrjeZYSDixiS1K4gyXKzvseKG1KIhDW1kCEht43AgswW-iEd4qn7XWMuMTnrY-O_zBS3XkX_Or4sxpMfIlHCGX7ZCZyTwZxL86L7swVSlLlUSpyV9HfgmaTeX78tCurUj-cDMb_xMKf4JdCOi1OG1-iYI1zV1P5oXRhRUtpIAQU6dwJPsHwZn8rxWK8IzO0gt03pmNLPueQ"
PEER_ID = 2000000003

HEADERS = {
    "User-Agent": "PoE-Trade-Tracker/8.3 (contact: tvoemilo@gmail.com)",
    "Content-Type": "application/json"
}

known_items = {acc: {} for acc in ACCOUNTS}
pending_missing = {acc: {} for acc in ACCOUNTS} 
first_run = {acc: True for acc in ACCOUNTS}

def get_log_time():
    return datetime.now().strftime("[%H:%M:%S]")

def send_vk(message):
    url = "https://api.vk.com/method/messages.send"
    params = {
        "access_token": VK_TOKEN,
        "peer_id": PEER_ID,
        "message": message,
        "random_id": random.getrandbits(31),
        "v": "5.131"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if "error" in r.json():
            print(f"{get_log_time()} ⚠️ Ошибка ВК: {r.json()['error'].get('error_msg')}")
    except Exception as e:
        print(f"{get_log_time()} ⚠️ Не удалось отправить в ВК: {e}")

def get_item_hash(item_data):
    """
    Сортируем моды и сокеты, чтобы изменение цены не ломало хэш.
    """
    name = item_data.get("name", "")
    type_line = item_data.get("typeLine", "")
    ilvl = item_data.get("ilvl", 0)
    corrupted = item_data.get("corrupted", False)
    
    mods = []
    for mod_type in["implicitMods", "explicitMods", "craftedMods", "enchantMods", "fracturedMods"]:
        mods.extend(item_data.get(mod_type,[]))
    
    mods.sort() 
    mods_str = "".join(mods)
    
    sockets = item_data.get("sockets", [])
    sockets_sorted = sorted([f"{s.get('sColor', '')}{s.get('group', 0)}" for s in sockets])
    socket_str = "".join(sockets_sorted)
    
    raw_string = f"{name}_{type_line}_{ilvl}_{corrupted}_{mods_str}_{socket_str}"
    return hashlib.md5(raw_string.encode('utf-8')).hexdigest()

def get_listed_ids(account_name):
    url = f"https://www.pathofexile.com/api/trade/search/{LEAGUE}"
    payload = {
        "query": {
            "status": {"option": "any"},
            "filters": {"trade_filters": {"filters": {"account": {"input": account_name}}}}
        },
        "sort": {"price": "asc"}
    }
    
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                return data.get("result",[]), data.get("id")
            elif r.status_code == 429:
                print(f"{get_log_time()} 🛑 429 Поиск. Ждем...")
                time.sleep(20)
            else:
                print(f"{get_log_time()} ⚠️ API Search Code: {r.status_code}")
                time.sleep(5)
        except Exception as e:
            print(f"{get_log_time()} ⚠️ Ошибка сети в get_listed_ids: {e}")
            time.sleep(5)
            
    return None, None

def get_item_details(item_ids, query_id):
    details = {}
    if not item_ids: return details
    
    for i in range(0, len(item_ids), 10):
        chunk = item_ids[i:i + 10]
        url = f"https://www.pathofexile.com/api/trade/fetch/{','.join(chunk)}?query={query_id}"
        
        retries = 0
        while retries < 5:
            try:
                r = requests.get(url, headers=HEADERS, timeout=15)
                if r.status_code == 200:
                    for item in r.json().get("result",[]):
                        i_id = item.get("id")
                        item_data = item.get("item", {})
                        
                        name = item_data.get('name', '')
                        type_line = item_data.get('typeLine', '')
                        full_name = f"{name} {type_line}".strip()
                        
                        p = item.get("listing", {}).get("price", {})
                        details[i_id] = {
                            "name": full_name, 
                            "price": f"{p.get('amount', '?')} {p.get('currency', '???')}",
                            "hash": get_item_hash(item_data)
                        }
                    break 
                elif r.status_code == 429:
                    time.sleep(25)
                    retries += 1
                else:
                    time.sleep(5)
                    retries += 1
            except Exception:
                time.sleep(5)
                retries += 1
        time.sleep(1.5) 
    return details

def main():
    print(f"{get_log_time()} ✅ Бот запущен. Начинаю чекать аккаунты...")
    
    while True:
        try:
            for acc in ACCOUNTS:
                current_ids, query_id = get_listed_ids(acc)
                
                if current_ids is None:
                    continue 

                current_ids_set = set(current_ids)

                if first_run[acc]:
                    # ВОТ ОНО, ВЕРНУЛ ТВОЙ ПРИНТ, НЕ ПЛАЧЬ
                    print(f"{get_log_time()} ⚙️ [{acc}] Инициализация. На трейде: {len(current_ids_set)} шт.")
                    known_items[acc] = get_item_details(list(current_ids_set), query_id)
                    first_run[acc] = False
                    continue

                known_ids_set = set(known_items[acc].keys())
                missing_ids = known_ids_set - current_ids_set
                new_ids = list(current_ids_set - known_ids_set)
                
                for m_id in missing_ids:
                    pending_missing[acc][m_id] = {
                        "data": known_items[acc].pop(m_id),
                        "cycles": 0 
                    }

                sold_to_report = []
                added_to_report =[]
                changed_to_report =[]

                if new_ids:
                    new_details_map = get_item_details(new_ids, query_id)
                    matched_pending_ids = set()

                    for n_id, n_data in new_details_map.items():
                        matched = False
                        
                        for p_id, p_info in pending_missing[acc].items():
                            if p_id in matched_pending_ids: continue
                            
                            if p_info["data"]["hash"] == n_data["hash"]:
                                matched = True
                                matched_pending_ids.add(p_id)
                                
                                old_price = p_info["data"]["price"]
                                new_price = n_data["price"]
                                
                                if old_price != new_price:
                                    changed_to_report.append({
                                        "name": n_data["name"],
                                        "old": old_price,
                                        "new": new_price
                                    })
                                
                                known_items[acc][n_id] = n_data
                                break
                        
                        if not matched:
                            added_to_report.append(n_data)
                            known_items[acc][n_id] = n_data

                    for p_id in matched_pending_ids:
                        del pending_missing[acc][p_id]

                timeout_ids = []
                for p_id, p_info in pending_missing[acc].items():
                    if p_info["cycles"] >= 3: 
                        sold_to_report.append(p_info["data"])
                        timeout_ids.append(p_id)
                    else:
                        p_info["cycles"] += 1 
                
                for p_id in timeout_ids:
                    del pending_missing[acc][p_id]

                # ОТПРАВКА И ЛОГИ В КОНСОЛЬ
                if sold_to_report:
                    print(f"{get_log_time()} 💰 [{acc}] Продано: {len(sold_to_report)}")
                    send_vk(f"💰 ПРОДАНО\nАкк: {acc}\n" + "\n".join([f"— {x['name']} за {x['price']}" for x in sold_to_report]))
                    
                if added_to_report:
                    print(f"{get_log_time()} 🛒 [{acc}] Выставлено: {len(added_to_report)}")
                    send_vk(f"🛒 ВЫСТАВЛЕНО\nАкк: {acc}\n" + "\n".join([f"— {x['name']} за {x['price']}" for x in added_to_report]))
                    
                if changed_to_report:
                    print(f"{get_log_time()} ⚖️ [{acc}] Цена изменена: {len(changed_to_report)}")
                    send_vk(f"⚖️ ИЗМЕНЕНИЕ ЦЕНЫ\nАкк: {acc}\n" + "\n".join([f"— {x['name']}: {x['old']} -> {x['new']}" for x in changed_to_report]))

                time.sleep(2) 

        except Exception as e:
            print(f"{get_log_time()} 💥 Ошибка в цикле: {e}")
            time.sleep(30)

        time.sleep(45)

if __name__ == "__main__":
    main()
