import os
import re
import json
import time
import sqlite3
import hashlib
import ssl
import urllib.request
import urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session, Response

app = Flask(__name__)
app.secret_key = os.environ.get("STREAMFLIX_SECRET", "streamflix-super-secret-key-2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_DB_PATH = os.path.join(BASE_DIR, "streamflix.db")
DEVICES_DB_PATH = os.path.join(BASE_DIR, "devices_tracking.db")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", hashlib.md5(b"streamflix-admin-auth-2026").hexdigest())

GEO_CACHE = {}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

# ==========================================================
# 1. BANCOS DE DADOS COM AUTO-MIGRAÇÃO DE COLUNAS
# ==========================================================
def get_main_db():
    conn = sqlite3.connect(MAIN_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_devices_db():
    conn = sqlite3.connect(DEVICES_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_table_columns(conn, table_name, columns_dict):
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table_name})")
        existing_cols = [row[1] for row in cur.fetchall()]
        for col_name, col_type in columns_dict.items():
            if col_name not in existing_cols:
                cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")
        conn.commit()
    except Exception as e:
        pass

def init_dbs():
    conn1 = get_main_db()
    cur1 = conn1.cursor()
    cur1.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur1.execute("""CREATE TABLE IF NOT EXISTS trial_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip_address TEXT,
        device_mac TEXT,
        username TEXT,
        password TEXT,
        plan_type TEXT,
        expires_at INTEGER,
        payment_link TEXT,
        apps_smart_tv TEXT,
        apps_android TEXT,
        created_at TEXT
    )""")
    cur1.execute("""CREATE TABLE IF NOT EXISTS active_users (
        username TEXT PRIMARY KEY,
        password TEXT,
        dns_used TEXT,
        plan_type TEXT,
        max_connections TEXT,
        expires_at INTEGER,
        payment_link TEXT,
        is_trial INTEGER DEFAULT 0
    )""")

    migrate_table_columns(conn1, "active_users", {
        "dns_used": "TEXT",
        "m3u_url": "TEXT",
        "plan_type": "TEXT",
        "max_connections": "TEXT",
        "expires_at": "INTEGER",
        "payment_link": "TEXT",
        "is_trial": "INTEGER DEFAULT 0",
        "created_at_str": "TEXT"
    })
    migrate_table_columns(conn1, "trial_records", {
        "ip_address": "TEXT",
        "device_mac": "TEXT",
        "username": "TEXT",
        "password": "TEXT",
        "plan_type": "TEXT",
        "expires_at": "INTEGER",
        "payment_link": "TEXT",
        "apps_smart_tv": "TEXT",
        "apps_android": "TEXT",
        "created_at": "TEXT"
    })

    defaults = {
        "admin_user": "admin",
        "admin_pass": "admin123",
        "dns_list": "http://zavryqonix.online:80",
        "trial_api_url": "https://infinity.panelbr.site/api/chatbot/lKWOwGbWzo/boQ1Ye1ONY",
        "trial_duration_hours": "6",
        "default_payment_link": "",
        "support_whatsapp": "https://wa.me/5500000000000?text=Preciso%20de%20suporte%20no%20StreamFlix",
        "tmdb_api_key": "",
        "adult_pin": "0000",
        "apps_smart_tv": "",
        "apps_android": "",
        "custom_domain": ""
    }
    for k, v in defaults.items():
        cur1.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn1.commit()
    conn1.close()

    conn2 = get_devices_db()
    cur2 = conn2.cursor()
    cur2.execute("""CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mac TEXT UNIQUE,
        ip TEXT,
        location TEXT,
        device TEXT,
        username TEXT,
        first_seen TEXT,
        last_seen TEXT
    )""")
    cur2.execute("""CREATE TABLE IF NOT EXISTS active_monitoring (
        session_id TEXT PRIMARY KEY,
        username TEXT,
        mac TEXT,
        ip TEXT,
        location TEXT,
        device TEXT,
        content_title TEXT,
        content_type TEXT,
        elapsed_seconds INTEGER,
        last_heartbeat INTEGER,
        started_at TEXT
    )""")
    conn2.commit()
    conn2.close()

init_dbs()

def get_settings():
    conn = get_main_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def save_settings(new_data):
    conn = get_main_db()
    for k, v in new_data.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

# ==========================================================
# 2. LOCALIZAÇÃO E DISPOSITIVOS
# ==========================================================
def resolve_ip_location(ip):
    if not ip or ip in ("127.0.0.1", "0.0.0.0", "localhost") or ip.startswith("192.168.") or ip.startswith("10."):
        return "Rede Local / VPS"
    if ip in GEO_CACHE:
        return GEO_CACHE[ip]
    try:
        url = f"http://ip-api.com/json/{ip}?fields=status,city,regionName,country"
        req = urllib.request.Request(url, headers={"User-Agent": "StreamFlixGeo/2.0"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            if data.get("status") == "success":
                loc = f"{data.get('city','')}, {data.get('regionName','')} - {data.get('country','')}".strip(", -")
                GEO_CACHE[ip] = loc
                return loc
    except Exception:
        pass
    GEO_CACHE[ip] = "Brasil (Local Estimado)"
    return GEO_CACHE[ip]

def parse_user_agent(ua_string):
    if not ua_string:
        return "Dispositivo Web"
    ua = ua_string.lower()
    if "android" in ua: return "Android Mobile" if "mobile" in ua else "Android TV / Box"
    elif "iphone" in ua: return "Apple iPhone"
    elif "ipad" in ua: return "Apple iPad"
    elif "smart-tv" in ua or "tizen" in ua or "webos" in ua: return "Smart TV"
    elif "windows" in ua: return "Windows PC"
    elif "linux" in ua: return "Linux PC"
    return "PC / Desktop"

def record_device_entry(mac, ip, ua_string, username="Desconhecido"):
    if not mac: return
    location = resolve_ip_location(ip)
    device_name = parse_user_agent(ua_string)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_devices_db()
    existing = conn.execute("SELECT id FROM devices WHERE mac = ?", (mac,)).fetchone()
    if existing:
        conn.execute("""UPDATE devices 
            SET ip = ?, location = ?, device = ?, username = ?, last_seen = ?
            WHERE mac = ?""", (ip, location, device_name, username, now_str, mac))
    else:
        conn.execute("""INSERT INTO devices 
            (mac, ip, location, device, username, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (mac, ip, location, device_name, username, now_str, now_str))
    conn.commit()
    conn.close()

# ==========================================================
# 3. PARSER DINÂMICO DE TESTES
# ==========================================================
def format_custom_trial_message(data):
    msg = "*SEGUE OS DADOS DE ACESSO*\n\n"
    msg += f"*Usuário:* {data.get('username','')}                      *Senha:* {data.get('password','')}\n"
    msg += f"*Plano:* {data.get('plan_type','TESTE COMPLETO')}\n"
    msg += "—————————————————\n"
    msg += f"🔗 Assinar/Renovar Plano: {data.get('payment_link','')}\n\n"
    msg += f"🗓️ *Criado em:* {data.get('created_formatted', datetime.now().strftime('%d/%m/%Y %H:%M:%S'))}\n"
    msg += f"🗓️ *Vencimento:* {data.get('expires_formatted','')}\n"
    msg += f"📶 *Conexões:* {data.get('max_connections', '1')}\n\n"
    msg += "—————————————————\n"
    if data.get('apps_smart_tv'):
        for p in data['apps_smart_tv'].split('\n\n'):
            msg += f"📺 {p}\n—————————————————\n"
    msg += "\n🔵 *LINK DIRETO PRA BAIXAR*\n\n"
    if data.get('apps_android'):
        for a in data['apps_android'].split('\n\n'):
            msg += f"🔵 {a}\n—————————————————\n"
    return msg.strip()

def parse_trial_api_response(raw_text, cfg):
    now_ts = int(time.time())
    hours = float(cfg.get("trial_duration_hours") or 6)
    fallback_exp_ts = now_ts + int(hours * 3600)
    fallback_exp_str = datetime.fromtimestamp(fallback_exp_ts).strftime("%d/%m/%Y %H:%M:%S")
    fallback_dns = (cfg.get("dns_list", "").splitlines()[0] if cfg.get("dns_list") else "").strip().rstrip("/")

    res = {
        "username": "", "password": "", "dns": fallback_dns, "m3u_url": "",
        "plan_type": "TESTE COMPLETO", "max_connections": "1",
        "expires_at": fallback_exp_ts, "expires_formatted": fallback_exp_str,
        "created_formatted": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "payment_link": "", "apps_smart_tv": "", "apps_android": "", "raw_message": ""
    }
    if not raw_text or not raw_text.strip(): return res
    cleaned = raw_text.strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            sub = data.get("data") or data.get("result") or data.get("user_info") or data
            u = sub.get("username") or sub.get("user") or sub.get("login") or data.get("username")
            p = sub.get("password") or sub.get("pass") or sub.get("senha") or data.get("password")
            dns = sub.get("dns") or sub.get("server") or data.get("dns")
            plan = sub.get("plan") or sub.get("plan_type") or sub.get("package") or data.get("plan")
            pay = sub.get("payUrl") or sub.get("payment_link") or sub.get("checkout_url") or data.get("payUrl")
            exp_formatted = data.get("expiresAtFormatted") or data.get("expiresAt") or data.get("expires_formatted")
            created_formatted = data.get("createdAtFormatted") or data.get("createdAt") or data.get("created_formatted")
            conns = data.get("connections") or data.get("max_connections")
            reply = data.get("reply") or ""
            if u: res["username"] = str(u).strip()
            if p: res["password"] = str(p).strip()
            if dns: res["dns"] = str(dns).strip().rstrip("/")
            if plan: res["plan_type"] = str(plan).replace("🔹", "").replace("TESTE", "Teste").strip()
            if conns: res["max_connections"] = str(conns).strip()
            if pay: res["payment_link"] = str(pay).strip()
            if exp_formatted: res["expires_formatted"] = str(exp_formatted).strip()
            if created_formatted: res["created_formatted"] = str(created_formatted).strip()
            if reply:
                tv_matches = []
                for p_match in re.findall(r'(\*?Parce?ria[^\n*]+\*?[\s\S]*?\*COD:\*?\s*[^\n]+)', reply, re.IGNORECASE):
                    clean_p = p_match.replace("*", "").replace("—", "").replace("🟣", "").replace("🟢", "").replace("🛑", "").replace("🟡", "").strip()
                    if clean_p: tv_matches.append(clean_p)
                if tv_matches: res["apps_smart_tv"] = "\n\n".join(tv_matches)

                and_matches = []
                for a_match in re.findall(r'(App:\s*[^\n*]+[\s\S]*?(?=——|—|\n\n\ud83d|\n\n\u26a0|$))', reply, re.IGNORECASE):
                    clean_a = a_match.replace("*", "").replace("—", "").replace("⚠️", "").replace("▫️", "").replace("🔹", "").replace("⚫️", "").replace("🔵", "").strip()
                    if clean_a: and_matches.append(clean_a)
                if and_matches: res["apps_android"] = "\n\n".join(and_matches)

                m3u_find = re.search(r'(https?://[^\s\"\'<>]+(?:get\.php|\.m3u8?)[^\s\"\'<>]*)', reply, re.I)
                if m3u_find: res["m3u_url"] = m3u_find.group(1).strip()
    except: pass

    if not res["username"]:
        u_m = re.search(r"(?:usu[aá]rio|user|login)\s*[:=\-*]+\s*([A-Za-z0-9_.\-]+)", cleaned, re.I)
        if u_m: res["username"] = u_m.group(1).strip()
    if not res["password"]:
        p_m = re.search(r"(?:senha|password|pass)\s*[:=\-*]+\s*([A-Za-z0-9_.\-]+)", cleaned, re.I)
        if p_m: res["password"] = p_m.group(1).strip()
    if not res["payment_link"]:
        pay_m = re.search(r"(https?://[^\s\"'<>]+(?:checkout|fatura|pagamento|mercadopago|pix|assinar|pay)[^\s\"'<>]*)", cleaned, re.I)
        if pay_m: res["payment_link"] = pay_m.group(1).strip()

    if not res["m3u_url"] and res["username"] and res["password"] and res["dns"]:
        res["m3u_url"] = f"{res['dns']}/get.php?username={urllib.parse.quote(res['username'])}&password={urllib.parse.quote(res['password'])}&type=m3u_plus&output=ts"
    if not res["payment_link"] and cfg.get("default_payment_link"):
        res["payment_link"] = cfg.get("default_payment_link")

    res["raw_message"] = format_custom_trial_message(res)
    return res

# ==========================================================
# 4. DOWNLOAD E PROXY DE PLAYLIST M3U
# ==========================================================
@app.route("/api/playlist/download")
def api_playlist_download():
    m3u_url = request.args.get("url", "").strip()
    if not m3u_url:
        dns = request.args.get("dns", "").strip().rstrip("/")
        u = request.args.get("username", "").strip()
        p = request.args.get("password", "").strip()
        if dns and u and p:
            if not dns.startswith("http://") and not dns.startswith("https://"): dns = "http://" + dns
            m3u_url = f"{dns}/get.php?username={urllib.parse.quote(u)}&password={urllib.parse.quote(p)}&type=m3u_plus&output=ts"

    if not m3u_url:
        return jsonify({"ok": False, "error": "URL da playlist não informada"}), 400

    try:
        req = urllib.request.Request(m3u_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=60, context=SSL_CTX) as resp:
            content = resp.read()
            try: text = content.decode("utf-8")
            except: text = content.decode("latin-1", errors="ignore")
            return Response(text, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "error": f"Falha ao conectar no servidor IPTV: {str(e)}"}), 502

# ==========================================================
# 5. MONITORAMENTO EM TEMPO REAL
# ==========================================================
@app.route("/api/monitor/heartbeat", methods=["POST"])
def api_monitor_heartbeat():
    data = request.get_json(silent=True) or {}
    mac = (data.get("mac") or "").strip().upper()
    username = (data.get("username") or "Anônimo").strip()
    title = (data.get("title") or "Nenhum").strip()
    ctype = (data.get("type") or "tv").strip()
    elapsed = int(data.get("elapsed") or 0)
    is_playing = bool(data.get("playing", True))

    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "0.0.0.0"
    ua = request.headers.get("User-Agent", "")

    record_device_entry(mac, ip, ua, username)
    session_id = f"{mac}_{username}"
    now_ts = int(time.time())
    now_str = datetime.now().strftime("%H:%M:%S")

    conn = get_devices_db()
    if not is_playing:
        conn.execute("DELETE FROM active_monitoring WHERE session_id = ?", (session_id,))
    else:
        location = resolve_ip_location(ip)
        device_name = parse_user_agent(ua)
        conn.execute("""INSERT OR REPLACE INTO active_monitoring
            (session_id, username, mac, ip, location, device, content_title, content_type, elapsed_seconds, last_heartbeat, started_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT started_at FROM active_monitoring WHERE session_id = ?), ?))
        """, (session_id, username, mac, ip, location, device_name, title, ctype, elapsed, now_ts, session_id, now_str))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ==========================================================
# 6. ROTAS DE LOGIN E TESTE GRÁTIS
# ==========================================================
@app.route("/api/config")
def api_config():
    cfg = get_settings()
    dns_list = [d.strip().rstrip("/") for d in cfg.get("dns_list", "").splitlines() if d.strip()]
    return jsonify({
        "dns": dns_list[0] if dns_list else "",
        "dns_list": dns_list,
        "support_whatsapp": cfg.get("support_whatsapp", ""),
        "adult_pin": cfg.get("adult_pin", "0000"),
        "default_payment_link": cfg.get("default_payment_link", "")
    })

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    mac = (data.get("mac") or "").strip().upper()

    if not username or not password:
        return jsonify({"ok": False, "error": "Informe usuário e senha."}), 400

    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "0.0.0.0"
    record_device_entry(mac, ip, request.headers.get("User-Agent", ""), username)

    cfg = get_settings()
    dns_list = [d.strip().rstrip("/") for d in cfg.get("dns_list", "").splitlines() if d.strip()]
    primary_dns = dns_list[0] if dns_list else "http://zavryqonix.online:80"

    conn = get_main_db()
    existing = conn.execute("SELECT * FROM active_users WHERE username = ?", (username,)).fetchone()
    now_ts = int(time.time())

    auth_success = False
    chosen_dns = primary_dns
    expires_at = now_ts + (30 * 86400)
    max_conns = "1"
    status_str = "Active"
    plan_type = "Assinatura IPTV"
    created_at_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    payment_link = cfg.get("default_payment_link", "")

    if dns_list:
        for dns in dns_list:
            if not dns.startswith("http://") and not dns.startswith("https://"): dns = "http://" + dns
            try:
                api_url = f"{dns}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
                req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
                with urllib.request.urlopen(req, timeout=7, context=SSL_CTX) as resp:
                    info = json.loads(resp.read().decode("utf-8", errors="ignore"))
                    u_info = info.get("user_info", {})
                    if str(u_info.get("auth")) == "1" or str(u_info.get("status")).lower() in ["active", "trial"]:
                        auth_success = True
                        chosen_dns = dns
                        status_str = u_info.get("status", "Active")
                        max_conns = str(u_info.get("max_connections", "1"))
                        if u_info.get("created_at"):
                            try: created_at_str = datetime.fromtimestamp(int(u_info.get("created_at"))).strftime("%d/%m/%Y %H:%M:%S")
                            except: pass
                        exp_date = u_info.get("exp_date")
                        if exp_date and str(exp_date).isdigit():
                            expires_at = int(exp_date)
                            if now_ts >= expires_at:
                                conn.close()
                                pay_link = existing["payment_link"] if existing else payment_link
                                return jsonify({
                                    "ok": False, "expired": True,
                                    "payment_link": pay_link,
                                    "error": "Assinatura vencida! Redirecionando..."
                                }), 403
                        break
            except Exception:
                continue

    if not auth_success and dns_list:
        conn.close()
        return jsonify({"ok": False, "error": "Credenciais inválidas no servidor."}), 401

    m3u_url = f"{chosen_dns}/get.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}&type=m3u_plus&output=ts"

    if not existing:
        conn.execute("""INSERT OR REPLACE INTO active_users
            (username, password, dns_used, m3u_url, plan_type, max_connections, expires_at, payment_link, is_trial, created_at_str)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (username, password, chosen_dns, m3u_url, plan_type, max_conns, expires_at, payment_link, created_at_str))
        conn.commit()
    else:
        conn.execute("UPDATE active_users SET expires_at = ?, max_connections = ?, created_at_str = ? WHERE username = ?", (expires_at, max_conns, created_at_str, username))
        conn.commit()
        plan_type = existing["plan_type"] or plan_type
        payment_link = existing["payment_link"] or payment_link
    conn.close()

    return jsonify({
        "ok": True, "username": username, "password": password,
        "dns": chosen_dns, "m3u_url": m3u_url, "plan_type": plan_type,
        "max_connections": max_conns, "status": status_str,
        "created_formatted": created_at_str, "expires_at": expires_at,
        "expires_formatted": datetime.fromtimestamp(expires_at).strftime("%d/%m/%Y %H:%M:%S") if expires_at else "Sem Vencimento",
        "payment_link": payment_link, "support_whatsapp": cfg.get("support_whatsapp", "")
    })

@app.route("/api/request-trial", methods=["POST"])
def api_request_trial():
    data = request.get_json(silent=True) or {}
    device_mac = (data.get("device_mac") or "").strip().upper()
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "0.0.0.0"

    if not device_mac:
        ua = request.headers.get("User-Agent", "unknown")
        h = hashlib.sha256(f"{client_ip}-{ua}".encode()).hexdigest()[:12].upper()
        device_mac = ":".join(h[i:i+2] for i in range(0, 12, 2))

    record_device_entry(device_mac, client_ip, request.headers.get("User-Agent", ""), "Teste Grátis")
    conn1 = get_main_db()
    dup = conn1.execute("SELECT * FROM trial_records WHERE ip_address = ? OR device_mac = ?", (client_ip, device_mac)).fetchone()
    cfg = get_settings()

    if dup:
        conn1.close()
        return jsonify({
            "ok": False, "already_used": True,
            "payment_link": dup["payment_link"] or cfg.get("default_payment_link", ""),
            "error": "Limite de Teste Grátis atingido para este aparelho/conexão."
        }), 403

    trial_api_url = (cfg.get("trial_api_url") or "").strip()
    raw_api_resp = ""
    if trial_api_url:
        try:
            req_post = urllib.request.Request(
                trial_api_url, data=b"{}",
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}, method="POST"
            )
            with urllib.request.urlopen(req_post, timeout=15, context=SSL_CTX) as resp:
                raw_api_resp = resp.read().decode("utf-8", errors="ignore")
        except:
            try:
                req_get = urllib.request.Request(trial_api_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req_get, timeout=15, context=SSL_CTX) as resp2:
                    raw_api_resp = resp2.read().decode("utf-8", errors="ignore")
            except: pass

    parsed = parse_trial_api_response(raw_api_resp, cfg)

    if not parsed["username"]:
        rand_suffix = hashlib.md5(f"{client_ip}{time.time()}".encode()).hexdigest()[:5]
        parsed["username"] = f"teste_{rand_suffix}"
        parsed["password"] = f"{int(time.time()) % 900000 + 100000}"
        if parsed["dns"]: parsed["m3u_url"] = f"{parsed['dns']}/get.php?username={parsed['username']}&password={parsed['password']}&type=m3u_plus&output=ts"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn1.execute("""INSERT INTO trial_records
        (ip_address, device_mac, username, password, plan_type, expires_at, payment_link, apps_smart_tv, apps_android, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_ip, device_mac, parsed["username"], parsed["password"], parsed["plan_type"],
         parsed["expires_at"], parsed["payment_link"], parsed["apps_smart_tv"], parsed["apps_android"], now_str))

    conn1.execute("""INSERT OR REPLACE INTO active_users
        (username, password, dns_used, m3u_url, plan_type, max_connections, expires_at, payment_link, is_trial, created_at_str)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (parsed["username"], parsed["password"], parsed["dns"], parsed["m3u_url"], parsed["plan_type"], parsed.get("max_connections", "1"), parsed["expires_at"], parsed["payment_link"], parsed["created_formatted"]))
    conn1.commit()
    conn1.close()

    return jsonify({"ok": True, "trial": parsed})

