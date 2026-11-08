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
from flask import Flask, request, jsonify, render_template_string, session, Response

app = Flask(__name__)
app.secret_key = os.environ.get("STREAMFLIX_SECRET", "streamflix-super-secret-key-2026")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_DB_PATH = os.path.join(BASE_DIR, "streamflix.db")
DEVICES_DB_PATH = os.path.join(BASE_DIR, "devices_tracking.db")

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

    # Garante que nenhuma coluna falte em bancos pré-existentes
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
    if "android" in ua:
        return "Android Mobile" if "mobile" in ua else "Android TV / Box"
    elif "iphone" in ua:
        return "Apple iPhone"
    elif "ipad" in ua:
        return "Apple iPad"
    elif "smart-tv" in ua or "tizen" in ua or "webos" in ua:
        return "Smart TV"
    elif "windows" in ua:
        return "Windows PC"
    elif "linux" in ua:
        return "Linux PC"
    return "PC / Desktop"

def record_device_entry(mac, ip, ua_string, username="Desconhecido"):
    if not mac:
        return
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
# 3. PARSER DINÂMICO DE TESTES (CAPTURA 100% DOS DADOS DO SERVIDOR)
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
    """Extrai dinamicamente dados do servidor IPTV sem fixar valores modelo."""
    now_ts = int(time.time())
    hours = float(cfg.get("trial_duration_hours") or 6)
    fallback_exp_ts = now_ts + int(hours * 3600)
    fallback_exp_str = datetime.fromtimestamp(fallback_exp_ts).strftime("%d/%m/%Y %H:%M:%S")
    fallback_dns = (cfg.get("dns_list", "").splitlines()[0] if cfg.get("dns_list") else "").strip().rstrip("/")

    res = {
        "username": "",
        "password": "",
        "dns": fallback_dns,
        "m3u_url": "",
        "plan_type": "TESTE COMPLETO",
        "max_connections": "1",
        "expires_at": fallback_exp_ts,
        "expires_formatted": fallback_exp_str,
        "created_formatted": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "payment_link": "",
        "apps_smart_tv": "",
        "apps_android": "",
        "raw_message": ""
    }

    if not raw_text or not raw_text.strip():
        return res

    cleaned = raw_text.strip()

    # 1. Tenta decodificar como JSON estruturado
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            sub = data.get("data") or data.get("result") or data.get("user_info") or data
            u = sub.get("username") or sub.get("user") or sub.get("login") or data.get("username")
            p = sub.get("password") or sub.get("pass") or sub.get("senha") or data.get("password")
            dns = sub.get("dns") or sub.get("server") or data.get("dns")
            plan = sub.get("plan") or sub.get("plan_type") or sub.get("package") or sub.get("pacote") or data.get("plan")
            pay = sub.get("payUrl") or sub.get("payment_link") or sub.get("checkout_url") or sub.get("pix_url") or data.get("payUrl")
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
                # Extrai parcerias de TV
                tv_matches = []
                for p_match in re.findall(r'(\*?Parce?ria[^\n*]+\*?[\s\S]*?\*COD:\*?\s*[^\n]+)', reply, re.IGNORECASE):
                    clean_p = p_match.replace("*", "").replace("—", "").replace("🟣", "").replace("🟢", "").replace("🛑", "").replace("🟡", "").strip()
                    if clean_p: tv_matches.append(clean_p)
                if tv_matches: res["apps_smart_tv"] = "\n\n".join(tv_matches)

                # Extrai apps Android
                and_matches = []
                for a_match in re.findall(r'(App:\s*[^\n*]+[\s\S]*?(?=——|—|\n\n\ud83d|\n\n\u26a0|$))', reply, re.IGNORECASE):
                    clean_a = a_match.replace("*", "").replace("—", "").replace("⚠️", "").replace("▫️", "").replace("🔹", "").replace("⚫️", "").replace("🔵", "").strip()
                    if clean_a: and_matches.append(clean_a)
                if and_matches: res["apps_android"] = "\n\n".join(and_matches)

                m3u_find = re.search(r'(https?://[^\s\"\'<>]+(?:get\.php|\.m3u8?)[^\s\"\'<>]*)', reply, re.I)
                if m3u_find: res["m3u_url"] = m3u_find.group(1).strip()
    except Exception:
        pass

    # 2. Extrai de texto formatado / mensagem via Regex
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
# 6. ROTAS DE LOGIN, TESTE GRÁTIS E ADMINISTRAÇÃO
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

    # Na rota de login, pegamos os dados reais do servidor toda vez
    chosen_dns = primary_dns
    expires_at = now_ts + (30 * 86400)
    max_conns = "1"
    status_str = "Active"
    plan_type = "Assinatura IPTV"
    created_at_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    payment_link = cfg.get("default_payment_link", "")

    for dns in dns_list:
        if not dns.startswith("http://") and not dns.startswith("https://"): dns = "http://" + dns
        try:
            api_url = f"{dns}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=7, context=SSL_CTX) as resp:
                info = json.loads(resp.read().decode("utf-8", errors="ignore"))
                u_info = info.get("user_info", {})
                if str(u_info.get("auth")) == "1" or str(u_info.get("status")).lower() in ["active", "trial"]:
                    chosen_dns = dns
                    status_str = u_info.get("status", "Active")
                    max_conns = str(u_info.get("max_connections", "1"))
                    if u_info.get("created_at"):
                        try:
                            created_at_str = datetime.fromtimestamp(int(u_info.get("created_at"))).strftime("%d/%m/%Y %H:%M:%S")
                        except: pass
                    exp_date = u_info.get("exp_date")
                    if exp_date and str(exp_date).isdigit():
                        expires_at = int(exp_date)
                        if now_ts >= expires_at:
                            conn.close()
                            return jsonify({
                                "ok": False, "expired": True,
                                "payment_link": existing["payment_link"] if existing else payment_link,
                                "error": "Assinatura vencida! Redirecionando..."
                            }), 403
                    break
        except Exception:
            continue

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
        "ok": True,
        "username": username,
        "password": password,
        "dns": chosen_dns,
        "m3u_url": m3u_url,
        "plan_type": plan_type,
        "max_connections": max_conns,
        "status": status_str,
        "created_formatted": created_at_str,
        "expires_at": expires_at,
        "expires_formatted": datetime.fromtimestamp(expires_at).strftime("%d/%m/%Y %H:%M:%S") if expires_at else "Sem Vencimento",
        "payment_link": payment_link,
        "support_whatsapp": cfg.get("support_whatsapp", "")
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
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                method="POST"
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

