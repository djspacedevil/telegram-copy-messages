# MIT License
#
# (c) 2023 Robert Giessmann
# Erweiterungen 2025: Topics-Liste, Thread-Forwarding, Scheduler, Dry-Run, saubere Duplikatvermeidung

from os import getenv, makedirs
import os
import pickle
from sys import exit
import threading
import time

from dotenv import load_dotenv, find_dotenv
from telegram.client import Telegram

load_dotenv(find_dotenv())

######################
# App Configurations #
######################

src_chat = getenv("SOURCE") or None
dst_chat = getenv("DESTINATION") or None
dst_thread_env = getenv("DESTINATION_MESSAGE_THREAD_ID")

# Steuerung
DRY_RUN = (getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"})
POLL_INTERVAL = int(getenv("POLL_INTERVAL_SECONDS", "5"))     # Poll-Frequenz
BATCH_LIMIT = int(getenv("BATCH_LIMIT", "50"))                 # wie viele pro Poll holen
STATE_DIR = "data"
LAST_SEEN_FILE = os.path.join(STATE_DIR, "last_seen.pickle")   # letzte gesehene ID je SOURCE
MAP_FILE = os.path.join(STATE_DIR, "message_copy_dict.pickle") # Mapping SOURCE->DEST

EXCLUDE_THESE_MESSAGE_TYPES = [
    "messageChatChangePhoto",
    "messageChatChangeTitle",
    "messageBasicGroupChatCreate",
    "messageChatDeleteMember",
    "messageChatAddMembers",
]

###########################
# Telegram Configurations #
###########################

tg = Telegram(
    api_id=getenv("API_ID"),
    api_hash=getenv("API_HASH"),
    phone=getenv("PHONE"),
    database_encryption_key=getenv("DB_PASSWORD"),
    files_directory=getenv("FILES_DIRECTORY"),
    proxy_server=getenv("PROXY_SERVER"),
    proxy_port=getenv("PROXY_PORT"),
    proxy_type={'@type': getenv("PROXY_TYPE")} if getenv("PROXY_TYPE") else None,
)

###############
# Helpers     #
###############

def _wait(res):
    try:
        res.wait()
    except Exception:
        pass
    return getattr(res, "update", None) or {}

def list_chats_and_topics():
    """Chats listen; für Foren alle Topics (Titel -> message_thread_id)."""
    print("\n===== Chats & Topics =====")
    result = tg.get_chats(); result.wait()
    chats = result.update.get('chat_ids', [])
    for chat_id in chats:
        r = tg.get_chat(chat_id); r.wait()
        upd = r.update or {}
        title = upd.get('title', '(no title)')
        print(f"{chat_id}, {title}")

        ctype = upd.get('type') or {}
        if ctype.get('@type') == 'chatTypeSupergroup':
            supergroup_id = ctype.get('supergroup_id')
            if supergroup_id:
                sg = tg.call_method('getSupergroup', {'supergroup_id': supergroup_id}, block=True)
                sg_upd = _wait(sg)
                if sg_upd.get('is_forum'):
                    topics_resp = tg.call_method('getForumTopics', {
                        'chat_id': chat_id,
                        'query': '',
                        'offset_date': 0,
                        'offset_message_id': 0,
                        'offset_message_thread_id': 0,
                        'limit': 100
                    }, block=True)
                    topics_upd = _wait(topics_resp)
                    topics = topics_upd.get('topics', []) or []
                    if topics:
                        for topic in topics:
                            info = topic.get('info', {}) or {}
                            mtid = info.get('message_thread_id')
                            t_title = info.get('title') or info.get('name')
                            if not t_title and mtid:
                                ft = tg.call_method('getForumTopic', {
                                    'chat_id': chat_id,
                                    'message_thread_id': mtid
                                }, block=True)
                                ft_upd = _wait(ft)
                                ft_info = (ft_upd.get('info') or {}) if isinstance(ft_upd, dict) else {}
                                t_title = ft_info.get('title') or ft_info.get('name')
                            if mtid == 1048576 and not t_title:
                                t_title = "General"
                            print(f"  - Topic: {t_title} -> message_thread_id={mtid}")
                    else:
                        print("  (No topics found)")
    print("===== End of list =====\n")

def copy_message(from_chat_id: int, to_chat_id: int, message_id: int, send_copy: bool = True, thread_id: int | None = None):
    data = {
        'chat_id': to_chat_id,
        'from_chat_id': from_chat_id,
        'message_ids': [message_id],
        'send_copy': send_copy,
    }
    if thread_id and thread_id > 0:
        data['message_thread_id'] = thread_id

    if DRY_RUN:
        print(f"[DRY-RUN] Would forward message {message_id} -> chat {to_chat_id}"
              + (f" (thread {thread_id})" if thread_id else ""))
        class Dummy: update = {"messages": [{"id": message_id}]}
        return Dummy()

    result = tg.call_method('forwardMessages', data, block=True)
    result.wait()
    upd = _wait(result)
    if not upd or upd.get("messages") == [None]:
        raise Exception(f"Message {message_id} could not be copied")
    return result

def load_pickle(path, default):
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return pickle.load(f) or default
        except Exception:
            return default
    return default

def save_pickle(path, obj):
    makedirs(STATE_DIR, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(obj, f)

###############
# Main        #
###############

if __name__ == "__main__":
    if (src_chat is None or dst_chat is None):
        print("\nPlease enter SOURCE and DESTINATION in .env file")
        exit(1)

    src_chat = int(src_chat)
    dst_chat = int(dst_chat)

    dst_thread_id = None
    if dst_thread_env:
        try:
            dst_thread_id = int(dst_thread_env)
        except ValueError:
            print(f"Warning: DESTINATION_MESSAGE_THREAD_ID='{dst_thread_env}' is not an int. Ignoring.")
            dst_thread_id = None

    print(f"Starting with SOURCE={src_chat}, DESTINATION={dst_chat}"
          + (f", THREAD={dst_thread_id}" if dst_thread_id else "")
          + f", DRY_RUN={DRY_RUN}, POLL_INTERVAL={POLL_INTERVAL}s, BATCH_LIMIT={BATCH_LIMIT}")

    tg.login()
    list_chats_and_topics()

    # Bereits bekannte Zuordnungen (verhindert Doppel-Forward)
    message_copy_dict = load_pickle(MAP_FILE, {})
    processed_ids = set(message_copy_dict.keys())

    # last_seen initialisieren – mind. so hoch wie bereits verarbeitete IDs
    last_seen_state = load_pickle(LAST_SEEN_FILE, {})
    last_seen = last_seen_state.get(str(src_chat), 0)
    if processed_ids:
        last_seen = max(last_seen, max(processed_ids))
    print(f"Initial last_seen for SOURCE {src_chat}: {last_seen}")

    while True:
        try:
            # Neueste N Nachrichten holen (absteigend)
            r = tg.get_chat_history(src_chat, limit=BATCH_LIMIT, from_message_id=0)
            r.wait()
            upd = r.update or {}
            msgs = upd.get("messages", []) or []

            if not msgs:
                time.sleep(POLL_INTERVAL); continue

            # In aufsteigende Reihenfolge bringen (alt -> neu)
            msgs.sort(key=lambda m: m["id"])

            latest_seen_in_batch = msgs[-1]["id"]  # höchste ID in diesem Poll

            # Endgültige Kandidaten: > last_seen, nicht ausgeschlossen, nicht schon verarbeitet
            candidates = []
            for m in msgs:
                mid = m["id"]
                if m["content"]["@type"] in EXCLUDE_THESE_MESSAGE_TYPES:
                    continue
                if mid <= last_seen:
                    continue
                if mid in processed_ids:
                    continue
                candidates.append(mid)

            if not candidates:
                # Kein echter Neu-Kandidat → last_seen auf aktuelle Spitze anheben,
                # damit wir “Found 50 …” nicht dauernd sehen
                if latest_seen_in_batch > last_seen:
                    last_seen = latest_seen_in_batch
                    last_seen_state[str(src_chat)] = last_seen
                    save_pickle(LAST_SEEN_FILE, last_seen_state)
                time.sleep(POLL_INTERVAL); continue

            print(f"Found {len(candidates)} new message(s) in SOURCE.")

            for mid in candidates:
                try:
                    res = copy_message(src_chat, dst_chat, mid, thread_id=dst_thread_id)
                except Exception as e:
                    print(e)
                    print("This message could not be copied:")
                    rr = tg.get_message(src_chat, mid); rr.wait()
                    print(rr.update)
                    # auch wenn’s fehlschlägt, heben wir last_seen an, um Endlosschleifen zu vermeiden
                    if mid > last_seen:
                        last_seen = mid
                        last_seen_state[str(src_chat)] = last_seen
                        save_pickle(LAST_SEEN_FILE, last_seen_state)
                    continue

                if not DRY_RUN:
                    # optional auf send-succeeded warten
                    ev = threading.Event()
                    def ok(update):
                        if update['old_message_id'] == res.update["messages"][0]['id']:
                            ev.set()
                    tg.add_update_handler('updateMessageSendSucceeded', ok)
                    ev.wait(timeout=30)
                    tg.remove_update_handler('updateMessageSendSucceeded', ok)

                    # Ziel-Chat/Thread: letzte ID (nur für Mapping-Info)
                    params = {"chat_id": dst_chat, "limit": 1}
                    if dst_thread_id:
                        params["message_thread_id"] = dst_thread_id
                    rdst = tg.call_method("getChatHistory", params, block=True); rdst.wait()
                    new_message_id = (rdst.update.get("messages") or [{}])[0].get("id")
                    if new_message_id:
                        message_copy_dict[mid] = new_message_id
                        save_pickle(MAP_FILE, message_copy_dict)
                        processed_ids.add(mid)

                # last_seen fortschreiben (auch im DRY_RUN)
                if mid > last_seen:
                    last_seen = mid
                    last_seen_state[str(src_chat)] = last_seen
                    save_pickle(LAST_SEEN_FILE, last_seen_state)

        except KeyboardInterrupt:
            print("\nInterrupted by user."); break
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(max(POLL_INTERVAL, 5))

        time.sleep(POLL_INTERVAL)