# ==========================================================
# 7. GESTÃO ADMIN COM TOKEN (BYPASS PROBLEMA DE SESSÃO)
# ==========================================================
@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    data = request.get_json(silent=True) or {}
    cfg = get_settings()
    if data.get("username") == cfg.get("admin_user") and data.get("password") == cfg.get("admin_pass"):
        return jsonify({"ok": True, "token": ADMIN_TOKEN})
    return jsonify({"ok": False, "error": "Credenciais administrativas inválidas."}), 401

@app.route("/api/admin/data", methods=["GET"])
def api_admin_data():
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: return jsonify({"ok": False}), 401
    cfg = get_settings()
    conn2 = get_devices_db()
    devices = [dict(r) for r in conn2.execute("SELECT * FROM devices ORDER BY id DESC LIMIT 200").fetchall()]
    now_ts = int(time.time())
    active_rows = conn2.execute("SELECT * FROM active_monitoring WHERE last_heartbeat >= ? ORDER BY last_heartbeat DESC", (now_ts - 25,)).fetchall()
    active_monitoring = []
    for r in active_rows:
        d = dict(r)
        sec = d.get("elapsed_seconds") or 0
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        d["elapsed_formatted"] = f"{h:02d}:{m:02d}:{s:02d}"
        active_monitoring.append(d)
    conn2.close()
    return jsonify({"ok": True, "settings": cfg, "devices": devices, "monitoring": active_monitoring})