@app.route("/api/check-session", methods=["POST"])
def api_check_session():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username: return jsonify({"valid": False})
    conn = get_main_db()
    row = conn.execute("SELECT * FROM active_users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if row:
        exp_ts = int(row["expires_at"] or 0)
        now_ts = int(time.time())
        if exp_ts > 0 and now_ts >= exp_ts:
            return jsonify({"valid": False, "expired": True, "payment_link": row["payment_link"]})
        return jsonify({
            "valid": True, "expired": False, "expires_at": exp_ts,
            "remaining_seconds": max(0, exp_ts - now_ts) if exp_ts > 0 else 999999,
            "plan_type": row["plan_type"],
            "payment_link": row["payment_link"]
        })
    return jsonify({"valid": True, "expired": False, "remaining_seconds": 86400})

# --- GESTÃO ADMIN COM PURGE TOTAL DE REGISTROS ---
@app.route("/api/admin/login", methods=["POST"])
def api_admin_login():
    data = request.get_json(silent=True) or {}
    cfg = get_settings()
    if data.get("username") == cfg.get("admin_user") and data.get("password") == cfg.get("admin_pass"):
        session["is_admin"] = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Credenciais administrativas inválidas."}), 401

@app.route("/api/admin/data", methods=["GET"])
def api_admin_data():
    if not session.get("is_admin"): return jsonify({"ok": False}), 401
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
    if not session.get("is_admin"): return jsonify({"ok": False}), 401
    purge_device_and_trials(dev_id=device_id)
    return jsonify({"ok": True})

@app.route("/api/admin/delete-trial/<int:trial_id>", methods=["DELETE"])
def api_admin_delete_trial(trial_id):
    if not session.get("is_admin"): return jsonify({"ok": False}), 401
    conn1 = get_main_db()
    r = conn1.execute("SELECT * FROM trial_records WHERE id = ?", (trial_id,)).fetchone()
    conn1.close()
    if r: purge_device_and_trials(target_mac=r["device_mac"], target_ip=r["ip_address"], target_username=r["username"])
    return jsonify({"ok": True})

@app.route("/api/admin/save", methods=["POST"])
def api_admin_save():
    if not session.get("is_admin"): return jsonify({"ok": False}), 401
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
# 7. FRONTEND ESTILO NETFLIX (CONTROLES SLIM, VOD PROGRESS E DRAWER CANAIS)
# ==========================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>StreamFlix Pro</title>
    <script src="https://unpkg.com/lucide@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/mpegts.js@latest/dist/mpegts.js"></script>
    <style>
        :root {
            --bg-base: #000000;
            --bg-surface: #0a0a0f;
            --bg-card: #12121a;
            --bg-glass: rgba(10, 10, 15, 0.94);
            --primary: #e50914;
            --primary-glow: rgba(229, 9, 20, 0.4);
            --gold: #f59e0b;
            --success: #10b981;
            --text-main: #ffffff;
            --text-muted: #94a3b8;
            --border: #1e1e2c;
            --radius-pill: 9999px;
            --radius-md: 14px;
            --radius-lg: 20px;
        }
        body.theme-midnight {
            --bg-base: #05070f;
            --bg-surface: #0b1120;
            --bg-card: #141c2e;
            --border: #232f48;
        }
        * { margin:0; padding:0; box-sizing:border-box; font-family:-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color:transparent; }
        body { background:var(--bg-base); color:var(--text-main); min-height:100vh; overflow-x:hidden; padding-bottom:76px; }
        .screen { display:none; min-height:100vh; width:100%; }
        .screen.active { display:block; }
        .hidden { display:none !important; }

        /* BOTÕES REDONDOS MINIMALISTAS ELEGANTES */
        .btn-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 0.45rem;
            padding: 0.65rem 1.35rem;
            border-radius: var(--radius-pill);
            font-size: 0.84rem;
            font-weight: 700;
            cursor: pointer;
            border: 1px solid transparent;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            text-decoration: none;
        }
        .btn-pill-primary {
            background: var(--primary);
            color: #fff;
            box-shadow: 0 4px 16px var(--primary-glow);
        }
        .btn-pill-primary:active { transform: scale(0.97); }
        .btn-pill-success {
            background: linear-gradient(135deg, #10b981, #059669);
            color: #fff;
            box-shadow: 0 4px 16px rgba(16, 185, 129, 0.35);
        }
        .btn-pill-outline {
            background: rgba(255, 255, 255, 0.05);
            border-color: rgba(255, 255, 255, 0.14);
            color: #cbd5e1;
        }
        .btn-pill-outline:hover, .btn-pill-outline:active {
            background: rgba(255, 255, 255, 0.12);
            color: #fff;
        }

        /* TELA DE LOGIN */
        #screen-login { display:none; align-items:center; justify-content:center; padding:1.2rem; min-height:100vh; background:radial-gradient(circle at top, #240508 0%, #000 75%); }
        #screen-login.active { display:flex; }
        .login-box { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:2rem 1.6rem; width:100%; max-width:390px; box-shadow:0 25px 60px rgba(0,0,0,0.95); text-align:center; }
        .brand-title { font-size:1.65rem; font-weight:900; letter-spacing:0.8px; margin-bottom:0.2rem; }
        .brand-title span { color:var(--primary); }
        .brand-sub { color:var(--text-muted); font-size:0.8rem; margin-bottom:1.5rem; }
        .form-group { margin-bottom:1rem; text-align:left; }
        .form-group label { display:block; font-size:0.72rem; font-weight:800; color:var(--text-muted); margin-bottom:0.35rem; text-transform:uppercase; letter-spacing:0.4px; }
        .input-wrap { display:flex; align-items:center; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius-pill); padding:0 1rem; }
        .input-wrap input, .input-wrap textarea { width:100%; background:transparent; border:none; outline:none; padding:0.75rem 0.4rem; color:#fff; font-size:0.9rem; }

        /* MODAL COM ANIMAÇÃO IMERSIVA DO TESTE GRÁTIS GERADO */
        .modal-sheet { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.88); backdrop-filter:blur(10px); z-index:500; align-items:center; justify-content:center; padding:1rem; }
        .modal-sheet.active { display:flex; animation:fadeIn 0.25s ease forwards; }

        @keyframes cardPop {
            0% { opacity:0; transform:scale(0.85) translateY(24px); }
            100% { opacity:1; transform:scale(1) translateY(0); }
        }
        @keyframes fadeIn {
            0% { opacity:0; }
            100% { opacity:1; }
        }

        .modal-box {
            background: var(--bg-surface);
            border: 1px solid var(--success);
            border-radius: var(--radius-lg);
            width: 100%;
            max-width: 450px;
            padding: 1.5rem;
            max-height: 92vh;
            overflow-y: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.95), 0 0 35px rgba(16, 185, 129, 0.22);
            animation: cardPop 0.32s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
        .trial-app-box { background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:0.85rem; font-size:0.78rem; color:#cbd5e1; white-space:pre-wrap; line-height:1.55; word-break:break-word; margin:1rem 0; text-align:left; }
        
        /* TOPBAR & NAVEGAÇÃO */
        .navbar { height:58px; background:var(--bg-glass); backdrop-filter:blur(12px); border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; padding:0 1rem; position:sticky; top:0; z-index:120; }
        .nav-left { display:flex; align-items:center; gap:0.6rem; }
        .nav-brand { font-weight:900; font-size:1.18rem; letter-spacing:0.5px; }
        .nav-brand span { color:var(--primary); }

        .content-wrap { max-width:1180px; margin:0 auto; padding:0.9rem; }
        .hero-box { position:relative; border-radius:var(--radius-lg); overflow:hidden; height:280px; background:#07070c; border:1px solid var(--border); margin-bottom:1.4rem; }
        .hero-bg { position:absolute; inset:0; background-size:cover; background-position:center; transition:background-image 0.4s ease; }
        .hero-overlay { position:absolute; inset:0; background:linear-gradient(0deg, #000 14%, rgba(0,0,0,0.65) 60%, rgba(0,0,0,0.2) 100%); display:flex; flex-direction:column; justify-content:flex-end; padding:1.25rem; }
        .hero-tag { align-self:flex-start; background:var(--primary); color:#fff; font-size:0.65rem; font-weight:800; text-transform:uppercase; padding:0.2rem 0.65rem; border-radius:var(--radius-pill); margin-bottom:0.4rem; }
        .hero-title { font-size:1.35rem; font-weight:900; margin-bottom:0.2rem; }
        .hero-meta { font-size:0.78rem; color:#cbd5e1; margin-bottom:0.85rem; }
        .hero-timer { position:absolute; bottom:0; left:0; right:0; height:4px; background:rgba(255,255,255,0.15); }
        .hero-timer-fill { height:100%; width:0%; background:var(--primary); transition:width 0.1s linear; }

        .home-row { margin-bottom:1.5rem; }
        .row-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:0.65rem; }
        .row-title { font-size:0.95rem; font-weight:800; display:flex; align-items:center; gap:0.4rem; }
        .vod-carousel { display:flex; gap:0.75rem; overflow-x:auto; padding-bottom:0.4rem; scrollbar-width:none; min-height:160px; }
        .vod-carousel::-webkit-scrollbar { display:none; }

        .poster-card { position:relative; flex:0 0 134px; background:var(--bg-surface); border:1px solid var(--border); border-radius:14px; overflow:hidden; cursor:pointer; transition:transform 0.15s, border-color 0.15s; }
        .poster-card:active { transform:scale(0.96); border-color:var(--primary); }
        .poster-aspect { position:relative; width:100%; aspect-ratio:2 / 3; background:#050509; overflow:hidden; }
        .poster-aspect img { width:100%; height:100%; object-fit:cover; display:block; }
        .poster-badge { position:absolute; top:6px; left:6px; background:rgba(229,9,20,0.92); color:#fff; font-size:0.56rem; font-weight:800; padding:2px 6px; border-radius:var(--radius-pill); text-transform:uppercase; }
        .poster-info { padding:0.5rem; }
        .poster-info h4 { font-size:0.76rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .poster-info span { font-size:0.66rem; color:var(--text-muted); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

        /* LISTA VERTICAL DE CANAIS DE TV (SUBSTITUI CARDS PARA TV) */
        .tv-channel-list { display:flex; flex-direction:column; gap:0.5rem; }
        .tv-channel-item { display:flex; align-items:center; gap:0.75rem; padding:0.6rem 0.8rem; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius-md); cursor:pointer; transition:border-color 0.15s; }
        .tv-channel-item:active { border-color:var(--primary); }
        .tv-channel-logo { width:45px; height:45px; border-radius:8px; object-fit:contain; background:#050509; padding:4px; flex-shrink:0; }
        .tv-channel-info { flex:1; overflow:hidden; }
        .tv-channel-info h4 { font-size:0.85rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:0.15rem; color:#fff; }
        .tv-channel-info span { font-size:0.68rem; color:var(--text-muted); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

        /* DRAWER LATERAL OCULTO (CATEGORIAS E LISTA DE CANAIS NO PLAYER) */
        .drawer-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.85); z-index:650; opacity:0; pointer-events:none; transition:opacity 0.25s; }
        .drawer-backdrop.active { opacity:1; pointer-events:auto; }
        .drawer-panel { position:fixed; top:0; left:0; bottom:0; width:290px; max-width:85vw; background:#07070c; border-right:1px solid var(--border); z-index:660; transform:translateX(-100%); transition:transform 0.28s ease; display:flex; flex-direction:column; }
        .drawer-panel.active { transform:translateX(0); }
        .drawer-header { height:58px; padding:0 1rem; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
        .drawer-list { flex:1; overflow-y:auto; padding:0.6rem; display:flex; flex-direction:column; gap:0.35rem; }
        .drawer-item { padding:0.7rem 0.9rem; border-radius:var(--radius-pill); color:var(--text-muted); background:transparent; border:none; text-align:left; font-size:0.84rem; font-weight:700; cursor:pointer; display:flex; justify-content:space-between; align-items:center; }
        .drawer-item.active { background:var(--primary); color:#fff; }

        .posters-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(122px, 1fr)); gap:0.75rem; padding-bottom:1.5rem; min-height:200px; }
        .posters-grid .poster-card { flex:unset; width:100%; }

        /* MODAL DE SÉRIES E DETALHES DE FILMES */
        .series-modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.92); backdrop-filter:blur(10px); z-index:450; align-items:center; justify-content:center; padding:1rem; }
        .series-modal.active { display:flex; animation:fadeIn 0.2s ease forwards; }
        .series-box { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); width:100%; max-width:540px; max-height:92vh; overflow-y:auto; position:relative; }
        .series-hero { height:180px; background-size:cover; background-position:center; position:relative; }
        .series-hero-overlay { position:absolute; inset:0; background:linear-gradient(0deg, var(--bg-surface) 10%, transparent 100%); }
        .series-content { padding:1.2rem; }
        .seasons-bar { display:flex; gap:0.4rem; overflow-x:auto; padding-bottom:0.5rem; margin:0.8rem 0; scrollbar-width:none; }
        .seasons-bar::-webkit-scrollbar { display:none; }
        .season-pill { background:var(--bg-card); border:1px solid var(--border); color:var(--text-muted); padding:0.35rem 0.85rem; border-radius:var(--radius-pill); font-size:0.76rem; font-weight:700; cursor:pointer; white-space:nowrap; }
        .season-pill.active { background:var(--primary); color:#fff; border-color:var(--primary); }
        .episodes-list { display:flex; flex-direction:column; gap:0.5rem; max-height:280px; overflow-y:auto; }
        .episode-card { display:flex; align-items:center; justify-content:space-between; gap:0.6rem; padding:0.6rem 0.8rem; background:var(--bg-card); border:1px solid var(--border); border-radius:12px; cursor:pointer; }
        .episode-card:active { border-color:var(--primary); }

        /* BOTTOM NAVIGATION BAR */
        .bottom-nav { position:fixed; bottom:0; left:0; right:0; height:66px; background:rgba(8,8,12,0.96); backdrop-filter:blur(16px); border-top:1px solid var(--border); display:none; align-items:center; justify-content:space-around; z-index:200; }
        .bottom-nav.active { display:flex; }
        .bnav-item { background:transparent; border:none; color:var(--text-muted); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.2rem; font-size:0.68rem; font-weight:700; cursor:pointer; flex:1; height:100%; }
        .bnav-item.active { color:var(--primary); }

        /* PLAYER DE VÍDEO COMPLETO E SLIM */
        .player-modal { display:none; position:fixed; inset:0; background:#000; z-index:600; flex-direction:column; overflow:hidden; }
        .player-modal.active { display:flex; }
        .video-stage { position:relative; flex:1; width:100%; height:100%; overflow:hidden; background:#000; display:flex; align-items:center; justify-content:center; cursor:pointer; }

        #video-player {
            width: 100vw;
            height: 100%;
            object-fit: contain;
            background: #000;
        }
        #video-player.mode-fill-horizontal {
            width: 100vw;
            height: 100%;
            object-fit: cover;
        }

        .center-play-overlay {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            background: rgba(0,0,0,0.45);
            z-index: 40;
            cursor: pointer;
        }
        .center-play-circle {
            width: 64px;
            height: 64px;
            border-radius: 50%;
            background: rgba(229, 9, 20, 0.9);
            color: #fff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.6rem;
            box-shadow: 0 0 25px var(--primary-glow);
            transition: transform 0.2s;
        }
        .center-play-circle:active { transform: scale(0.92); }

        /* TOPO SLIM DO PLAYER - TOTALMENTE INVISÍVEL EM REPRODUÇÃO */
        .player-top-slim {
            position: absolute;
            top: 14px;
            left: 14px;
            right: 14px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            z-index: 50;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.3s ease;
        }
        .player-top-slim.visible {
            opacity: 1;
            pointer-events: auto;
        }
        .player-title-badge {
            background: rgba(10, 10, 16, 0.65);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255,255,255,0.1);
            color: #fff;
            font-size: 0.78rem;
            font-weight: 700;
            padding: 0.35rem 0.85rem;
            border-radius: var(--radius-pill);
            max-width: 65vw;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* DOCK SLIM FLUTUANTE - TOTALMENTE INVISÍVEL EM REPRODUÇÃO */
        .player-dock-wrap {
            position: absolute;
            bottom: 16px;
            left: 50%;
            transform: translateX(-50%);
            display: flex;
            flex-direction: column;
            align-items: center;
            z-index: 55;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.3s ease;
            max-width: 95vw;
            width: 100%;
            padding: 0 1rem;
        }
        .player-dock-wrap.visible {
            opacity: 1;
            pointer-events: auto;
        }

        /* BARRA DE PROGRESSO (SEEK BAR) EXCLUSIVA PARA VOD (FILMES/SÉRIES) */
        .vod-seek-bar-wrap {
            width: 100%;
            max-width: 440px;
            display: none;
            align-items: center;
            gap: 8px;
            background: rgba(10, 10, 16, 0.75);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: var(--radius-pill);
            padding: 5px 12px;
            margin-bottom: 8px;
        }
        .vod-seek-bar-wrap.active {
            display: flex;
        }
        .seek-time-label {
            font-size: 0.68rem;
            font-weight: 700;
            color: var(--text-muted);
            min-width: 42px;
            text-align: center;
        }
        .seek-track-bg {
            position: relative;
            flex: 1;
            height: 6px;
            background: rgba(255,255,255,0.18);
            border-radius: var(--radius-pill);
            cursor: pointer;
            overflow: hidden;
        }
        .seek-track-fill {
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: var(--primary);
            border-radius: var(--radius-pill);
            transition: width 0.1s linear;
        }

        .player-dock {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 5px 12px;
            background: rgba(12, 12, 18, 0.75);
            backdrop-filter: blur(14px);
            -webkit-backdrop-filter: blur(14px);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: var(--radius-pill);
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.7);
        }
        .mini-btn {
            width: 36px;
            height: 36px;
            border-radius: 50%;
            background: rgba(255, 255, 255, 0.08);
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #cbd5e1;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            font-size: 0.76rem;
            font-weight: 800;
            transition: all 0.18s ease;
            position: relative;
        }
        .mini-btn:active, .mini-btn.active {
            background: var(--primary);
            color: #fff;
            border-color: var(--primary);
            box-shadow: 0 0 12px var(--primary-glow);
        }

        /* POPUP BUBBLE FLUTUANTE INVISÍVEL EM REPRODUÇÃO */
        .dock-bubble {
            position: absolute;
            bottom: 58px;
            left: 50%;
            transform: translateX(-50%) translateY(8px);
            background: rgba(10, 10, 16, 0.95);
            backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.14);
            border-radius: 16px;
            padding: 6px;
            display: none; /* Só exibe ao clicar, com a classe open */
            flex-direction: column;
            gap: 4px;
            box-shadow: 0 14px 35px rgba(0,0,0,0.9);
            min-width: 145px;
            z-index: 60;
            pointer-events: none; /* Garante que não bloqueie cliques se invisível */
            opacity: 0;
            transition: opacity 0.2s;
        }
        .dock-bubble.open {
            display: flex;
            opacity: 1;
            pointer-events: auto;
            animation: fadeIn 0.18s ease forwards;
        }
        .bubble-item {
            padding: 0.45rem 0.8rem;
            border-radius: var(--radius-pill);
            color: #cbd5e1;
            background: transparent;
            border: none;
            font-size: 0.76rem;
            font-weight: 700;
            cursor: pointer;
            text-align: left;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .bubble-item:active, .bubble-item.active {
            background: var(--primary);
            color: #fff;
        }

        /* EPG BAR (SOMENTE PARA TV) */
        .epg-bar { background:rgba(20,20,30,0.85); border-radius:var(--radius-pill); padding:0.45rem 0.85rem; font-size:0.75rem; display:none; justify-content:space-between; margin-bottom:6px; max-width:85vw; }
        .epg-bar.active { display:flex; }

        .config-card { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:1.2rem; margin-bottom:1rem; }
        .config-title { font-size:0.92rem; font-weight:800; color:var(--primary); margin-bottom:0.75rem; display:flex; align-items:center; gap:0.4rem; }

        .admin-table { width:100%; border-collapse:collapse; font-size:0.76rem; margin-top:0.7rem; }
        .admin-table th, .admin-table td { border-bottom:1px solid var(--border); padding:0.55rem 0.5rem; text-align:left; }
        .admin-table th { color:var(--text-muted); text-transform:uppercase; font-size:0.68rem; }
        .badge-live { background:rgba(239,68,68,0.2); color:#f87171; border:1px solid #ef4444; padding:2px 8px; border-radius:var(--radius-pill); font-weight:800; font-size:0.65rem; }

        .loading-cover { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.82); backdrop-filter:blur(5px); z-index:900; align-items:center; justify-content:center; flex-direction:column; gap:0.8rem; }
        .loading-cover.active { display:flex; }
        .spinner { width:42px; height:42px; border:3px solid rgba(255,255,255,0.2); border-top-color:var(--primary); border-radius:50%; animation:spin 0.7s linear infinite; }
        @keyframes spin { to { transform:rotate(360deg); } }

        @media (min-width: 640px) {
            .posters-grid { grid-template-columns:repeat(auto-fill, minmax(155px, 1fr)); }
            .poster-card { flex:0 0 155px; }
            .tv-channel-item { flex-direction:row; }
        }
    </style>
</head>
<body>

    <div id="loading-cover" class="loading-cover">
        <div class="spinner"></div>
        <span id="loading-msg" style="font-size:0.88rem; color:#e2e8f0; font-weight:700;">Conectando ao servidor IPTV...</span>
    </div>

    <!-- 1. LOGIN -->
    <div id="screen-login" class="screen active">
        <div class="login-box">
            <h1 class="brand-title">STREAM<span>FLIX</span></h1>
            <p class="brand-sub">Cinema, Séries & TV ao Vivo</p>
            <form id="form-login">
                <div class="form-group"><label>Usuário</label><div class="input-wrap"><input type="text" id="l-user" required></div></div>
                <div class="form-group"><label>Senha</label><div class="input-wrap"><input type="password" id="l-pass" required></div></div>
                <button type="submit" class="btn-pill btn-pill-primary" style="width:100%; margin-top:0.4rem;">Entrar e Assistir</button>
            </form>
            <button type="button" class="btn-pill btn-pill-success" id="btn-trial" style="width:100%; margin-top:0.75rem;">⚡ Gerar Teste Grátis Automático</button>
        </div>
    </div>

    <!-- 2. CARD DO TESTE GRÁTIS GERADO DINAMICAMENTE (COM ANIMAÇÃO DE POPUP) -->
    <div id="trial-modal" class="modal-sheet">
        <div class="modal-box">
            <h3 style="color:var(--success); font-size:1.15rem; font-weight:900; margin-bottom:0.25rem;">✅ Teste Grátis Liberado!</h3>
            
            <div class="trial-app-box" id="tr-raw" style="font-size:0.72rem; line-height:1.5;"></div>

            <a href="#" target="_blank" class="btn-pill btn-pill-primary hidden" id="btn-pay-link" style="width:100%; margin-bottom:0.6rem; background:linear-gradient(135deg, #f59e0b, #d97706); color:#000; font-weight:900;">💳 Pagar / Assinar Plano Oficial</a>

            <div style="display:grid; grid-template-columns:1fr 1fr; gap:0.55rem; margin-bottom:0.65rem;">
                <button type="button" class="btn-pill btn-pill-outline" id="btn-copy-tr">📋 Copiar Acesso</button>
                <a href="#" target="_blank" class="btn-pill btn-pill-outline" id="btn-wa-tr" style="background:rgba(37,211,102,0.18); border-color:#25d366; color:#4ade80;">📲 Enviar WhatsApp</a>
            </div>
            <button type="button" class="btn-pill btn-pill-success" id="btn-proceed-tr" style="width:100%;">▶ Iniciar Reprodução</button>
        </div>
    </div>

    <!-- 3. TOPBAR GERAL -->
    <header class="navbar hidden" id="main-navbar">
        <div class="nav-left">
            <button class="btn-pill btn-pill-outline hidden" id="btn-global-back" style="padding:0.35rem 0.75rem; font-size:0.75rem;">‹ Voltar</button>
            <div class="nav-brand">STREAM<span>FLIX</span></div>
        </div>
        <div style="display:flex; gap:0.4rem; align-items:center;">
            <button class="btn-pill btn-pill-outline" id="btn-open-drawer" style="padding:0.35rem 0.75rem; font-size:0.75rem;">📂 Categorias</button>
        </div>
    </header>

    <!-- DRAWER LATERAL OCULTO (CATEGORIAS E LISTA DE TV NO PLAYER) -->
    <div class="drawer-backdrop" id="drawer-backdrop"></div>
    <aside class="drawer-panel" id="drawer-panel">
        <div class="drawer-header">
            <strong style="font-size:0.9rem;" id="drawer-title">Menu</strong>
            <button class="btn-pill btn-pill-outline" id="btn-close-drawer" style="padding:0.3rem 0.65rem; font-size:0.72rem;">✕ Fechar</button>
        </div>
        <div class="drawer-list" id="drawer-list"></div>
    </aside>

    <!-- 4. TELAS PRINCIPAIS -->
    <main class="content-wrap hidden" id="main-content">
        <!-- TELA HOME -->
        <div id="view-home">
            <div class="hero-box">
                <div class="hero-bg" id="hero-bg"></div>
                <div class="hero-overlay">
                    <span class="hero-tag">DESTAQUE RECÉM ADICIONADO (5s)</span>
                    <h2 class="hero-title" id="hero-title">Carregando Destaques...</h2>
                    <p class="hero-meta" id="hero-meta">Prévia automática exclusiva de Filmes e Séries</p>
                    <button class="btn-pill btn-pill-primary" id="btn-hero-play" style="align-self:flex-start;">▶ Assistir Agora</button>
                </div>
                <div class="hero-timer"><div class="hero-timer-fill" id="hero-timer-fill"></div></div>
            </div>

            <section class="home-row">
                <div class="row-header"><div class="row-title">🎬 Filmes Recém Adicionados</div></div>
                <div class="vod-carousel" id="carousel-home-movies"></div>
            </section>

            <section class="home-row">
                <div class="row-header"><div class="row-title">🍿 Séries Recém Adicionadas</div></div>
                <div class="vod-carousel" id="carousel-home-series"></div>
            </section>
        </div>

        <!-- TELA CATÁLOGO (FILMES / SÉRIES) -->
        <div id="view-catalog" class="hidden">
            <div class="input-wrap" style="margin-bottom:0.75rem; border-radius:var(--radius-pill);">
                <input type="text" id="catalog-search" placeholder="🔍 Pesquisar por título...">
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">
                <span id="cat-selected-label">Categoria: Todas</span>
                <span id="cat-total-label">0 itens</span>
            </div>
            <div class="posters-grid" id="posters-grid"></div>
        </div>

        <!-- TELA TV AO VIVO (LISTA VERTICAL) -->
        <div id="view-tv" class="hidden">
            <div class="input-wrap" style="margin-bottom:0.75rem; border-radius:var(--radius-pill);">
                <input type="text" id="tv-search" placeholder="🔍 Pesquisar canal de TV...">
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">
                <span id="tv-selected-label">Categoria: Todas</span>
                <span id="tv-total-label">0 canais</span>
            </div>
            <div class="tv-channel-list" id="tv-list"></div>
        </div>

        <!-- TELA CONFIGURAÇÕES -->
        <div id="view-config" class="hidden">
            <div class="config-card">
                <div class="config-title">👤 Dados do Acesso do Servidor</div>
                <div class="trial-row" style="border-bottom:1px dashed #202434; padding-bottom:0.4rem; margin-bottom:0.4rem; display:flex; justify-content:space-between;"><strong>Usuário:</strong><span id="cfg-user">-</span></div>
                <div class="trial-row" style="border-bottom:1px dashed #202434; padding-bottom:0.4rem; margin-bottom:0.4rem; display:flex; justify-content:space-between;"><strong>Senha:</strong><span id="cfg-pass">-</span></div>
                <div class="trial-row" style="border-bottom:1px dashed #202434; padding-bottom:0.4rem; margin-bottom:0.4rem; display:flex; justify-content:space-between;"><strong>Vencimento:</strong><span id="cfg-exp">-</span></div>
                <div class="trial-row" style="border-bottom:1px dashed #202434; padding-bottom:0.4rem; margin-bottom:0.4rem; display:flex; justify-content:space-between;"><strong>Data do Servidor (Login):</strong><span id="cfg-created">-</span></div>
                <div class="trial-row" style="border-bottom:1px dashed #202434; padding-bottom:0.4rem; margin-bottom:0.4rem; display:flex; justify-content:space-between;"><strong>Tipo de Plano:</strong><span id="cfg-plan">-</span></div>
                <div class="trial-row" style="display:flex; justify-content:space-between;"><strong>Telas Simultâneas:</strong><span id="cfg-screens">1 tela</span></div>
            </div>

            <div class="config-card">
                <div class="config-title">🔒 Controle Parental (+18 / Adultos)</div>
                <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.5rem;">Conteúdos adultos ficam ocultos das prévias e da lista geral. Senha padrão: <strong>0000</strong>.</p>
                <div class="input-wrap" style="margin-bottom:0.5rem;"><input type="password" id="cfg-adult-pin" maxlength="4" placeholder="Novo PIN (4 dígitos)"></div>
                <button class="btn-pill btn-pill-primary" id="btn-save-pin">Alterar Senha Adultos</button>
            </div>

            <div class="config-card">
                <div class="config-title">🎨 Aparência e Armazenamento</div>
                <div style="display:flex; gap:0.5rem; margin-bottom:0.75rem;">
                    <button class="btn-pill btn-pill-outline" id="btn-theme-oled" style="flex:1;">OLED Pure Black</button>
                    <button class="btn-pill btn-pill-outline" id="btn-theme-midnight" style="flex:1;">Midnight Blue</button>
                </div>
                <button class="btn-pill btn-pill-outline" id="btn-clear-cache" style="width:100%; color:#fca5a5; border-color:#e50914;">🗑️ Limpar Cache e Recarregar</button>
            </div>

            <div class="config-card">
                <div class="config-title">💬 Suporte e Atendimento</div>
                <a href="#" target="_blank" class="btn-pill btn-pill-success" id="btn-support-wa" style="width:100%;">📲 Chamar no WhatsApp</a>
            </div>
            
            <button class="btn-pill btn-pill-outline" id="btn-logout-cfg" style="width:100%; border-color:#e50914; color:#fca5a5;">Sair da Conta</button>
        </div>
    </main>

    <!-- MODAL DETALHES DE FILMES (COM SINOPSE REAL) -->
    <div id="tmdb-modal" class="series-modal">
        <div class="series-box">
            <div class="series-hero" id="tmdb-hero"><div class="series-hero-overlay"></div></div>
            <div class="series-content">
                <h3 id="tmdb-title" style="font-size:1.25rem; font-weight:900; margin-bottom:0.25rem;">Título</h3>
                <p id="tmdb-meta" style="font-size:0.78rem; color:var(--gold); margin-bottom:0.7rem;">Ano • Avaliação</p>
                <p id="tmdb-plot" style="font-size:0.82rem; color:#cbd5e1; line-height:1.45; margin-bottom:1rem;">Carregando sinopse do filme...</p>
                <div style="display:flex; gap:0.55rem;">
                    <button class="btn-pill btn-pill-primary" id="btn-tmdb-play" style="flex:2;">▶ Assistir Filme</button>
                    <button class="btn-pill btn-pill-outline" id="btn-tmdb-close" style="flex:1;">Fechar</button>
                </div>
            </div>
        </div>
    </div>

    <!-- MODAL SÉRIES COM TEMPORADAS E EPISÓDIOS SELECIONÁVEIS -->
    <div id="series-modal" class="series-modal">
        <div class="series-box">
            <div class="series-hero" id="series-hero"><div class="series-hero-overlay"></div></div>
            <div class="series-content">
                <h3 id="series-title" style="font-size:1.25rem; font-weight:900; margin-bottom:0.25rem;">Nome da Série</h3>
                <p id="series-meta" style="font-size:0.78rem; color:var(--gold); margin-bottom:0.5rem;">Ano • Gênero</p>
                <p id="series-plot" style="font-size:0.82rem; color:#cbd5e1; line-height:1.45; margin-bottom:0.8rem;">Carregando sinopse da série...</p>

                <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.3rem;">
                    <span style="font-size:0.78rem; font-weight:800; color:#fff;">Escolher Temporada:</span>
                    <button class="btn-pill btn-pill-outline" id="btn-close-series" style="padding:0.25rem 0.65rem; font-size:0.7rem;">✕ Fechar</button>
                </div>

                <div class="seasons-bar" id="seasons-bar"></div>

                <div style="font-size:0.76rem; font-weight:800; color:var(--text-muted); margin:0.4rem 0;">Episódios Disponíveis:</div>
                <div class="episodes-list" id="episodes-list"></div>
            </div>
        </div>
    </div>

    <!-- 5. BARRA INFERIOR FIXA -->
    <nav class="bottom-nav" id="bottom-nav">
        <button class="bnav-item active" data-tab="home"><span>🏠</span><span>Início</span></button>
        <button class="bnav-item" data-tab="tv"><span>📺</span><span>TV</span></button>
        <button class="bnav-item" data-tab="movies"><span>🎬</span><span>Filmes</span></button>
        <button class="bnav-item" data-tab="series"><span>🍿</span><span>Séries</span></button>
        <button class="bnav-item" data-tab="config"><span>⚙️</span><span>Ajustes</span></button>
    </nav>

    <!-- 6. PLAYER DE VÍDEO COMPLETO E SLIM -->
    <div id="player-modal" class="player-modal">
        <!-- Topo Slim do Player: Oculto durante reprodução, aparece ao tocar -->
        <div class="player-top-slim" id="player-top-slim">
            <button class="btn-pill btn-pill-outline" id="btn-player-back" style="padding:0.35rem 0.75rem; font-size:0.72rem;">‹ Sair</button>
            <div class="player-title-badge" id="player-title">Reproduzindo...</div>
            <div style="display:flex; gap:0.4rem;">
                <button class="btn-pill btn-pill-outline hidden" id="btn-player-tv-list" style="padding:0.35rem 0.75rem; font-size:0.72rem;">📺 Lista</button>
                <button class="btn-pill btn-pill-outline hidden" id="btn-player-vod-list" style="padding:0.35rem 0.75rem; font-size:0.72rem;">🎬 Voltar a Categoria</button>
            </div>
        </div>

        <div class="video-stage" id="video-stage">
            <video id="video-player" playsinline preload="auto"></video>
            <div class="center-play-overlay hidden" id="center-play-overlay">
                <div class="center-play-circle">▶</div>
            </div>
        </div>

        <!-- DOCK SLIM FLUTUANTE: INVISÍVEL EM REPRODUÇÃO, SURGE AO CLICAR NA TELA E SOME EM 3 SEGUNDOS -->
        <div class="player-dock-wrap" id="player-dock-wrap">
            <div class="epg-bar" id="epg-bar">
                <span id="epg-now">Agora: Ao Vivo</span>
                <span id="epg-time" style="color:var(--text-muted);">--:--</span>
            </div>

            <!-- BARRA DE PROGRESSO (SEEK BAR) EXCLUSIVA PARA FILMES E SÉRIES -->
            <div class="vod-seek-bar-wrap" id="vod-seek-wrap">
                <span class="seek-time-label" id="seek-cur-time">00:00</span>
                <div class="seek-track-bg" id="seek-track-bg">
                    <div class="seek-track-fill" id="seek-track-fill"></div>
                </div>
                <span class="seek-time-label" id="seek-total-time">00:00</span>
            </div>

            <!-- BUBBLES QUE ABREM POR CIMA AO CLICAR NO BOTÃO E SOMEM IMEDIATAMENTE APÓS USO -->
            <div class="dock-bubble" id="bubble-speed">
                <button class="bubble-item active" data-speed="1.0">1.0x (Normal)</button>
                <button class="bubble-item" data-speed="1.3">1.3x</button>
                <button class="bubble-item" data-speed="1.5">1.5x</button>
                <button class="bubble-item" data-speed="1.8">1.8x</button>
                <button class="bubble-item" data-speed="2.0">2.0x</button>
            </div>

            <div class="dock-bubble" id="bubble-aspect">
                <button class="bubble-item active" id="opt-aspect-fill">Preencher Horizontal</button>
                <button class="bubble-item" id="opt-aspect-fit">Original (16:9)</button>
            </div>

            <div class="dock-bubble" id="bubble-engine">
                <button class="bubble-item active" data-engine="auto">Auto (Otimizado)</button>
                <button class="bubble-item" data-engine="hls">HLS (.m3u8)</button>
                <button class="bubble-item" data-engine="mpegts">MPEG-TS (.ts)</button>
                <button class="bubble-item" data-engine="native">Nativo (MP4)</button>
                <a href="#" target="_blank" class="bubble-item" id="opt-vlc-ext" style="text-decoration:none; color:var(--gold);">Abrir no VLC ↗</a>
            </div>

            <!-- LINHA ÚNICA MINIMALISTA -->
            <div class="player-dock" id="player-dock">
                <button class="mini-btn" id="dock-btn-rw" title="Voltar 10s">⏪</button>
                <button class="mini-btn active" id="dock-btn-play" title="Play/Pause">⏸</button>
                <button class="mini-btn" id="dock-btn-ff" title="Avançar 10s">⏩</button>
                <button class="mini-btn" id="dock-btn-speed" title="Velocidade">1.0x</button>
                <button class="mini-btn" id="dock-btn-aspect" title="Ajuste de Tela">📐</button>
                <button class="mini-btn" id="dock-btn-engine" title="Decodificador">⚙️</button>
            </div>
        </div>
    </div>

    <!-- 7. TELA ADMIN -->
    <div id="screen-admin" class="screen">
        <div style="max-width:960px; margin:1.5rem auto; padding:1rem;">
            <div style="display:flex; justify-content:space-between; margin-bottom:1rem; align-items:center;">
                <h2>⚙️ Painel Administrador</h2>
                <button class="btn-pill btn-pill-outline" id="btn-exit-admin">Voltar ao Player</button>
            </div>

            <div id="adm-login-box" class="config-card">
                <div class="form-group"><label>Senha Admin</label><div class="input-wrap"><input type="password" id="adm-pass" placeholder="admin123"></div></div>
                <button class="btn-pill btn-pill-primary" id="btn-adm-login">Acessar Painel</button>
            </div>

            <div id="adm-content" class="hidden">
                <!-- MONITORAMENTO EM TEMPO REAL -->
                <div class="config-card" style="border-color:var(--primary);">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.75rem;">
                        <div class="config-title" style="margin:0;">🔴 Monitoramento em Tempo Real (Transmissões Ativas)</div>
                        <span class="badge-live" id="live-count">0 assistindo agora</span>
                    </div>
                    <div style="overflow-x:auto;">
                        <table class="admin-table">
                            <thead>
                                <tr>
                                    <th>Usuário</th>
                                    <th>Conteúdo Reproduzido</th>
                                    <th>Tempo</th>
                                    <th>IP</th>
                                    <th>Localização</th>
                                    <th>MAC</th>
                                    <th>Dispositivo</th>
                                </tr>
                            </thead>
                            <tbody id="adm-monitoring-tbody">
                                <tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:1rem;">Nenhum usuário assistindo no momento.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- BANCO SEPARADO: REGISTRO DE MACs, IPs E LOCALIZAÇÃO COM BOTÃO DELETAR QUE LIBERA NOVO TESTE -->
                <div class="config-card">
                    <div class="config-title">🛡️ Banco de Dispositivos Registrados (MAC, IP e Localização)</div>
                    <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">Salvo no banco de dados separado <code>devices_tracking.db</code>. Ao deletar, todos os registros daquele aparelho são apagados permitindo novo teste.</p>
                    <div style="overflow-x:auto;">
                        <table class="admin-table">
                            <thead>
                                <tr>
                                    <th>MAC</th>
                                    <th>Último IP</th>
                                    <th>Localização</th>
                                    <th>Dispositivo</th>
                                    <th>Usuário</th>
                                    <th>Último Acesso</th>
                                    <th>Ação</th>
                                </tr>
                            </thead>
                            <tbody id="adm-devices-tbody"></tbody>
                        </table>
                    </div>
                </div>

                <!-- CONFIGURAÇÕES GERAIS -->
                <div class="config-card">
                    <div class="config-title">⚙️ Configurações de Conexão</div>
                    <div class="form-group"><label>DNS Servidor IPTV (Xtream Codes)</label><div class="input-wrap"><textarea id="adm-dns" rows="2"></textarea></div></div>
                    <div class="form-group"><label>URL API de Teste Automático</label><div class="input-wrap"><input type="text" id="adm-trial-api"></div></div>
                    <div class="form-group"><label>Link Padrão de Pagamento (Fallback opcional)</label><div class="input-wrap"><input type="url" id="adm-pay-link"></div></div>
                    <div class="form-group"><label>WhatsApp de Suporte (Link Completo)</label><div class="input-wrap"><input type="text" id="adm-wa-link" placeholder="https://wa.me/55..."></div></div>
                    <button class="btn-pill btn-pill-primary" id="btn-adm-save">Salvar Alterações</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        lucide.createIcons();

        let activeTab = "home";
        let activeCategory = "Todas";
        let currentAuth = JSON.parse(localStorage.getItem("sf_session") || "null");
        let serverConfig = {};
        let catalog = { tv: [], movies: [], series: [] };
        let categoriesMap = { tv: {}, movies: {}, series: {} };
        let currentPlayingItem = null;
        let hlsInstance = null;
        let mpegtsInstance = null;
        let adultPin = "0000";
        let monitorInterval = null;
        let playSecondsCounter = 0;
        let dockTimeout = null;

        const ADULT_REGEX = /\b(adult[os]?|\+18|xxx|porn[oa]?|erotic[oa]?|sexy|playboy|venus|for adults)\b/i;

        function isAdultItem(item) {
            const txt = ((item.name || "") + " " + (item.group || ""));
            return ADULT_REGEX.test(txt);
        }

        function getOrCreateMac() {
            let m = localStorage.getItem("sf_device_mac");
            if (!m) {
                const raw = [navigator.userAgent, screen.width + "x" + screen.height, Intl.DateTimeFormat().resolvedOptions().timeZone].join("|");
                let hash = 0;
                for (let i = 0; i < raw.length; i++) hash = ((hash << 5) - hash) + raw.charCodeAt(i);
                const hex = Math.abs(hash).toString(16).padStart(12, 'A').slice(0, 12).toUpperCase();
                m = hex.match(/.{1,2}/g).join(":");
                localStorage.setItem("sf_device_mac", m);
            }
            return m;
        }

        function copyValue(elementId) {
            const el = document.getElementById(elementId);
            if (el) {
                navigator.clipboard.writeText(el.textContent.trim());
                alert("Copiado com sucesso!");
            }
        }

        function formatTime(sec) {
            if (isNaN(sec) || sec < 0) return "00:00";
            const s = Math.floor(sec % 60);
            const m = Math.floor((sec / 60) % 60);
            const h = Math.floor(sec / 3600);
            const pad = (n) => String(n).padStart(2, "0");
            return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
        }

        function decodeEpgString(str) {
            if (!str) return "";
            try {
                const s = str.trim();
                if (/^[A-Za-z0-9+/=]+$/.test(s) && s.length % 4 === 0 && s.length > 8) {
                    const dec = decodeURIComponent(escape(atob(s)));
                    if (dec && !/[\x00-\x08\x0E-\x1F]/.test(dec)) return dec;
                }
            } catch(e) {}
            const txt = document.createElement("textarea");
            txt.innerHTML = str;
            return txt.value;
        }

        async function loadPublicConfig() {
            try {
                const r = await fetch("/api/config");
                serverConfig = await r.json();
                adultPin = serverConfig.adult_pin || "0000";
                if (serverConfig.support_whatsapp) {
                    document.getElementById("btn-support-wa").href = serverConfig.support_whatsapp;
                }
            } catch (e) {}
        }
        loadPublicConfig();

        let heroItems = [];
        let heroIdx = 0;
        let heroMs = 0;
        setInterval(() => {
            if (activeTab !== "home" || document.getElementById("player-modal").classList.contains("active")) return;
            heroMs += 100;
            document.getElementById("hero-timer-fill").style.width = `${Math.min(100, (heroMs / 5000) * 100)}%`;
            if (heroMs >= 5000) {
                heroMs = 0;
                if (heroItems.length > 0) {
                    heroIdx = (heroIdx + 1) % heroItems.length;
                    renderHeroSlide(heroItems[heroIdx]);
                }
            }
        }, 100);

        function renderHeroSlide(it) {
            if (!it) return;
            document.getElementById("hero-bg").style.backgroundImage = `url('${it.logo}')`;
            document.getElementById("hero-title").textContent = it.name;
            document.getElementById("hero-meta").textContent = `${it.group} • Destaque 5s`;
            document.getElementById("btn-hero-play").onclick = () => {
                if (it.type === 'movies') openTmdbDetails(it);
                else if (it.type === 'series') openSeriesDetails(it);
                else openItemPlay(it);
            };
        }

        function openIndexedDB() {
            return new Promise((resolve, reject) => {
                const req = indexedDB.open("StreamFlix_V5_Store", 1);
                req.onupgradeneeded = (e) => {
                    const db = e.target.result;
                    if (!db.objectStoreNames.contains("cache")) db.createObjectStore("cache");
                };
                req.onsuccess = () => resolve(req.result);
                req.onerror = () => reject(req.error);
            });
        }

        async function saveCatalogToDB(catData) {
            try {
                const db = await openIndexedDB();
                const tx = db.transaction("cache", "readwrite");
                tx.objectStore("cache").put(catData, "last_catalog");
            } catch (e) {}
        }

        async function loadCatalogFromDB() {
            try {
                const db = await openIndexedDB();
                return await new Promise((resolve) => {
                    const tx = db.transaction("cache", "readonly");
                    const req = tx.objectStore("cache").get("last_catalog");
                    req.onsuccess = () => resolve(req.result || null);
                    req.onerror = () => resolve(null);
                });
            } catch (e) { return null; }
        }

        function classifyItem(name, group, url) {
            const u = url.toLowerCase();
            const g = group.toLowerCase();
            const n = name.toLowerCase();
            if (u.includes('/series/')) return 'series';
            if (u.includes('/movie/')) return 'movies';
            if (u.includes('/live/') || u.endsWith('.ts')) return 'tv';
            if (/s\d{1,2}\s*e\d{1,3}|\bep\.?\s*\d+/i.test(n) || g.includes('série') || g.includes('serie') || g.includes('novela') || g.includes('dorama') || g.includes('temporada')) return 'series';
            if ((u.endsWith('.mp4') || u.endsWith('.mkv') || u.endsWith('.avi')) && !g.includes('24h') && !g.includes('canal')) return 'movies';
            if (g.includes('filme') || g.includes('cinema') || g.includes('vod') || g.includes('lançamento') || g.includes('lancamento')) return 'movies';
            return 'tv';
        }

        async function parseM3UContent(text) {
            const lines = text.split(/\r?\n/);
            const total = lines.length;
            const fallbackLogo = 'https://images.unsplash.com/photo-1593784991095-a205069470b6?w=400&q=80';

            catalog = { tv: [], movies: [], series: [] };
            categoriesMap = { tv: {}, movies: {}, series: {} };

            let name = "", group = "Geral", logo = fallbackLogo, hasInf = false, idSeq = 0;
            const msgEl = document.getElementById("loading-msg");

            for (let i = 0; i < total; i++) {
                const l = lines[i].trim();
                if (!l) continue;

                if (l.startsWith("#EXTINF:")) {
                    const cIdx = l.lastIndexOf(",");
                    name = cIdx !== -1 ? l.slice(cIdx + 1).trim() : `Canal ${idSeq + 1}`;
                    const gMatch = l.match(/group-title="([^"]*)"/i);
                    group = gMatch && gMatch[1].trim() ? gMatch[1].trim() : "Geral";
                    const lMatch = l.match(/tvg-logo="([^"]*)"/i);
                    logo = lMatch && lMatch[1].trim() ? lMatch[1].trim() : fallbackLogo;
                    hasInf = true;
                } else if (!l.startsWith("#") && hasInf) {
                    idSeq++;
                    const type = classifyItem(name, group, l);
                    const item = { id: idSeq, name, group, logo, url: l, type };

                    catalog[type].push(item);
                    categoriesMap[type][group] = (categoriesMap[type][group] || 0) + 1;
                    hasInf = false;

                    if (idSeq % 800 === 0) {
                        msgEl.textContent = `Processando canais: ${idSeq} títulos organizados...`;
                        await new Promise(r => setTimeout(r, 0));
                    }
                }
            }

            heroItems = [...catalog.movies, ...catalog.series].filter(it => !isAdultItem(it)).slice(0, 15);
            if (heroItems.length > 0) renderHeroSlide(heroItems[0]);

            renderHomeCarousels();
            saveCatalogToDB({ catalog, categoriesMap });
        }

        async function loadPlaylistData() {
            const cover = document.getElementById("loading-cover");
            const msgEl = document.getElementById("loading-msg");
            cover.classList.add("active");
            msgEl.textContent = "Carregando canais e categorias do servidor...";

            const cached = await loadCatalogFromDB();
            if (cached && cached.catalog && cached.catalog.tv.length > 0) {
                catalog = cached.catalog;
                categoriesMap = cached.categoriesMap;
                heroItems = [...catalog.movies, ...catalog.series].filter(it => !isAdultItem(it)).slice(0, 15);
                if (heroItems.length > 0) renderHeroSlide(heroItems[0]);
                renderHomeCarousels();
                cover.classList.remove("active");
                return;
            }

            let m3uUrl = (currentAuth && currentAuth.m3u_url) || "";
            if (!m3uUrl && currentAuth && currentAuth.username && currentAuth.password && currentAuth.dns) {
                m3uUrl = `${currentAuth.dns}/get.php?username=${encodeURIComponent(currentAuth.username)}&password=${encodeURIComponent(currentAuth.password)}&type=m3u_plus&output=ts`;
            }

            if (!m3uUrl) {
                cover.classList.remove("active");
                alert("Nenhuma URL de playlist configurada. Configure o DNS na Área Admin.");
                return;
            }

            try {
                const proxyUrl = `/api/playlist/download?url=${encodeURIComponent(m3uUrl)}`;
                const r = await fetch(proxyUrl);
                if (!r.ok) throw new Error("Servidor retornou erro ao baixar playlist");
                const text = await r.text();
                if (!text || !text.includes("#EXTINF")) throw new Error("Resposta da playlist vazia ou inválida");
                await parseM3UContent(text);
            } catch (err) {
                alert("Não foi possível carregar a playlist automaticamente do servidor.");
            } finally {
                cover.classList.remove("active");
            }
        }

        function renderHomeCarousels() {
            const fillC = (id, list) => {
                const el = document.getElementById(id);
                el.innerHTML = "";
                list.filter(it => !isAdultItem(it)).slice(0, 16).forEach(it => el.appendChild(createPosterCard(it)));
            };
            fillC("carousel-home-movies", catalog.movies);
            fillC("carousel-home-series", catalog.series);
        }

        function createPosterCard(it) {
            const card = document.createElement("div");
            card.className = "poster-card";
            const tag = it.type === "tv" ? "AO VIVO" : (it.type === "movies" ? "FILME" : "SÉRIE");
            const fallback = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=400&q=80";

            card.innerHTML = `
                <div class="poster-aspect">
                    <img src="${it.logo || fallback}" onerror="this.src='${fallback}'" loading="lazy" alt="${it.name}">
                    <span class="poster-badge">${tag}</span>
                </div>
                <div class="poster-info">
                    <h4>${it.name}</h4>
                    <span>${it.group}</span>
                </div>
            `;
            card.onclick = () => {
                if (it.type === "movies") openTmdbDetails(it);
                else if (it.type === "series") openSeriesDetails(it);
                else openItemPlay(it);
            };
            return card;
        }

        // SÉRIES: MODAL COM ESCOLHER TEMPORADA E EPISÓDIOS REAIS
        async function openSeriesDetails(it) {
            document.getElementById("series-modal").classList.add("active");
            document.getElementById("series-title").textContent = it.name;
            document.getElementById("series-meta").textContent = `Carregando informações da série...`;
            document.getElementById("series-plot").textContent = `Buscando temporadas e episódios...`;
            document.getElementById("series-hero").style.backgroundImage = `url('${it.logo}')`;
            document.getElementById("seasons-bar").innerHTML = "";
            document.getElementById("episodes-list").innerHTML = `<div style="text-align:center; padding:1.5rem; color:var(--text-muted); font-size:0.8rem;">Carregando temporadas...</div>`;

            if (!currentAuth) return;
            let dns = currentAuth.dns.replace(/\/$/, "");
            if (!dns.startsWith("http://") && !dns.startsWith("https://")) dns = "http://" + dns;

            try {
                const r = await fetch(`${dns}/player_api.php?username=${encodeURIComponent(currentAuth.username)}&password=${encodeURIComponent(currentAuth.password)}&action=get_series_info&series_id=${it.id}`);
                const data = await r.json();
                const info = data.info || {};
                const episodes = data.episodes || {};

                const plot = info.plot || info.description || "Sinopse não disponível para esta série.";
                const rating = info.rating ? `⭐ Avaliação: ${info.rating}/10` : "⭐ Série Completa";
                const releaseYear = info.releaseDate ? ` • ${info.releaseDate.split('-')[0]}` : "";
                const genre = info.genre ? ` • ${info.genre}` : "";

                document.getElementById("series-meta").textContent = `${rating}${releaseYear}${genre}`;
                document.getElementById("series-plot").textContent = plot;

                if (info.backdrop_path && info.backdrop_path[0]) {
                    document.getElementById("series-hero").style.backgroundImage = `url('${info.backdrop_path[0]}')`;
                }

                const sBar = document.getElementById("seasons-bar");
                sBar.innerHTML = "";

                const seasonKeys = Object.keys(episodes);
                if (seasonKeys.length === 0) {
                    document.getElementById("episodes-list").innerHTML = `<div style="text-align:center; padding:1.5rem; color:var(--text-muted); font-size:0.8rem;">Nenhum episódio cadastrado nesta série.</div>`;
                    return;
                }

                let currentSeasonKey = seasonKeys[0];

                const renderEpisodes = (sKey) => {
                    const epList = document.getElementById("episodes-list");
                    epList.innerHTML = "";
                    const eps = episodes[sKey] || [];

                    if (eps.length === 0) {
                        epList.innerHTML = `<div style="padding:1rem; color:var(--text-muted); font-size:0.8rem;">Nenhum episódio nesta temporada.</div>`;
                        return;
                    }

                    eps.forEach(ep => {
                        const epCard = document.createElement("div");
                        epCard.className = "episode-card";
                        const epNum = ep.episode_num || ep.id;
                        const epTitle = ep.title ? `Ep. ${epNum} - ${ep.title}` : `Episódio ${epNum}`;
                        const epDur = (ep.info && ep.info.duration) ? ep.info.duration : "";
                        const epExt = ep.container_extension || "mp4";
                        const playUrl = `${dns}/series/${currentAuth.username}/${currentAuth.password}/${ep.id}.${epExt}`;

                        epCard.innerHTML = `
                            <div style="display:flex; align-items:center; gap:0.6rem; overflow:hidden;">
                                <div style="width:28px; height:28px; border-radius:50%; background:var(--primary); color:#fff; display:flex; align-items:center; justify-content:center; font-size:0.75rem; flex-shrink:0;">▶</div>
                                <div style="overflow:hidden;">
                                    <div style="font-size:0.8rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${epTitle}</div>
                                    <span style="font-size:0.68rem; color:var(--text-muted);">${epDur}</span>
                                </div>
                            </div>
                            <span class="btn-pill btn-pill-outline" style="padding:0.25rem 0.65rem; font-size:0.68rem;">Assistir</span>
                        `;

                        epCard.onclick = () => {
                            document.getElementById("series-modal").classList.remove("active");
                            openItemPlay({
                                name: `${info.name || it.name} (${epTitle})`,
                                group: it.group,
                                url: playUrl,
                                type: "series",
                                logo: (ep.info && ep.info.movie_image) || it.logo
                            });
                        };
                        epList.appendChild(epCard);
                    });
                };

                seasonKeys.forEach((sKey, idx) => {
                    const pill = document.createElement("button");
                    pill.className = `season-pill ${idx === 0 ? "active" : ""}`;
                    pill.textContent = `Temporada ${sKey}`;
                    pill.onclick = () => {
                        document.querySelectorAll(".season-pill").forEach(p => p.classList.remove("active"));
                        pill.classList.add("active");
                        renderEpisodes(sKey);
                    };
                    sBar.appendChild(pill);
                });

                renderEpisodes(currentSeasonKey);

            } catch (err) {
                console.error("Erro ao carregar série:", err);
                document.getElementById("series-plot").textContent = "Erro ao buscar detalhes da série no servidor.";
            }
        }
        document.getElementById("btn-close-series").onclick = () => document.getElementById("series-modal").classList.remove("active");

        // DETALHES DE FILMES (COM SINOPSE REAL)
        async function openTmdbDetails(it) {
            document.getElementById("tmdb-modal").classList.add("active");
            document.getElementById("tmdb-title").textContent = it.name;
            document.getElementById("tmdb-meta").textContent = `Carregando informações...`;
            document.getElementById("tmdb-plot").textContent = `Buscando sinopse do filme...`;
            document.getElementById("tmdb-hero").style.backgroundImage = `url('${it.logo}')`;

            document.getElementById("btn-tmdb-play").onclick = () => {
                document.getElementById("tmdb-modal").classList.remove("active");
                openItemPlay(it);
            };

            if (currentAuth && it.type === "movies") {
                try {
                    let dns = currentAuth.dns.replace(/\/$/, "");
                    if (!dns.startsWith("http://") && !dns.startsWith("https://")) dns = "http://" + dns;
                    const r = await fetch(`${dns}/player_api.php?username=${encodeURIComponent(currentAuth.username)}&password=${encodeURIComponent(currentAuth.password)}&action=get_vod_info&vod_id=${it.id}`);
                    const d = await r.json();
                    const info = d.info || {};
                    const plot = info.plot || info.description || "Sinopse não informada pelo servidor.";
                    const rating = info.rating ? `⭐ Avaliação: ${info.rating}/10` : "⭐ Disponível";
                    const year = info.releasedate ? ` • ${info.releasedate.split('-')[0]}` : "";

                    document.getElementById("tmdb-meta").textContent = `${rating}${year} • ${it.group}`;
                    document.getElementById("tmdb-plot").textContent = plot;
                    if (info.backdrop_path && info.backdrop_path[0]) {
                        document.getElementById("tmdb-hero").style.backgroundImage = `url('${info.backdrop_path[0]}')`;
                    }
                } catch (e) {}
            }
        }
        document.getElementById("btn-tmdb-close").onclick = () => document.getElementById("tmdb-modal").classList.remove("active");

        // LISTA LATERAL DE TV E DRAWER DE CATEGORIAS
        function openTvChannelList(catName) {
            const listEl = document.getElementById("drawer-list");
            listEl.innerHTML = "";
            document.getElementById("drawer-title").textContent = catName;

            const btnBack = document.createElement("button");
            btnBack.className = "drawer-item";
            btnBack.innerHTML = `<span>‹ Voltar as Categorias</span>`;
            btnBack.onclick = () => openCategoriesDrawer("tv");
            listEl.appendChild(btnBack);

            const filtered = catalog.tv.filter(it => it.group === catName);
            filtered.forEach(it => {
                const b = document.createElement("button");
                b.className = "drawer-item";
                b.style.padding = "0.4rem 0.75rem";
                b.style.justifyContent = "flex-start";
                b.style.gap = "0.75rem";
                b.innerHTML = `
                    <div style="width:40px; height:40px; background:#050509; border-radius:6px; flex-shrink:0; padding:3px; display:flex; align-items:center; justify-content:center;">
                        <img src="${it.logo}" onerror="this.style.display='none'" style="max-width:100%; max-height:100%;">
                    </div>
                    <div style="overflow:hidden; text-align:left;">
                        <div style="font-size:0.8rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${it.name}</div>
                    </div>
                `;
                b.onclick = () => {
                    closeDrawer();
                    openItemPlay(it);
                };
                listEl.appendChild(b);
            });
        }

        function openCategoriesDrawer(forceTab = null) {
            if (forceTab) activeTab = forceTab;
            const list = document.getElementById("drawer-list");
            list.innerHTML = "";
            const cats = categoriesMap[activeTab] || {};
            const catNames = Object.keys(cats);
            
            let t = activeTab === "tv" ? "TV ao Vivo" : (activeTab === "movies" ? "Filmes" : "Séries");
            document.getElementById("drawer-title").textContent = `Categorias (${t})`;

            const bAll = document.createElement("button");
            bAll.className = `drawer-item ${activeCategory === "Todas" ? "active" : ""}`;
            bAll.innerHTML = `<span>Todas as Categorias</span><span style="font-size:0.7rem; opacity:0.7;">${catalog[activeTab].length}</span>`;
            bAll.onclick = () => {
                selectCategory("Todas");
                if (activeTab === "tv") {
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.add("hidden");
                    document.getElementById("view-tv").classList.remove("hidden");
                    renderTvList();
                }
                closeDrawer();
            };
            list.appendChild(bAll);

            catNames.forEach(cName => {
                const b = document.createElement("button");
                b.className = `drawer-item ${activeCategory === cName ? "active" : ""}`;
                b.innerHTML = `<span>${cName}</span><span style="font-size:0.7rem; opacity:0.7;">${cats[cName]}</span>`;
                b.onclick = () => {
                    if (ADULT_REGEX.test(cName)) {
                        const entered = prompt("🔒 Digite o PIN de 4 dígitos para conteúdo Adulto:");
                        if (entered !== adultPin) return alert("PIN incorreto!");
                    }
                    if (activeTab === "tv") {
                        if (document.getElementById("player-modal").classList.contains("active")) {
                            openTvChannelList(cName);
                        } else {
                            selectCategory(cName);
                            closeDrawer();
                            document.getElementById("view-home").classList.add("hidden");
                            document.getElementById("view-catalog").classList.add("hidden");
                            document.getElementById("view-tv").classList.remove("hidden");
                            renderTvList();
                        }
                    } else {
                        selectCategory(cName);
                        closeDrawer();
                    }
                };
                list.appendChild(b);
            });

            document.getElementById("drawer-backdrop").classList.add("active");
            document.getElementById("drawer-panel").classList.add("active");
        }

        function closeDrawer() {
            document.getElementById("drawer-backdrop").classList.remove("active");
            document.getElementById("drawer-panel").classList.remove("active");
        }
        document.getElementById("btn-open-drawer").onclick = () => openCategoriesDrawer();
        document.getElementById("btn-close-drawer").onclick = closeDrawer;
        document.getElementById("drawer-backdrop").onclick = closeDrawer;

        function selectCategory(catName) {
            activeCategory = catName;
            if (activeTab === "tv") {
                document.getElementById("tv-selected-label").textContent = `Categoria: ${catName}`;
                renderTvList();
            } else {
                document.getElementById("cat-selected-label").textContent = `Categoria: ${catName}`;
                renderCatalogGrid();
            }
        }

        function renderCatalogGrid() {
            const rawList = catalog[activeTab] || [];
            const search = document.getElementById("catalog-search").value.toLowerCase().trim();

            const filtered = rawList.filter(it => {
                const adult = isAdultItem(it);
                if (activeCategory === "Todas") {
                    if (adult) return false;
                } else if (it.group !== activeCategory) {
                    return false;
                }
                if (search && !it.name.toLowerCase().includes(search)) return false;
                return true;
            });

            document.getElementById("cat-total-label").textContent = `${filtered.length} títulos`;
            const grid = document.getElementById("posters-grid");
            grid.innerHTML = "";
            filtered.slice(0, 150).forEach(it => grid.appendChild(createPosterCard(it)));
        }
        document.getElementById("catalog-search").oninput = renderCatalogGrid;

        function renderTvList() {
            const rawList = catalog.tv || [];
            const search = document.getElementById("tv-search").value.toLowerCase().trim();

            const filtered = rawList.filter(it => {
                const adult = isAdultItem(it);
                if (activeCategory === "Todas") {
                    if (adult) return false;
                } else if (it.group !== activeCategory) {
                    return false;
                }
                if (search && !it.name.toLowerCase().includes(search)) return false;
                return true;
            });

            document.getElementById("tv-total-label").textContent = `${filtered.length} canais`;
            const listEl = document.getElementById("tv-list");
            listEl.innerHTML = "";
            filtered.slice(0, 300).forEach(it => {
                const div = document.createElement("div");
                div.className = "tv-channel-item";
                const fallback = "https://images.unsplash.com/photo-1593784991095-a205069470b6?w=400&q=80";
                div.innerHTML = `
                    <div class="tv-channel-logo">
                        <img src="${it.logo || fallback}" onerror="this.src='${fallback}'" style="width:100%; height:100%; object-fit:contain;">
                    </div>
                    <div class="tv-channel-info">
                        <h4>${it.name}</h4>
                        <span>${it.group}</span>
                    </div>
                `;
                div.onclick = () => openItemPlay(it);
                listEl.appendChild(div);
            });
        }
        document.getElementById("tv-search").oninput = renderTvList;


        // ==========================================================
        // PLAYER DE VÍDEO SLIM: CONTROLES OCULTOS DURANTE REPRODUÇÃO
        // ==========================================================
        const vEl = document.getElementById("video-player");

        function closeAllBubbles() {
            document.querySelectorAll(".dock-bubble").forEach(b => b.classList.remove("open"));
        }

        function resetDockTimer() {
            const dockWrap = document.getElementById("player-dock-wrap");
            const topSlim = document.getElementById("player-top-slim");
            dockWrap.classList.add("visible");
            topSlim.classList.add("visible");

            if (dockTimeout) clearTimeout(dockTimeout);
            dockTimeout = setTimeout(() => {
                if (!vEl.paused) {
                    closeAllBubbles();
                    dockWrap.classList.remove("visible");
                    topSlim.classList.remove("visible");
                }
            }, 3000);
        }

        document.getElementById("video-stage").addEventListener("click", (e) => {
            if (e.target.closest("#center-play-overlay")) return;
            const dockWrap = document.getElementById("player-dock-wrap");
            if (dockWrap.classList.contains("visible")) {
                closeAllBubbles();
                dockWrap.classList.remove("visible");
                document.getElementById("player-top-slim").classList.remove("visible");
                if (dockTimeout) clearTimeout(dockTimeout);
            } else {
                resetDockTimer();
            }
        });

        // BARRA DE PROGRESSO (SEEK BAR) DE VOD
        vEl.addEventListener("timeupdate", () => {
            if (!currentPlayingItem || currentPlayingItem.type === "tv") return;
            const cur = vEl.currentTime || 0;
            const dur = vEl.duration || 0;
            document.getElementById("seek-cur-time").textContent = formatTime(cur);
            document.getElementById("seek-total-time").textContent = formatTime(dur);
            if (dur > 0) {
                const pct = (cur / dur) * 100;
                document.getElementById("seek-track-fill").style.width = `${pct}%`;
            }
        });

        document.getElementById("seek-track-bg").addEventListener("click", (e) => {
            e.stopPropagation();
            if (!vEl.duration) return;
            const rect = e.currentTarget.getBoundingClientRect();
            const clickPos = (e.clientX - rect.left) / rect.width;
            vEl.currentTime = clickPos * vEl.duration;
            resetDockTimer();
        });

        function sendMonitorHeartbeat(isPlaying) {
            if (!currentPlayingItem) return;
            const mac = getOrCreateMac();
            fetch("/api/monitor/heartbeat", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    mac: mac,
                    username: currentAuth ? currentAuth.username : "Anônimo",
                    title: currentPlayingItem.name,
                    type: currentPlayingItem.type,
                    elapsed: playSecondsCounter,
                    playing: isPlaying
                })
            }).catch(()=>{});
        }

        function openItemPlay(it, forceEngine = "auto") {
            currentPlayingItem = it;
            playSecondsCounter = 0;
            currentActiveEngine = forceEngine;

            document.getElementById("player-modal").classList.add("active");
            document.getElementById("player-title").textContent = it.name;
            document.getElementById("center-play-overlay").classList.add("hidden");

            stopPlayerEngines();
            vEl.className = "";
            closeAllBubbles();

            // Botões de Retorno ao Catálogo/Lista no Topo do Player
            if (it.type === "tv") {
                document.getElementById("btn-player-tv-list").classList.remove("hidden");
                document.getElementById("btn-player-vod-list").classList.add("hidden");
                document.getElementById("btn-player-tv-list").onclick = (e) => {
                    e.stopPropagation();
                    openTvChannelList(it.group);
                    document.getElementById("drawer-backdrop").classList.add("active");
                    document.getElementById("drawer-panel").classList.add("active");
                };
            } else {
                document.getElementById("btn-player-tv-list").classList.add("hidden");
                document.getElementById("btn-player-vod-list").classList.remove("hidden");
                document.getElementById("btn-player-vod-list").onclick = (e) => {
                    e.stopPropagation();
                    stopPlayerEngines();
                    document.getElementById("player-modal").classList.remove("active");
                    document.querySelector(`.bottom-nav .bnav-item[data-tab="${it.type}"]`).click();
                    selectCategory(it.group);
                };
            }

            resetDockTimer();

            const seekWrap = document.getElementById("vod-seek-wrap");
            if (it.type === "tv") {
                seekWrap.classList.remove("active");
            } else {
                seekWrap.classList.add("active");
                document.getElementById("seek-cur-time").textContent = "00:00";
                document.getElementById("seek-total-time").textContent = "00:00";
                document.getElementById("seek-track-fill").style.width = "0%";
            }

            const epgBar = document.getElementById("epg-bar");
            if (it.type === "tv") {
                epgBar.classList.add("active");
                fetchEpg(it);
            } else {
                epgBar.classList.remove("active");
            }

            const url = it.url;
            let engine = forceEngine;

            if (engine === "auto") {
                if (url.includes(".m3u8")) engine = "hls";
                else if (it.type === "tv" || url.endsWith(".ts")) engine = "ts_or_hls";
                else engine = "native";
            }

            // OTIMIZAÇÃO DE BAIXA LATÊNCIA PARA BUFFER RÁPIDO NO HLS
            if (engine === "ts_or_hls" || engine === "hls") {
                const hlsUrl = url.endsWith('.ts') ? url.replace(/\.ts$/i, '.m3u8') : url;

                if (typeof Hls !== "undefined" && Hls.isSupported()) {
                    hlsInstance = new Hls({
                        enableWorker: true,
                        lowLatencyMode: true,
                        maxBufferLength: 4,
                        maxMaxBufferLength: 8,
                        liveSyncDurationCount: 3,
                        startLevel: -1,
                        autoStartLoad: true
                    });
                    hlsInstance.loadSource(hlsUrl);
                    hlsInstance.attachMedia(vEl);

                    hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => {
                        vEl.play().catch(() => {
                            document.getElementById("center-play-overlay").classList.remove("hidden");
                        });
                    });

                    hlsInstance.on(Hls.Events.ERROR, (event, data) => {
                        if (data.fatal) {
                            startMpegTsOrNative(url);
                        }
                    });
                    return;
                } else if (vEl.canPlayType('application/vnd.apple.mpegurl')) {
                    vEl.src = hlsUrl;
                    vEl.play().catch(() => {
                        document.getElementById("center-play-overlay").classList.remove("hidden");
                    });
                    return;
                }
            }

            if (engine === "mpegts") {
                startMpegTsOrNative(url);
                return;
            }

            startNativePlay(url);
        }

        function startMpegTsOrNative(url) {
            stopPlayerEngines();
            if (typeof mpegts !== "undefined" && mpegts.isSupported()) {
                try {
                    mpegtsInstance = mpegts.createPlayer({
                        type: 'mpegts',
                        isLive: true,
                        url: url,
                        cors: true
                    }, {
                        enableWorker: true,
                        enableStashBuffer: false,
                        stashInitialSize: 128
                    });
                    mpegtsInstance.attachMediaElement(vEl);
                    mpegtsInstance.load();
                    vEl.play().catch(() => {
                        document.getElementById("center-play-overlay").classList.remove("hidden");
                    });
                    return;
                } catch(e) {
                    startNativePlay(url);
                    return;
                }
            }
            startNativePlay(url);
        }

        function startNativePlay(url) {
            stopPlayerEngines();
            vEl.src = url;
            vEl.load();
            vEl.play().catch(() => {
                document.getElementById("center-play-overlay").classList.remove("hidden");
            });
        }

        document.getElementById("center-play-overlay").onclick = (e) => {
            e.stopPropagation();
            document.getElementById("center-play-overlay").classList.add("hidden");
            vEl.play().catch(()=>{});
            resetDockTimer();
        };

        // EPG DECODIFICADO CORRETAMENTE
        async function fetchEpg(it) {
            if (!currentAuth || !it.id) return;
            try {
                let dns = currentAuth.dns.replace(/\/$/, "");
                if (!dns.startsWith("http://") && !dns.startsWith("https://")) dns = "http://" + dns;
                const r = await fetch(`${dns}/player_api.php?username=${encodeURIComponent(currentAuth.username)}&password=${encodeURIComponent(currentAuth.password)}&action=get_short_epg&stream_id=${it.id}`);
                const d = await r.json();
                const prog = (d.epg_listings && d.epg_listings[0]) || {};
                
                const rawTitle = prog.title || it.name;
                const decodedTitle = decodeEpgString(rawTitle);
                
                document.getElementById("epg-now").textContent = `Agora: ${decodedTitle}`;
                document.getElementById("epg-time").textContent = prog.start ? `${prog.start} - ${prog.end}` : "Ao Vivo";
            } catch (e) {
                document.getElementById("epg-now").textContent = `Agora: ${it.name}`;
            }
        }

        function stopPlayerEngines() {
            if (monitorInterval) {
                clearInterval(monitorInterval);
                monitorInterval = null;
            }
            sendMonitorHeartbeat(false);
            if (hlsInstance) { try { hlsInstance.destroy(); } catch (e) {} hlsInstance = null; }
            if (mpegtsInstance) { try { mpegtsInstance.destroy(); } catch (e) {} mpegtsInstance = null; }
            vEl.pause();
            vEl.removeAttribute("src");
            vEl.load();
        }

        document.getElementById("btn-player-back").onclick = (e) => {
            e.stopPropagation();
            stopPlayerEngines();
            document.getElementById("player-modal").classList.remove("active");
        };

        // CONTROLES SLIM DO DOCK COM BOLHAS INVISÍVEIS DURANTE REPRODUÇÃO
        const btnPlay = document.getElementById("dock-btn-play");
        btnPlay.onclick = (e) => {
            e.stopPropagation();
            closeAllBubbles();
            if (vEl.paused) { vEl.play(); btnPlay.textContent = "⏸"; }
            else { vEl.pause(); btnPlay.textContent = "▶"; }
            resetDockTimer();
        };

        document.getElementById("dock-btn-rw").onclick = (e) => {
            e.stopPropagation();
            closeAllBubbles();
            vEl.currentTime = Math.max(0, vEl.currentTime - 10);
            resetDockTimer();
        };
        document.getElementById("dock-btn-ff").onclick = (e) => {
            e.stopPropagation();
            closeAllBubbles();
            vEl.currentTime += 10;
            resetDockTimer();
        };

        const btnSpeed = document.getElementById("dock-btn-speed");
        const bubbleSpeed = document.getElementById("bubble-speed");
        btnSpeed.onclick = (e) => {
            e.stopPropagation();
            const isOpen = bubbleSpeed.classList.contains("open");
            closeAllBubbles();
            if (!isOpen) bubbleSpeed.classList.add("open");
            resetDockTimer();
        };

        document.querySelectorAll("#bubble-speed .bubble-item").forEach(item => {
            item.onclick = (e) => {
                e.stopPropagation();
                const sp = parseFloat(item.dataset.speed);
                vEl.playbackRate = sp;
                btnSpeed.textContent = `${sp}x`;
                document.querySelectorAll("#bubble-speed .bubble-item").forEach(i => i.classList.remove("active"));
                item.classList.add("active");
                closeAllBubbles();
                resetDockTimer();
            };
        });

        const btnAspect = document.getElementById("dock-btn-aspect");
        const bubbleAspect = document.getElementById("bubble-aspect");
        btnAspect.onclick = (e) => {
            e.stopPropagation();
            const isOpen = bubbleAspect.classList.contains("open");
            closeAllBubbles();
            if (!isOpen) bubbleAspect.classList.add("open");
            resetDockTimer();
        };

        document.getElementById("opt-aspect-fill").onclick = (e) => {
            e.stopPropagation();
            vEl.className = "mode-fill-horizontal";
            btnAspect.classList.add("active");
            closeAllBubbles();
            resetDockTimer();
        };

        document.getElementById("opt-aspect-fit").onclick = (e) => {
            e.stopPropagation();
            vEl.className = "";
            btnAspect.classList.remove("active");
            closeAllBubbles();
            resetDockTimer();
        };

        const btnEngine = document.getElementById("dock-btn-engine");
        const bubbleEngine = document.getElementById("bubble-engine");
        btnEngine.onclick = (e) => {
            e.stopPropagation();
            const isOpen = bubbleEngine.classList.contains("open");
            closeAllBubbles();
            if (!isOpen) bubbleEngine.classList.add("open");
            resetDockTimer();
        };

        document.querySelectorAll("#bubble-engine [data-engine]").forEach(item => {
            item.onclick = (e) => {
                e.stopPropagation();
                if (!currentPlayingItem) return;
                closeAllBubbles();
                openItemPlay(currentPlayingItem, item.dataset.engine);
                resetDockTimer();
            };
        });

        // NAVEGAÇÃO BOTTOM BAR E CATEGORIAS GERAIS
        document.querySelectorAll(".bottom-nav .bnav-item").forEach(btn => {
            btn.onclick = () => {
                activeTab = btn.dataset.tab;
                document.querySelectorAll(".bottom-nav .bnav-item").forEach(b => b.classList.remove("active"));
                btn.classList.add("active");

                const backBtn = document.getElementById("btn-global-back");
                if (activeTab === "home") {
                    backBtn.classList.add("hidden");
                    document.getElementById("btn-open-drawer").classList.add("hidden");
                    document.getElementById("view-home").classList.remove("hidden");
                    document.getElementById("view-catalog").classList.add("hidden");
                    document.getElementById("view-tv").classList.add("hidden");
                    document.getElementById("view-config").classList.add("hidden");
                } else if (activeTab === "config") {
                    backBtn.classList.remove("hidden");
                    document.getElementById("btn-open-drawer").classList.add("hidden");
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.add("hidden");
                    document.getElementById("view-tv").classList.add("hidden");
                    document.getElementById("view-config").classList.remove("hidden");
                    fillConfigScreen();
                } else if (activeTab === "tv") {
                    backBtn.classList.remove("hidden");
                    document.getElementById("btn-open-drawer").classList.remove("hidden");
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.add("hidden");
                    document.getElementById("view-config").classList.add("hidden");
                    document.getElementById("view-tv").classList.remove("hidden");
                    activeCategory = "Todas";
                    renderTvList();
                    openCategoriesDrawer("tv");
                } else {
                    backBtn.classList.remove("hidden");
                    document.getElementById("btn-open-drawer").classList.remove("hidden");
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.remove("hidden");
                    document.getElementById("view-tv").classList.add("hidden");
                    document.getElementById("view-config").classList.add("hidden");
                    activeCategory = "Todas";
                    renderCatalogGrid();
                    openCategoriesDrawer(activeTab);
                }
                window.scrollTo({ top: 0, behavior: "smooth" });
            };
        });

        document.getElementById("btn-global-back").onclick = () => {
            document.querySelector('.bottom-nav .bnav-item[data-tab="home"]').click();
        };

        function fillConfigScreen() {
            if (!currentAuth) return;
            document.getElementById("cfg-user").textContent = currentAuth.username;
            document.getElementById("cfg-pass").textContent = currentAuth.password;
            document.getElementById("cfg-exp").textContent = currentAuth.expires_formatted || "Ativo";
            document.getElementById("cfg-created").textContent = currentAuth.created_formatted || "Hoje";
            document.getElementById("cfg-plan").textContent = currentAuth.plan_type || "Plano IPTV";
            document.getElementById("cfg-screens").textContent = `${currentAuth.max_connections || '1'} tela(s)`;
        }

        document.getElementById("btn-save-pin").onclick = async () => {
            const p = document.getElementById("cfg-adult-pin").value.trim();
            const r = await fetch("/api/update-adult-pin", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ new_pin: p }) });
            const d = await r.json();
            if (d.ok) { adultPin = p; alert("✅ Senha Adulto alterada com sucesso!"); }
            else alert(d.error || "Erro ao salvar.");
        };

        document.getElementById("btn-theme-oled").onclick = () => document.body.className = "";
        document.getElementById("btn-theme-midnight").onclick = () => document.body.className = "theme-midnight";
        document.getElementById("btn-clear-cache").onclick = async () => {
            try {
                const db = await openIndexedDB();
                const tx = db.transaction("cache", "readwrite");
                tx.objectStore("cache").clear();
            } catch(e){}
            location.reload();
        };

        // LOGIN DO CLIENTE
        document.getElementById("form-login").onsubmit = async (e) => {
            e.preventDefault();
            const u = document.getElementById("l-user").value.trim();
            const p = document.getElementById("l-pass").value.trim();
            const mac = getOrCreateMac();
            const r = await fetch("/api/login", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ username: u, password: p, mac: mac }) });
            const d = await r.json();
            if (!d.ok) return alert(d.error || "Erro ao entrar.");
            currentAuth = d;
            localStorage.setItem("sf_session", JSON.stringify(d));
            enterApp();
        };

        // GERAÇÃO DE TESTE GRÁTIS AUTOMÁTICO COM ANIMAÇÃO DE CARD E DADOS 100% FORMATADOS
        document.getElementById("btn-trial").onclick = async () => {
            const btn = document.getElementById("btn-trial");
            btn.textContent = "⏳ Conectando ao servidor e gerando seu teste...";
            btn.disabled = true;

            try {
                const mac = getOrCreateMac();
                const r = await fetch("/api/request-trial", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ device_mac: mac }) });
                const d = await r.json();
                if (!d.ok) {
                    btn.textContent = "⚡ Gerar Teste Grátis Automático";
                    btn.disabled = false;
                    return alert(d.error || "Não foi possível gerar o teste.");
                }

                const tr = d.trial;
                currentAuth = tr;
                localStorage.setItem("sf_session", JSON.stringify(tr));

                // Popula os dados reais sem mensagem modelo do sistema
                document.getElementById("tr-u").textContent = tr.username || "-";
                document.getElementById("tr-p").textContent = tr.password || "-";
                document.getElementById("tr-exp").textContent = tr.expires_formatted || "-";
                document.getElementById("tr-plan").textContent = tr.plan_type || "Teste Grátis";

                if (tr.apps_smart_tv) {
                    document.getElementById("row-tr-tv").classList.remove("hidden");
                    document.getElementById("tr-tv").textContent = tr.apps_smart_tv;
                } else {
                    document.getElementById("row-tr-tv").classList.add("hidden");
                }

                if (tr.apps_android) {
                    document.getElementById("row-tr-and").classList.remove("hidden");
                    document.getElementById("tr-and").textContent = tr.apps_android;
                } else {
                    document.getElementById("row-tr-and").classList.add("hidden");
                }

                if (tr.payment_link) {
                    const payBtn = document.getElementById("btn-pay-link");
                    payBtn.classList.remove("hidden");
                    payBtn.href = tr.payment_link;
                } else {
                    document.getElementById("btn-pay-link").classList.add("hidden");
                }

                if (tr.raw_message && tr.raw_message.length > 30) {
                    document.getElementById("row-tr-raw").classList.remove("hidden");
                    document.getElementById("tr-raw").textContent = tr.raw_message;
                } else {
                    document.getElementById("row-tr-raw").classList.add("hidden");
                }

                const shareText = tr.raw_message || "Teste criado com sucesso!";

                document.getElementById("btn-copy-tr").onclick = () => {
                    navigator.clipboard.writeText(shareText);
                    alert("Dados copiados com sucesso!");
                };
                document.getElementById("btn-wa-tr").href = `https://wa.me/?text=${encodeURIComponent(shareText)}`;

                document.getElementById("trial-modal").classList.add("active");
            } catch (e) {
                console.error(e);
                alert("Erro de conexão ao gerar o teste: " + e.message);
            } finally {
                btn.textContent = "⚡ Gerar Teste Grátis Automático";
                btn.disabled = false;
            }
        };

        document.getElementById("btn-proceed-tr").onclick = () => {
            document.getElementById("trial-modal").classList.remove("active");
            enterApp();
        };

        function enterApp() {
            document.getElementById("screen-login").classList.remove("active");
            document.getElementById("main-navbar").classList.remove("hidden");
            document.getElementById("main-content").classList.remove("hidden");
            document.getElementById("bottom-nav").classList.add("active");
            loadPlaylistData();
        }

        document.getElementById("btn-logout-cfg").onclick = () => {
            localStorage.removeItem("sf_session");
            location.reload();
        };

        // ROTA ADMINISTRATIVA (INVISÍVEL NA INTERFACE DO CLIENTE)
        let adminPolling = null;

        // Injeção direta pelo JS para ativar a rota Admin se o path for /admin
        if (window.location.pathname === "/admin") {
            document.getElementById("screen-login").classList.remove("active");
            document.getElementById("screen-admin").classList.add("active");
        }

        document.getElementById("btn-exit-admin").onclick = () => {
            if (adminPolling) clearInterval(adminPolling);
            document.getElementById("screen-admin").classList.remove("active");
            if (currentAuth) enterApp(); else document.getElementById("screen-login").classList.add("active");
        };

        document.getElementById("btn-adm-login").onclick = async () => {
            const p = document.getElementById("adm-pass").value;
            const r = await fetch("/api/admin/login", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ username: "admin", password: p }) });
            if (r.ok) {
                document.getElementById("adm-login-box").classList.add("hidden");
                document.getElementById("adm-content").classList.remove("hidden");
                loadAdminDashboard();
                if (adminPolling) clearInterval(adminPolling);
                adminPolling = setInterval(loadAdminDashboard, 3500);
            } else alert("Senha incorreta");
        };

        async function loadAdminDashboard() {
            try {
                const res = await fetch("/api/admin/data");
                const d = await res.json();
                if (!d.ok) return;

                document.getElementById("adm-dns").value = d.settings.dns_list || "";
                document.getElementById("adm-trial-api").value = d.settings.trial_api_url || "";
                document.getElementById("adm-pay-link").value = d.settings.default_payment_link || "";
                document.getElementById("adm-wa-link").value = d.settings.support_whatsapp || "";

                const monTbody = document.getElementById("adm-monitoring-tbody");
                monTbody.innerHTML = "";
                document.getElementById("live-count").textContent = `${d.monitoring.length} assistindo agora`;

                if (d.monitoring.length === 0) {
                    monTbody.innerHTML = `<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:1rem;">Nenhum usuário assistindo no momento.</td></tr>`;
                } else {
                    d.monitoring.forEach(m => {
                        const tr = document.createElement("tr");
                        tr.innerHTML = `
                            <td><strong>${m.username}</strong></td>
                            <td style="color:var(--gold);">▶ ${m.content_title}</td>
                            <td><span class="badge-live">${m.elapsed_formatted}</span></td>
                            <td>${m.ip}</td>
                            <td>${m.location}</td>
                            <td><code>${m.mac}</code></td>
                            <td>${m.device}</td>
                        `;
                        monTbody.appendChild(tr);
                    });
                }

                const devTbody = document.getElementById("adm-devices-tbody");
                devTbody.innerHTML = "";
                d.devices.forEach(dev => {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td><code>${dev.mac}</code></td>
                        <td>${dev.ip}</td>
                        <td>${dev.location}</td>
                        <td>${dev.device}</td>
                        <td><strong>${dev.username}</strong></td>
                        <td>${dev.last_seen}</td>
                        <td><button class="btn-pill btn-pill-outline" style="color:#fca5a5; border-color:#e50914; padding:0.25rem 0.65rem; font-size:0.7rem;" onclick="deleteDeviceRecord(${dev.id})">🗑️ Deletar</button></td>
                    `;
                    devTbody.appendChild(tr);
                });
            } catch (e) {}
        }

        window.deleteDeviceRecord = async (id) => {
            if (!confirm("Deseja realmente deletar este registro de dispositivo? Isso liberará este aparelho para gerar um novo teste.")) return;
            await fetch(`/api/admin/delete-device/${id}`, { method: "DELETE" });
            loadAdminDashboard();
        };

        document.getElementById("btn-adm-save").onclick = async () => {
            await fetch("/api/admin/save", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
                dns_list: document.getElementById("adm-dns").value,
                trial_api_url: document.getElementById("adm-trial-api").value,
                default_payment_link: document.getElementById("adm-pay-link").value,
                support_whatsapp: document.getElementById("adm-wa-link").value
            })});
            alert("✅ Configurações salvas!");
        };

        if (currentAuth && currentAuth.username && window.location.pathname !== "/admin") {
            enterApp();
        }
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/admin")
def admin_direct():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8095))
    app.run(host="0.0.0.0", port=port, debug=False)