def purge_device_and_trials(target_mac=None, target_ip=None, target_username=None, dev_id=None):
    conn1 = get_main_db()
    conn2 = get_devices_db()
    if dev_id and (not target_mac or not target_ip):
        r = conn2.execute("SELECT * FROM devices WHERE id = ?", (dev_id,)).fetchone()
        if r:
            target_mac = r["mac"] or target_mac
            target_ip = r["ip"] or target_ip
            target_username = r["username"] or target_username
    if target_mac or target_ip or target_username:
        conn1.execute("""DELETE FROM trial_records WHERE 
            (? IS NOT NULL AND device_mac = ?) OR 
            (? IS NOT NULL AND ip_address = ?) OR 
            (? IS NOT NULL AND username = ?)""", (target_mac, target_mac, target_ip, target_ip, target_username, target_username))
        if target_username: conn1.execute("DELETE FROM active_users WHERE username = ?", (target_username,))
        conn1.commit()
    if dev_id: conn2.execute("DELETE FROM devices WHERE id = ?", (dev_id,))
    if target_mac:
        conn2.execute("DELETE FROM devices WHERE mac = ?", (target_mac,))
        conn2.execute("DELETE FROM active_monitoring WHERE mac = ?", (target_mac,))
    if target_ip:
        conn2.execute("DELETE FROM devices WHERE ip = ?", (target_ip,))
        conn2.execute("DELETE FROM active_monitoring WHERE ip = ?", (target_ip,))
    if target_username:
        conn2.execute("DELETE FROM devices WHERE username = ?", (target_username,))
        conn2.execute("DELETE FROM active_monitoring WHERE username = ?", (target_username,))
    conn2.commit()
    conn1.close()
    conn2.close()

@app.route("/api/admin/delete-device/<int:device_id>", methods=["DELETE"])
def api_admin_delete_device(device_id):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: return jsonify({"ok": False}), 401
    purge_device_and_trials(dev_id=device_id)
    return jsonify({"ok": True})

@app.route("/api/admin/delete-trial/<int:trial_id>", methods=["DELETE"])
def api_admin_delete_trial(trial_id):
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: return jsonify({"ok": False}), 401
    conn1 = get_main_db()
    r = conn1.execute("SELECT * FROM trial_records WHERE id = ?", (trial_id,)).fetchone()
    conn1.close()
    if r: purge_device_and_trials(target_mac=r["device_mac"], target_ip=r["ip_address"], target_username=r["username"])
    return jsonify({"ok": True})

@app.route("/api/admin/save", methods=["POST"])
def api_admin_save():
    if request.headers.get("X-Admin-Token") != ADMIN_TOKEN: return jsonify({"ok": False}), 401
    save_settings(request.get_json(silent=True) or {})
    return jsonify({"ok": True})

@app.route("/api/update-adult-pin", methods=["POST"])
def api_update_adult_pin():
    data = request.get_json(silent=True) or {}
    new_pin = str(data.get("new_pin") or "").strip()
    if len(new_pin) != 4 or not new_pin.isdigit(): return jsonify({"ok": False, "error": "O PIN deve conter 4 dígitos."}), 400
    save_settings({"adult_pin": new_pin})
    return jsonify({"ok": True, "pin": new_pin})

# ==========================================================
# 8. ROTAS FRONTEND SEPARADAS
# ==========================================================
@app.route("/")
def index():
    return render_template("user.html")

@app.route("/admin")
def admin_page():
    return render_template("admin.html")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8095))
    app.run(host="0.0.0.0", port=port, debug=False)
