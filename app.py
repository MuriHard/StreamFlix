import os
import re
import json
import time
import sqlite3
import hashlib
import ssl
import urllib.request
import urllib.parse
import hmac
import base64
import secrets
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit
from flask import stream_with_context
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, session, Response

app = Flask(__name__)
# Segredo persistente fora do código: cookies não podem ser forjados com uma senha pública.
def _load_app_secret():
    value = os.environ.get("STREAMFLIX_SECRET")
    if value:
        return value
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamflix_secret")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    with open(path, "r") as f:
        return f.read().strip()

app.secret_key = _load_app_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

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
    except Exception:
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
        "created_at_str": "TEXT",
        "adult_pin": "TEXT"
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
        # Em uma VPS já instalada, a URL configurada permanece no SQLite.
        # Não publicar tokens de API no app.py de um repositório GitHub público.
        "trial_api_url": os.environ.get("STREAMFLIX_TRIAL_API_URL", ""),
        "trial_duration_hours": "6",
        "default_payment_link": "",
        "support_whatsapp": "https://wa.me/5500000000000?text=Preciso%20de%20suporte%20no%20StreamFlix",
        "adult_pin": "0000"
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
    migrate_table_columns(conn2, "active_monitoring", {"playback_status":"TEXT DEFAULT 'connecting'", "content_id":"TEXT"})
    cur2.execute("""CREATE TABLE IF NOT EXISTS playback_history (
        session_id TEXT PRIMARY KEY,
        username TEXT, content_title TEXT, content_type TEXT, content_id TEXT,
        started_at TEXT, last_heartbeat INTEGER, elapsed_seconds INTEGER,
        playback_status TEXT, ended_at TEXT
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
# 3. PARSER DINÂMICO DE TESTES (MODELO EXATO INFINITY / PANELBR)
# ==========================================================
def format_custom_trial_message(data):
    msg = "*SEGUE OS DADOS DE ACESSO*\n\n"
    msg += f"*Usuário:* {data.get('username','')}                      *Senha:* {data.get('password','')}\n"
    msg += f"*Plano:* {data.get('plan_type','TESTE COMPLETO (1 Tela)')}\n"
    msg += "—————————————————\n"
    pay = data.get('payment_link', '')
    msg += f"🔗 Assinar/Renovar Plano: {pay}\n\n"
    msg += f"🗓️ *Criado em:* {data.get('created_formatted', datetime.now().strftime('%d/%m/%Y %H:%M:%S'))}\n"
    msg += f"🗓️ *Vencimento:* {data.get('expires_formatted','')}\n"
    msg += f"📶 *Conexões:* {data.get('max_connections', '1')}\n\n"
    msg += "—————————————————\n"

    tv = data.get('apps_smart_tv', '').strip()
    if tv:
        for p in tv.split('\n\n'):
            msg += f"{p.strip()}\n—————————————————\n"
    else:
        msg += "🟣*Parceria LOTUS*🟣\n*COD:* infinity\n—————————————————\n"
        msg += "🟢*parceria ZUXO*🟢\n*COD:* BLAZE\n—————————————————\n"
        msg += "🟣  *pareceria RX PURPLE* 🟣\n*COD:* 80822887\n—————————————————\n"
        msg += "🛑 PARCERIA BLESSED🛑\n*COD:* Infinity4097\n—————————————————\n"

    msg += "\n🔵 *LINK DIRETO PRA BAIXAR*\n\n"
    android = data.get('apps_android', '').strip()
    if android:
        for a in android.split('\n\n'):
            msg += f"  🔵 *{a.strip()}*\n—————————————————\n"
    else:
        msg += "  🔵 *App: Infinity XC 2*\n    Link:  http://aftv.news/8203953\n    Cód Downloader: 6414370\n    Pin Ntdow: 59119\n—————————————————\n"
        msg += "    🔵 *App: Infinity Vu*\n    Link:   bit.ly/InfinityVu\n    Cód Downloader: 319740\n     Pin Ntdow: 64566\n"

    return msg.strip()

def parse_trial_api_response(raw_text, cfg):
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
        "plan_type": "TESTE COMPLETO (1 Tela)",
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

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            u = data.get("username")
            p = data.get("password")
            dns = data.get("dns")
            plan = data.get("package")
            pay = data.get("payUrl") or data.get("payment_link")
            exp_formatted = data.get("expiresAtFormatted") or data.get("expiresAt")
            created_formatted = data.get("createdAtFormatted") or data.get("createdAt")
            conns = data.get("connections") or data.get("max_connections")
            reply = data.get("reply") or ""

            if u: res["username"] = str(u).strip()
            if p: res["password"] = str(p).strip()
            if dns: res["dns"] = str(dns).strip().rstrip("/")
            if plan: res["plan_type"] = str(plan).replace("🔹", "").strip()
            if conns: res["max_connections"] = str(conns).strip()
            if pay: res["payment_link"] = str(pay).strip()
            if exp_formatted: res["expires_formatted"] = str(exp_formatted).strip()
            if created_formatted: res["created_formatted"] = str(created_formatted).strip()

            if reply:
                # Extrai parcerias Smart TV
                tv_matches = []
                for p_match in re.findall(r'(\*?Parce?ria[^\n*]+\*?[\s\S]*?\*COD:\*?\s*[^\n]+)', reply, re.IGNORECASE):
                    clean_p = p_match.replace("*", "").replace("—", "").strip()
                    if clean_p: tv_matches.append(clean_p)
                if tv_matches: res["apps_smart_tv"] = "\n\n".join(tv_matches)

                # Extrai apps Android
                and_matches = []
                for a_match in re.findall(r'(App:\s*[^\n*]+[\s\S]*?(?=——|—|\n\n\ud83d|\n\n\u26a0|$))', reply, re.IGNORECASE):
                    clean_a = a_match.replace("*", "").replace("—", "").replace("⚠️", "").replace("▫️", "").replace("🔹", "").replace("⚫️", "").strip()
                    if clean_a: and_matches.append(clean_a)
                if and_matches: res["apps_android"] = "\n\n".join(and_matches)

                m3u_find = re.search(r'(https?://[^\s\"\'<>]+(?:get\.php|\.m3u8?)[^\s\"\'<>]*)', reply, re.I)
                if m3u_find: res["m3u_url"] = m3u_find.group(1).strip()
    except Exception:
        pass

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

    return res

# ==========================================================
# 4. MONITORAMENTO EM TEMPO REAL
# ==========================================================
@app.route("/api/monitor/heartbeat", methods=["POST"])
def api_monitor_heartbeat():
    data = request.get_json(silent=True) or {}
    username = session.get("streamflix_user")
    if not username:
        return jsonify({"ok": False, "error": "Sessão encerrada"}), 401
    mac = str(data.get("mac") or "").strip().upper()[:64]
    title = str(data.get("title") or "Sem título").strip()[:250]
    ctype = str(data.get("type") or "tv").strip()[:24]
    content_id = str(data.get("content_id") or "")[:80]
    status = str(data.get("status") or ("playing" if data.get("playing") else "stopped"))[:32]
    if status not in ("connecting", "playing", "paused", "buffering", "error", "stopped"):
        status = "connecting"
    try:
        elapsed = max(0, min(int(data.get("elapsed") or 0), 365 * 24 * 3600))
    except (ValueError, TypeError):
        elapsed = 0
    client_id = re.sub(r"[^a-zA-Z0-9_-]", "", str(data.get("session_id") or ""))[:64]
    if not client_id:
        return jsonify({"ok": False, "error": "Identificador de sessão ausente"}), 400
    session_id = hashlib.sha256((username + ":" + client_id).encode()).hexdigest()
    forwarded = request.headers.get("X-Forwarded-For", "")
    ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "0.0.0.0"
    ua = request.headers.get("User-Agent", "")
    record_device_entry(mac, ip, ua, username)
    now_ts = int(time.time())
    conn = get_devices_db()
    if status == "stopped":
        conn.execute("DELETE FROM active_monitoring WHERE session_id = ?", (session_id,))
    else:
        # A sessão aparece no monitoramento já durante o carregamento.
        # O indicador de estado evita tratar 'buffering' como vídeo realmente reproduzido.
        row = conn.execute("SELECT content_title, content_id, started_at FROM active_monitoring WHERE session_id=?", (session_id,)).fetchone()
        started = row["started_at"] if row and row["content_id"] == content_id and row["content_title"] == title else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("""INSERT OR REPLACE INTO active_monitoring
           (session_id,username,mac,ip,location,device,content_title,content_type,elapsed_seconds,last_heartbeat,started_at,playback_status,content_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
           (session_id, username, mac, ip, resolve_ip_location(ip), parse_user_agent(ua), title, ctype, elapsed, now_ts, started, status, content_id))
    # Histórico independente da sessão ao vivo, preservando cada título assistido.
    if status == "stopped":
        conn.execute("UPDATE playback_history SET playback_status='stopped', ended_at=?, last_heartbeat=? WHERE session_id=?", (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), now_ts, session_id))
    else:
        conn.execute("""INSERT INTO playback_history
          (session_id,username,content_title,content_type,content_id,started_at,last_heartbeat,elapsed_seconds,playback_status,ended_at)
          VALUES (?,?,?,?,?,?,?,?,?,NULL)
          ON CONFLICT(session_id) DO UPDATE SET
          last_heartbeat=excluded.last_heartbeat,elapsed_seconds=excluded.elapsed_seconds,
          playback_status=excluded.playback_status""",
          (session_id, username, title, ctype, content_id, started, now_ts, elapsed, status))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ==========================================================
# 5. ROTAS DE LOGIN, TESTE GRÁTIS E ADMINISTRAÇÃO
# ==========================================================
@app.route("/api/config")
def api_config():
    cfg = get_settings()
    dns_list = [d.strip().rstrip("/") for d in cfg.get("dns_list", "").splitlines() if d.strip()]
    return jsonify({
        "dns": dns_list[0] if dns_list else "",
        "dns_list": dns_list,
        "support_whatsapp": cfg.get("support_whatsapp", ""),
        "default_payment_link": cfg.get("default_payment_link", "")
    })

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    mac = str(data.get("mac") or "").strip().upper()[:64]
    if not username or not password:
        return jsonify({"ok": False, "error": "Informe usuário e senha."}), 400
    cfg = get_settings()
    dns_list = [d.strip().rstrip("/") for d in cfg.get("dns_list", "").splitlines() if d.strip()]
    conn = get_main_db()
    existing = conn.execute("SELECT * FROM active_users WHERE username=?", (username,)).fetchone()
    now = int(time.time())
    pay = (existing["payment_link"] if existing else "") or cfg.get("default_payment_link", "")
    # Não criar acesso fictício de 30 dias quando os servidores não respondem.
    if existing and existing["dns_used"] and existing["dns_used"] not in dns_list:
        dns_list.insert(0, existing["dns_used"])
    info = None
    chosen_dns = None
    upstream_reached = False
    for dns in dns_list:
        if not dns.startswith(("http://", "https://")):
            dns = "http://" + dns
        try:
            api_url = f"{dns}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "IPTVSmartersPro/3.1.5", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=7, context=SSL_CTX) as resp:
                result = json.loads(resp.read(1024 * 1024).decode("utf-8", errors="replace"))
            if not isinstance(result, dict) or not isinstance(result.get("user_info"), dict):
                continue
            upstream_reached = True
            uinfo = result["user_info"]
            exp = int(uinfo.get("exp_date") or 0) if str(uinfo.get("exp_date") or "0").isdigit() else 0
            state = str(uinfo.get("status") or "").lower()
            if (exp and exp <= now) or state in ("expired", "disabled", "banned"):
                conn.close()
                return jsonify({"ok": False, "expired": True, "payment_link": pay, "error": "Seu acesso venceu."}), 403
            if str(uinfo.get("auth")) == "1" and (state in ("active", "trial", "") or not state):
                info = uinfo
                chosen_dns = dns
                break
        except Exception:
            continue
    if not info:
        cached_exp = int(existing["expires_at"] or 0) if existing else 0
        conn.close()
        if existing and cached_exp and cached_exp <= now:
            return jsonify({"ok": False, "expired": True, "payment_link": pay, "error": "Seu acesso venceu."}), 403
        return jsonify({"ok": False, "error": "Credenciais inválidas ou servidor IPTV indisponível." if upstream_reached else "Não foi possível validar o acesso no servidor IPTV."}), 401
    expires = int(info.get("exp_date") or 0) if str(info.get("exp_date") or "0").isdigit() else 0
    created = existing["created_at_str"] if existing else datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    if str(info.get("created_at") or "").isdigit():
        try: created = datetime.fromtimestamp(int(info["created_at"])).strftime("%d/%m/%Y %H:%M:%S")
        except (ValueError, OverflowError): pass
    m3u = f"{chosen_dns}/get.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}&type=m3u_plus&output=mpegts"
    plan = (existing["plan_type"] if existing else "") or "Assinante Oficial"
    max_conns = str(info.get("max_connections") or "1")
    is_trial = int(existing["is_trial"] or 0) if existing else 0
    conn.execute("""INSERT OR REPLACE INTO active_users
      (username,password,dns_used,m3u_url,plan_type,max_connections,expires_at,payment_link,is_trial,created_at_str)
      VALUES (?,?,?,?,?,?,?,?,?,?)""",
      (username,password,chosen_dns,m3u,plan,max_conns,expires,pay,is_trial,created))
    conn.commit()
    conn.close()
    session["streamflix_user"] = username
    session.permanent = True
    forwarded = request.headers.get("X-Forwarded-For", "")
    record_device_entry(mac, forwarded.split(",")[0].strip() if forwarded else request.remote_addr, request.headers.get("User-Agent", ""), username)
    return jsonify({"ok": True, "username": username, "password": password, "dns": chosen_dns,
      "m3u_url": m3u, "plan_type": plan, "max_connections": max_conns,
      "status": info.get("status") or "Active", "created_formatted": created,
      "expires_at": expires, "expires_formatted": datetime.fromtimestamp(expires).strftime("%d/%m/%Y às %H:%M") if expires else "Sem vencimento",
      "payment_link": pay, "support_whatsapp": cfg.get("support_whatsapp", "")})

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
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"},
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

    if not parsed["username"] or not parsed["password"]:
        conn1.close()
        return jsonify({"ok": False, "error": "A API de testes não forneceu uma conta IPTV válida. Nenhum teste fictício foi registrado."}), 502

    parsed["raw_message"] = format_custom_trial_message(parsed)

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

    session["streamflix_user"] = parsed["username"]
    session.permanent = True
    return jsonify({"ok": True, "trial": parsed})

@app.route("/api/check-session", methods=["POST"])
def api_check_session():
    username = session.get("streamflix_user")
    if not username:
        return jsonify({"valid": False, "expired": False}), 401
    conn = get_main_db()
    row = conn.execute("SELECT * FROM active_users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        session.pop("streamflix_user", None)
        return jsonify({"valid": False, "expired": False}), 401
    exp = int(row["expires_at"] or 0)
    now = int(time.time())
    if exp and exp <= now:
        session.pop("streamflix_user", None)
        return jsonify({"valid": False, "expired": True, "payment_link": row["payment_link"] or get_settings().get("default_payment_link", "")}), 403
    return jsonify({"valid": True, "expired": False, "expires_at": exp,
                    "remaining_seconds": max(0, exp - now) if exp else None,
                    "plan_type": row["plan_type"], "payment_link": row["payment_link"] or get_settings().get("default_payment_link", "")})

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("streamflix_user", None)
    return jsonify({"ok": True})

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
    active_rows = conn2.execute("SELECT * FROM active_monitoring WHERE last_heartbeat >= ? ORDER BY last_heartbeat DESC", (now_ts - 45,)).fetchall()
    active_monitoring = []
    for r in active_rows:
        d = dict(r)
        sec = d.get("elapsed_seconds") or 0
        m, s = divmod(sec, 60)
        h, m = divmod(m, 60)
        d["elapsed_formatted"] = f"{h:02d}:{m:02d}:{s:02d}"
        active_monitoring.append(d)
    conn2.close()
    conn1 = get_main_db()
    trials = [dict(r) for r in conn1.execute("SELECT id, username, plan_type, expires_at, created_at, device_mac, ip_address, payment_link FROM trial_records ORDER BY id DESC LIMIT 500").fetchall()]
    conn1.close()
    conn_hist = get_devices_db()
    history = [dict(r) for r in conn_hist.execute("SELECT username,content_title,content_type,started_at,elapsed_seconds,playback_status FROM playback_history ORDER BY last_heartbeat DESC LIMIT 80").fetchall()]
    conn_hist.close()
    return jsonify({"ok": True, "settings": cfg, "devices": devices, "trials": trials, "monitoring": active_monitoring, "history": history})

@app.route("/api/admin/delete-device/<int:device_id>", methods=["DELETE"])
def api_admin_delete_device(device_id):
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    conn = get_devices_db()
    row = conn.execute("SELECT id FROM devices WHERE id=?", (device_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Dispositivo não encontrado"}), 404
    # Exclui somente o dispositivo escolhido; jamais apaga testes por IP compartilhado.
    conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@app.route("/api/admin/delete-trial/<int:trial_id>", methods=["DELETE"])
def api_admin_delete_trial(trial_id):
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    conn = get_main_db()
    row = conn.execute("SELECT username FROM trial_records WHERE id=?", (trial_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Teste não encontrado"}), 404
    conn.execute("DELETE FROM trial_records WHERE id=?", (trial_id,))
    remaining = conn.execute("SELECT COUNT(*) FROM trial_records WHERE username=?", (row["username"],)).fetchone()[0]
    if remaining == 0:
        conn.execute("DELETE FROM active_users WHERE username=? AND is_trial=1", (row["username"],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": 1})

@app.route("/api/admin/delete-all-trials", methods=["DELETE"])
def api_admin_delete_all_trials():
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    conn = get_main_db()
    count = conn.execute("SELECT COUNT(*) FROM trial_records").fetchone()[0]
    conn.execute("DELETE FROM active_users WHERE is_trial=1")
    conn.execute("DELETE FROM trial_records")
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": count})

@app.route("/api/admin/save", methods=["POST"])
def api_admin_save():
    if not session.get("is_admin"): return jsonify({"ok": False}), 401
    save_settings(request.get_json(silent=True) or {})
    return jsonify({"ok": True})

@app.route("/api/adult/unlock", methods=["POST"])
def api_adult_unlock():
    username = session.get("streamflix_user")
    if not username:
        return jsonify({"ok": False, "error": "Entre na sua conta"}), 401
    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin") or "").strip()
    conn = get_main_db()
    row = conn.execute("SELECT adult_pin FROM active_users WHERE username=?", (username,)).fetchone()
    conn.close()
    expected = (row["adult_pin"] if row else "") or get_settings().get("adult_pin", "0000")
    if not hmac.compare_digest(pin, str(expected)):
        return jsonify({"ok": False, "error": "PIN incorreto"}), 403
    session["adult_unlocked"] = True
    return jsonify({"ok": True})

@app.route("/api/update-adult-pin", methods=["POST"])
def api_update_adult_pin():
    username = session.get("streamflix_user")
    if not username:
        return jsonify({"ok": False, "error": "Faça login primeiro"}), 401
    data = request.get_json(silent=True) or {}
    new_pin = str(data.get("new_pin") or "").strip()
    if len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({"ok": False, "error": "O PIN deve conter 4 dígitos."}), 400
    conn = get_main_db()
    conn.execute("UPDATE active_users SET adult_pin=? WHERE username=?", (new_pin, username))
    conn.commit()
    conn.close()
    session["adult_unlocked"] = False
    return jsonify({"ok": True})

# ==========================================================
# STREAMING E XTREAM POR MESMA ORIGEM (HTTPS/CORS)
# ==========================================================
# A playlist não é exposta via URL arbitrária: somente o DNS autenticado e
# URLs HLS assinadas podem ser acessados. O proxy NÃO transcodifica codecs.
class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Redirecionamento inválido")
        host = parsed.hostname
        if not host:
            raise ValueError("Host ausente")
        # Impede redirecionamentos para localhost/redes privadas (SSRF).
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Redirecionamento para endereço privado bloqueado")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

_public_dns_cache = {}
def _check_public_media_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL de mídia inválida")
    key = (parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    cached = _public_dns_cache.get(key)
    if cached and cached > time.time():
        return
    infos = socket.getaddrinfo(*key, type=socket.SOCK_STREAM)
    if not infos or any(not ipaddress.ip_address(a[4][0]).is_global for a in infos):
        raise ValueError("Endereço de mídia não público")
    if len(_public_dns_cache) > 300:
        _public_dns_cache.clear()
    _public_dns_cache[key] = time.time() + 60

def _remote_open(url, range_header=None):
    _check_public_media_url(url)
    headers = {"User-Agent": "VLC/3.0 StreamFlix", "Accept": "*/*", "Accept-Encoding": "identity"}
    if range_header and re.fullmatch(r"bytes=\d*-\d*", range_header):
        headers["Range"] = range_header
    opener = urllib.request.build_opener(_SafeRedirect(), urllib.request.HTTPSHandler(context=SSL_CTX))
    return opener.open(urllib.request.Request(url, headers=headers), timeout=20)

def _media_user():
    username = session.get("streamflix_user")
    if not username:
        return None
    conn = get_main_db()
    row = conn.execute("SELECT * FROM active_users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row or (int(row["expires_at"] or 0) and int(row["expires_at"]) <= int(time.time())):
        return None
    return row

def _sign_hls_url(url, username):
    expires = int(time.time()) + 6 * 3600
    payload = json.dumps({"url": url, "user": username, "exp": expires}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    mac = hmac.new(app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key, body.encode(), hashlib.sha256).hexdigest()
    return "/api/hls/" + body + "." + mac

def _hls_rewrite(content, remote_url, username):
    out = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            out.append(_sign_hls_url(urljoin(remote_url, stripped), username))
        elif stripped.startswith("#EXT-X-") and 'URI="' in line:
            out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + _sign_hls_url(urljoin(remote_url, m.group(1)), username) + '"', line))
        else:
            out.append(line)
    return "\n".join(out) + "\n"

def _proxy_media(remote_url, username):
    try:
        source = _remote_open(remote_url, request.headers.get("Range"))
    except Exception as exc:
        app.logger.warning("Falha ao abrir mídia: %s", type(exc).__name__)
        return jsonify({"error": "Servidor de mídia indisponível"}), 502
    content_type = source.headers.get("Content-Type", "application/octet-stream").split(";")[0]
    final_url = source.geturl()
    is_playlist = ("mpegurl" in content_type.lower() or urlsplit(final_url).path.lower().endswith(".m3u8"))
    if is_playlist:
        data = source.read(4 * 1024 * 1024 + 1)
        source.close()
        if len(data) > 4 * 1024 * 1024:
            return jsonify({"error": "Playlist muito grande"}), 502
        body = _hls_rewrite(data.decode("utf-8-sig", "replace"), final_url, username)
        return Response(body, mimetype="application/vnd.apple.mpegurl", headers={"Cache-Control": "no-store"})
    status_code = getattr(source, "status", 200)
    def generate():
        try:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            source.close()
    heads = {"Cache-Control": "no-store", "Accept-Ranges": source.headers.get("Accept-Ranges", "bytes")}
    for key in ("Content-Length", "Content-Range"):
        if source.headers.get(key):
            heads[key] = source.headers[key]
    return Response(stream_with_context(generate()), status=status_code, content_type=content_type, headers=heads)

@app.route("/api/xtream")
def api_xtream_proxy():
    row = _media_user()
    if not row: return jsonify({"error": "Acesso encerrado ou vencido"}), 401
    allowed = {"get_live_categories", "get_vod_categories", "get_series_categories", "get_vod_streams", "get_series", "get_live_streams", "get_series_info", "get_vod_info", "get_short_epg"}
    action = request.args.get("action", "")
    if action not in allowed: return jsonify({"error": "Ação não permitida"}), 400
    args = {"username": row["username"], "password": row["password"], "action": action}
    for key in ("series_id", "vod_id", "stream_id"):
        if request.args.get(key) and re.fullmatch(r"[0-9]{1,15}", request.args[key]):
            args[key] = request.args[key]
    url = row["dns_used"].rstrip("/") + "/player_api.php?" + urllib.parse.urlencode(args)
    try:
        with _remote_open(url) as r:
            data = r.read(15 * 1024 * 1024 + 1)
            if len(data) > 15 * 1024 * 1024:
                return jsonify({"error": "Resposta excedeu o limite"}), 502
            return Response(data, content_type="application/json", headers={"Cache-Control": "no-store"})
    except Exception as exc:
        app.logger.warning("Falha na API Xtream: %s", type(exc).__name__)
        return jsonify({"error": "Falha na API do servidor IPTV"}), 502

@app.route("/api/media/<kind>/<stream_id>.<ext>")
def api_media_stream(kind, stream_id, ext):
    row = _media_user()
    if not row: return jsonify({"error": "Acesso encerrado ou vencido"}), 401
    if kind not in ("live", "movie", "series") or not re.fullmatch(r"[a-zA-Z0-9_-]{1,45}", stream_id) or ext.lower() not in ("m3u8", "ts", "mp4", "mkv", "avi", "mov", "m4v", "webm"):
        return jsonify({"error": "Mídia inválida"}), 400
    remote = f"{row['dns_used'].rstrip('/')}/{kind}/{urllib.parse.quote(row['username'])}/{urllib.parse.quote(row['password'])}/{stream_id}.{ext.lower()}"
    return _proxy_media(remote, row["username"])

@app.route("/api/hls/<token>")
def api_hls_resource(token):
    row = _media_user()
    if not row: return jsonify({"error": "Acesso encerrado ou vencido"}), 401
    try:
        body, signature = token.rsplit(".", 1)
        secret = app.secret_key.encode() if isinstance(app.secret_key, str) else app.secret_key
        correct = hmac.new(secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(correct, signature): raise ValueError()
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if payload.get("user") != row["username"] or payload.get("exp", 0) < time.time(): raise ValueError()
        url = payload["url"]
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https"): raise ValueError()
        # URLs encadeadas somente através de playlist assinada, nunca URL arbitrária do cliente.
        return _proxy_media(url, row["username"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return jsonify({"error": "Token de vídeo inválido"}), 403

# ==========================================================
# 6. FRONTEND ESTILO NETFLIX (CONTROLES SLIM, VOD PROGRESS E LISTA TV)
# ==========================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta http-equiv="Cache-Control" content="no-store, max-age=0">
    <title>StreamFlix Pro</title>
    <meta name="streamflix-version" content="2026-11-10-integrado-monitoramento-v2">
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
            transition: all 0.15s cubic-bezier(0.16, 1, 0.3, 1);
            text-decoration: none;
        }
        .btn-pill:active { transform: scale(0.94); }
        .btn-pill-primary {
            background: var(--primary);
            color: #fff;
            box-shadow: 0 4px 16px var(--primary-glow);
        }
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

        /* TELA DE LOGIN (SEM BOTÃO ADMIN EXPOSTO) */
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
            max-width: 460px;
            padding: 1.5rem;
            max-height: min(92dvh, 760px);
            overflow-y: auto;
            overscroll-behavior: contain;
            box-shadow: 0 20px 60px rgba(0,0,0,0.95), 0 0 35px rgba(16, 185, 129, 0.22);
            animation: cardPop 0.32s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
        .trial-app-box { background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:12px; padding:0.85rem; font-size:0.76rem; color:#cbd5e1; white-space:pre-wrap; line-height:1.55; overflow-wrap:anywhere; word-break:normal; margin:1rem 0; text-align:left; font-family:monospace; }
        .trial-app-box a { color:#7dd3fc; text-decoration:underline; text-underline-offset:2px; overflow-wrap:anywhere; }
        @media (max-height:700px) {
            .modal-box { padding:1rem; max-height:94dvh; }
            .trial-app-box { margin:0.65rem 0; max-height:42dvh; overflow-y:auto; }
        }
        
        /* TOPBAR & NAVEGAÇÃO */
        .navbar { height:58px; background:var(--bg-glass); backdrop-filter:blur(12px); border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; padding:0 1rem; position:sticky; top:0; z-index:120; }
        .nav-left { display:flex; align-items:center; gap:0.6rem; }
        .nav-brand { font-weight:900; font-size:1.18rem; letter-spacing:0.5px; }
        .nav-brand span { color:var(--primary); }

        .content-wrap { max-width:1180px; margin:0 auto; padding:0.9rem; }
        .hero-box { position:relative; border-radius:var(--radius-lg); overflow:hidden; height:clamp(220px, 42vh, 340px); min-height:210px; background:#07070c; border:1px solid var(--border); margin-bottom:1.4rem; }
        .hero-bg { position:absolute; inset:0; background-size:cover; background-position:center; transition:background-image 0.4s ease; }
        .hero-overlay { position:absolute; inset:0; background:linear-gradient(0deg, #000 10%, rgba(0,0,0,0.48) 48%, rgba(0,0,0,0.08) 100%); display:flex; flex-direction:column; justify-content:flex-end; align-items:flex-start; padding:clamp(0.75rem, 3vw, 1.25rem); gap:0.45rem; }
        .hero-title { font-size:clamp(1rem, 4vw, 1.35rem); line-height:1.15; font-weight:900; margin:0; max-width:100%; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
        #btn-hero-play { width:50%; min-height:34px; padding:0.35rem 0.65rem; font-size:0.72rem; margin-top:0.2rem; }

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

        /* LISTA VERTICAL DE CANAIS DE TV */
        .tv-channel-list { display:flex; flex-direction:column; gap:0.45rem; }
        .tv-channel-item { display:flex; align-items:center; gap:0.75rem; padding:0.55rem 0.8rem; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius-md); cursor:pointer; transition:border-color 0.15s; }
        .tv-channel-item:active { border-color:var(--primary); transform:scale(0.98); }
        .tv-channel-logo { width:42px; height:42px; border-radius:8px; object-fit:contain; background:#050509; padding:3px; flex-shrink:0; }
        .tv-channel-info { flex:1; overflow:hidden; }
        .tv-channel-info h4 { font-size:0.84rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; margin-bottom:0.15rem; color:#fff; }
        .tv-channel-info span { font-size:0.68rem; color:var(--text-muted); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

        /* DRAWER LATERAL OCULTO */
        .drawer-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.72); z-index:650; opacity:0; pointer-events:none; transition:opacity 0.25s; }
        .drawer-backdrop.active { opacity:1; pointer-events:auto; }
        .drawer-panel { position:fixed; top:0; left:0; bottom:0; width:min(52vw, 270px); min-width:170px; max-width:52vw; background:#07070c; border-right:1px solid var(--border); z-index:660; transform:translateX(-100%); transition:transform 0.28s ease; display:flex; flex-direction:column; }
        .drawer-panel.active { transform:translateX(0); }
        .drawer-header { height:58px; padding:0 1rem; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
        .drawer-list { flex:1; overflow-y:auto; padding:0.6rem; display:flex; flex-direction:column; gap:0.35rem; }
        .drawer-item { padding:0.65rem 0.85rem; border-radius:var(--radius-pill); color:var(--text-muted); background:transparent; border:none; text-align:left; font-size:0.82rem; font-weight:700; cursor:pointer; display:flex; justify-content:space-between; align-items:center; }
        .drawer-item.active { background:var(--primary); color:#fff; }
        .drawer-item:active { transform:scale(0.98); }

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
        body.player-open .bottom-nav.active { display:none; }
        body.player-open.player-list-mode .bottom-nav.active { display:flex; }
        body.player-open.player-controls-visible:not(.player-list-mode) .bottom-nav.active { display:flex; }
        .bnav-item { background:transparent; border:none; color:var(--text-muted); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.2rem; font-size:0.68rem; font-weight:700; cursor:pointer; flex:1; height:100%; }
        .bnav-item.active { color:var(--primary); }

        /* ==========================================================
           PLAYER DE VÍDEO SLIM: CONTROLES OCULTOS DURANTE REPRODUÇÃO
           ========================================================== */
        .player-modal { display:none; position:fixed; inset:0; background:#000; z-index:600; flex-direction:column; overflow:hidden; }
        .player-modal.active { display:flex; }
        .video-stage { position:relative; flex:1; width:100%; height:100%; overflow:hidden; background:#000; display:flex; align-items:center; justify-content:center; cursor:pointer; }

        #video-player {
            width: 100vw;
            height: 100%;
            object-fit: contain;
            background: #000;
        }
        #video-player.mode-fill-horizontal,
        #video-player.mode-vertical-extend {
            width: 100%;
            height: 140%;
            max-height: none;
            object-fit: fill;
            object-position: center;
        }
        .player-modal.tv-list-open .video-stage {
            position:absolute;
            right:0;
            top:50%;
            transform:translateY(-50%);
            width:calc(100% - min(52vw, 270px));
            height:34vh;
            min-height:150px;
            flex:none;
            z-index:5;
            border:1px solid rgba(255,255,255,0.16);
            border-radius:10px 0 0 10px;
        }
        .player-modal.tv-list-open #video-player { width:100%; height:100%; object-fit:contain; }
        .player-modal.tv-list-open .player-top-slim,
        .player-modal.tv-list-open .player-dock-wrap { display:none !important; }
        .player-modal.tv-list-open .epg-bar { display:none !important; }
        .player-modal.tv-list-open #playback-status { font-size:.65rem; padding:5px; max-height:45%; overflow:hidden; }
        .player-modal.tv-list-open .center-play-overlay { pointer-events:none; }
        .player-modal.tv-list-open .video-stage { cursor:pointer; }
        .player-modal:not(.tv-list-open) .player-dock-wrap.visible,
        .player-modal:not(.tv-list-open) .player-top-slim.visible { pointer-events:auto; }
        body.player-open:not(.player-list-mode) .drawer-panel, body.player-open:not(.player-list-mode) .drawer-backdrop { display:none !important; }
        body.player-open .drawer-backdrop.active { background:transparent; pointer-events:none; }
        body.player-open .drawer-panel { width:min(52vw, 270px); max-width:52vw; }
        body.player-open .bottom-nav { z-index:710; }
        body.player-open .bottom-nav .bnav-item[data-tab="config"] { display:none; }
        @media (max-width:380px) {
            .player-modal.tv-list-open .video-stage { height:28vh; min-height:125px; }
            .drawer-panel { min-width:150px; }
        }

        .playback-status { position:absolute; left:50%; top:50%; transform:translate(-50%,-50%); z-index:35; background:rgba(10,10,15,.88); border:1px solid rgba(255,255,255,.16); border-radius:14px; padding:12px 18px; color:#fff; font-size:.78rem; text-align:center; max-width:85%; pointer-events:none; }
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

        /* TOPO SLIM DO PLAYER */
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
            max-width: 50vw;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* DOCK SLIM FLUTUANTE */
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

        /* BARRA DE PROGRESSO (SEEK BAR) EXCLUSIVA PARA VOD */
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
            transition: all 0.15s ease;
            position: relative;
        }
        .mini-btn:active, .mini-btn.active {
            background: var(--primary);
            color: #fff;
            border-color: var(--primary);
            box-shadow: 0 0 12px var(--primary-glow);
            transform: scale(0.92);
        }

        /* POPUP BUBBLE FLUTUANTE */
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
            display: none;
            flex-direction: column;
            gap: 4px;
            box-shadow: 0 14px 35px rgba(0,0,0,0.9);
            min-width: 145px;
            z-index: 60;
            pointer-events: none;
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

        .epg-bar { background:rgba(20,20,30,0.85); border-radius:var(--radius-pill); padding:0.4rem 0.7rem; font-size:0.68rem; display:none; justify-content:space-between; gap:0.5rem; margin-bottom:6px; max-width:92vw; }
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
        /* Ajustes isolados: preserva layout e player StreamFlix da versão integrada. */
        .drawer-panel { width:50vw; min-width:0; max-width:50vw; }
        body.player-open .drawer-panel { width:50vw; min-width:0; max-width:50vw; }
        .player-modal.tv-list-open .video-stage { width:50vw; right:0; top:50%; height:auto; min-height:0; aspect-ratio:16/9; max-height:50vh; transform:translateY(-50%); border-radius:8px; }
        @media (max-width:360px) { .drawer-panel,body.player-open .drawer-panel { width:50vw; min-width:0; max-width:50vw; } .player-modal.tv-list-open .video-stage { width:50vw; height:auto; min-height:0; aspect-ratio:16/9; } }
        body.player-open.player-list-mode .drawer-backdrop.active { pointer-events:auto; background:transparent; }
        body.player-open.player-list-mode .drawer-panel { z-index:670; }
        body.player-open.player-list-mode .bottom-nav.active { z-index:710; }
        /* Mantém a navegação inferior visível somente ao tocar no player,
           com o dock deslocado acima dela; evita sobreposição de botões. */
        body.player-open.player-controls-visible:not(.player-list-mode) .player-dock-wrap {
            bottom:calc(88px + env(safe-area-inset-bottom, 0px));
        }
        .player-modal.tv-list-open .player-dock-wrap,.player-modal.tv-list-open .player-top-slim { display:none !important; }
        .player-modal.tv-list-open .video-stage { cursor:pointer; }
        #video-player.mode-vertical-extend { width:100%; height:100%; object-fit:fill; max-height:100%; transform:none; }
        .video-stage.vertical-fit { height:100%; }
        .series-modal.active { z-index:800; }
        .series-hero { height:min(45vh, 320px); background-size:contain; background-repeat:no-repeat; background-position:center; background-color:#090911; }
        .series-hero-overlay { background:linear-gradient(0deg,var(--bg-surface),transparent 38%); pointer-events:none; }
        .poster-aspect img { object-fit:contain; }
        .hero-bg { background-size:contain; background-repeat:no-repeat; background-color:#050509; }
        .player-dock-wrap { bottom:calc(18px + env(safe-area-inset-bottom, 0px)); }
        .player-dock { max-width:100%; overflow-x:auto; scrollbar-width:none; }
        .player-dock .mini-btn { flex-shrink:0; }
        .drawer-item.now-playing { color:#fff; background:rgba(229,9,20,.25); border:1px solid rgba(229,9,20,.6); }
        .expired-card { background:var(--bg-surface); width:min(92vw,440px); border:1px solid #f87171; border-radius:20px; padding:1.4rem; text-align:center; box-shadow:0 20px 80px rgba(0,0,0,.6); }
        .expired-card h2 { font-size:1.45rem; margin:.5rem 0; }
        .expired-card p { font-size:.86rem; color:var(--text-muted); margin:.65rem 0 1.1rem; line-height:1.5; }
        #access-expired-modal { position:fixed; inset:0; z-index:2000; background:rgba(0,0,0,.96); display:none; align-items:center; justify-content:center; padding:15px; }
        #access-expired-modal.active { display:flex; }
        .expiry-banner { background:rgba(245,158,11,.16); color:#fbbf24; border:1px solid rgba(245,158,11,.4); border-radius:12px; padding:.65rem .85rem; margin:10px; font-size:.78rem; text-align:center; }
        .monitor-status { font-weight:800; font-size:.7rem; }
        .monitor-status.playing { color:#10b981; }
        .monitor-status.connecting,.monitor-status.buffering { color:#fbbf24; }
        .monitor-status.error { color:#f87171; }
        .admin-table td { overflow-wrap:anywhere; }

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
            
            <div class="trial-app-box" id="tr-raw"></div>

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
    <!-- AVISO DE VENCIMENTO: mostrado também ao tentar logar com acesso expirado -->
    <div id="access-expired-modal" role="dialog" aria-modal="true" aria-labelledby="expired-title">
        <div class="expired-card">
            <div style="font-size:2rem;">🔒</div>
            <h2 id="expired-title">Seu acesso venceu.</h2>
            <p>Para voltar a assistir TV, filmes e séries, renove seu acesso.</p>
            <a id="btn-expired-pay" class="btn-pill btn-pill-primary" href="#" target="_blank" rel="noopener noreferrer" style="display:inline-block; padding:.85rem 1.6rem; text-decoration:none;">Liberar acesso</a>
            <p id="expired-pay-info" class="hidden">O link de pagamento não está disponível. Procure o suporte para renovar.</p>
            <div style="margin-top:1rem;"><button type="button" class="btn-pill btn-pill-outline" id="btn-expired-switch">Entrar com outra conta</button></div>
        </div>
    </div>

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
                    <h2 class="hero-title" id="hero-title">Carregando Destaques...</h2>
                    <button class="btn-pill btn-pill-primary" id="btn-hero-play">▶ Assistir Agora</button>
                </div>
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

            <div class="config-card" id="cfg-payment-card">
                <div class="config-title">💳 Pagamento e Renovação</div>
                <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.65rem;">Acesse o link de pagamento associado ao seu acesso para renovar ou contratar um plano.</p>
                <a href="#" target="_blank" rel="noopener noreferrer" class="btn-pill btn-pill-primary hidden" id="btn-config-pay" style="width:100%; background:linear-gradient(135deg, #f59e0b, #d97706); color:#000;">💳 Fazer Pagamento</a>
                <span id="cfg-payment-unavailable" style="font-size:0.75rem; color:var(--text-muted);">Link de pagamento não disponível para este acesso.</span>
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
            <button class="btn-pill btn-pill-outline" id="btn-player-back" aria-label="Voltar" title="Voltar" style="width:34px;height:34px;padding:0;font-size:1.25rem;border-radius:50%;">‹</button>
            <div class="player-title-badge" id="player-title">Reproduzindo...</div>
            <div style="display:flex; gap:0.4rem;">
                <button class="btn-pill btn-pill-outline hidden" id="btn-player-vod-list" style="padding:0.35rem 0.75rem; font-size:0.72rem;">🎬 Voltar a Categoria</button>
            </div>
        </div>

        <div class="video-stage" id="video-stage">
            <video id="video-player" playsinline webkit-playsinline preload="auto"></video>
            <div id="playback-status" class="playback-status hidden" role="status" aria-live="polite"></div>
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
                <button class="bubble-item" id="opt-aspect-fill">Preencher Horizontal</button>
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
                <button class="mini-btn" id="dock-btn-categories" title="Abrir categorias">☰ Categorias</button>
                <button class="mini-btn hidden" id="dock-btn-tv-list" title="Lista de canais">☷</button>
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
                                    <th>Estado</th>
                                    <th>Tempo</th>
                                    <th>IP</th>
                                    <th>Localização</th>
                                    <th>MAC</th>
                                    <th>Dispositivo</th>
                                </tr>
                            </thead>
                            <tbody id="adm-monitoring-tbody">
                                <tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:1rem;">Nenhuma sessão ativa no momento.</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="config-card">
                    <div class="config-title">📊 Histórico de conteúdos assistidos</div>
                    <div style="overflow-x:auto; max-height:240px;overflow-y:auto;">
                        <table class="admin-table"><thead><tr><th>Usuário</th><th>Conteúdo</th><th>Tipo</th><th>Início</th><th>Tempo</th><th>Estado</th></tr></thead>
                        <tbody id="adm-history-tbody"><tr><td colspan="6">Nenhum histórico ainda.</td></tr></tbody></table>
                    </div>
                </div>
                <!-- EXCLUSÃO INDIVIDUAL DE TESTES: não usa IP/MAC compartilhado -->
                <div class="config-card">
                    <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:.6rem;">
                        <div class="config-title" style="margin:0;">🧪 Testes gerados</div>
                        <button id="btn-admin-delete-all-trials" class="btn-pill btn-pill-outline" style="color:#fca5a5; border-color:#ef4444;">🗑️ Remover todos os testes</button>
                    </div>
                    <p style="font-size:.72rem; color:var(--text-muted); margin:.5rem 0;">A lixeira exclui somente o teste selecionado. A exclusão de todos exige confirmação.</p>
                    <div style="overflow-x:auto; max-height:350px; overflow-y:auto;">
                        <table class="admin-table"><thead><tr><th>ID</th><th>Usuário</th><th>Criado</th><th>Vencimento</th><th>Ação</th></tr></thead>
                        <tbody id="adm-trials-tbody"><tr><td colspan="5">Carregando...</td></tr></tbody></table>
                    </div>
                </div>

                <!-- BANCO SEPARADO: REGISTRO DE MACs, IPs E LOCALIZAÇÃO COM BOTÃO DELETAR QUE LIBERA NOVO TESTE -->
                <div class="config-card">
                    <div class="config-title">🛡️ Banco de Dispositivos Registrados (MAC, IP e Localização)</div>
                    <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">Dispositivos no banco <code>devices_tracking.db</code>. A lixeira de dispositivo remove somente a linha selecionada. Para liberar um teste específico, exclua-o na tabela de testes acima.</p>
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
        if (window.lucide && typeof window.lucide.createIcons === "function") window.lucide.createIcons();

        let activeTab = "home";
        let activeCategory = "Todas";
        let currentAuth = null;
        try { currentAuth = JSON.parse(localStorage.getItem("sf_session") || "null"); }
        catch (_) { localStorage.removeItem("sf_session"); }
        let serverConfig = {};
        let xtreamCategories = { tv: [], movies: [], series: [] };
        let catalog = { tv: [], movies: [], series: [] };
        let currentPlayingItem = null;
        let hlsInstance = null;
        let mpegtsInstance = null;
        let adultPin = "0000";
        let monitorInterval = null;
        let playSecondsCounter = 0;
        let currentActiveEngine = "auto";
        let dockTimeout = null;
        let playbackWatchdog = null;
        let playbackAttempt = 0;
        let playbackCandidates = [];
        let playbackCandidateIndex = 0;
        let playbackToken = 0;
        let playbackSwitching = false;
        let playbackAttemptStarted = 0;
        let playbackLastError = "";

        const ADULT_REGEX = /(?:18\s*\+|\+\s*18|\b18\s*anos\b|\badult[oa]s?\b|\bxxx\b|\bporn\w*\b|\berotic\w*\b|\bsexy\b|\bplayboy\b|\bvenus\b|\bfor adults\b)/i;
        let adultUnlocked = false;
        function isAdultCategory(name) { return ADULT_REGEX.test(String(name || "")); }
        async function requestAdultAccess() {
            const pin = prompt("🔒 Pasta protegida: digite o PIN de 4 dígitos:");
            if (pin === null) return false;
            try {
                const resp = await fetch("/api/adult/unlock", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({pin})});
                const data = await resp.json();
                if (data.ok) { adultUnlocked = true; return true; }
                alert(data.error || "PIN incorreto!");
            } catch (_) { alert("Não foi possível validar o PIN."); }
            return false;
        }

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

        // DECODIFICADOR ROBUSTO DE EPG
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
                if (serverConfig.support_whatsapp) {
                    document.getElementById("btn-support-wa").href = serverConfig.support_whatsapp;
                }
            } catch (e) {}
        }
        loadPublicConfig();

        function xtreamUrl(action, extra = {}) {
            const params = new URLSearchParams({action, ...extra});
            return `/api/xtream?${params.toString()}`;
        }
        function mediaUrl(kind, id, ext) {
            return `/api/media/${kind}/${encodeURIComponent(String(id))}.${encodeURIComponent(String(ext || 'mp4').toLowerCase())}`;
        }
        function escapeHtml(value) {
            return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }

        // HERO CAROUSEL 5s
        let heroItems = [];
        let heroIdx = 0;
        let heroMs = 0;
        setInterval(() => {
            if (activeTab !== "home" || document.getElementById("player-modal").classList.contains("active")) return;
            heroMs += 100;
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
            document.getElementById("btn-hero-play").onclick = () => {
                if (it.type === 'movies') openTmdbDetails(it);
                else if (it.type === 'series') openSeriesDetails(it);
                else openItemPlay(it);
            };
        }

        // CARREGA DADOS XTREAM CODES DIRETO DO SERVIDOR (BASE ORIGINAL COMPROVADA v5)
        async function fetchXtreamData() {
            let dns = (currentAuth && currentAuth.dns) || serverConfig.dns;
            if (!currentAuth || !dns) {
                alert("Servidor IPTV não configurado. Verifique o DNS na Área Admin.");
                return;
            }
            dns = dns.replace(/\/$/, "");
            if (!dns.startsWith("http://") && !dns.startsWith("https://")) {
                dns = "http://" + dns;
            }
            const u = encodeURIComponent(currentAuth.username);
            const p = encodeURIComponent(currentAuth.password);

            const cover = document.getElementById("loading-cover");
            const msgEl = document.getElementById("loading-msg");
            cover.classList.add("active");

            try {
                msgEl.textContent = "Obtendo categorias do servidor...";
                const [rTvCat, rVodCat, rSerCat] = await Promise.all([
                    fetch(xtreamUrl("get_live_categories")).catch(()=>({json:()=>[]})),
                    fetch(xtreamUrl("get_vod_categories")).catch(()=>({json:()=>[]})),
                    fetch(xtreamUrl("get_series_categories")).catch(()=>({json:()=>[]}))
                ]);
                xtreamCategories.tv = await rTvCat.json().catch(()=>[]);
                xtreamCategories.movies = await rVodCat.json().catch(()=>[]);
                xtreamCategories.series = await rSerCat.json().catch(()=>[]);

                const vodCatMap = {};
                (Array.isArray(xtreamCategories.movies) ? xtreamCategories.movies : []).forEach(c => vodCatMap[c.category_id] = c.category_name);

                const serCatMap = {};
                (Array.isArray(xtreamCategories.series) ? xtreamCategories.series : []).forEach(c => serCatMap[c.category_id] = c.category_name);

                const liveCatMap = {};
                (Array.isArray(xtreamCategories.tv) ? xtreamCategories.tv : []).forEach(c => liveCatMap[c.category_id] = c.category_name);

                msgEl.textContent = "Sincronizando conteúdos...";
                const [rVod, rSer, rLive] = await Promise.all([
                    fetch(xtreamUrl("get_vod_streams")).catch(()=>({json:()=>[]})),
                    fetch(xtreamUrl("get_series")).catch(()=>({json:()=>[]})),
                    fetch(xtreamUrl("get_live_streams")).catch(()=>({json:()=>[]}))
                ]);
                const vods = await rVod.json().catch(()=>[]);
                const series = await rSer.json().catch(()=>[]);
                const live = await rLive.json().catch(()=>[]);

                catalog.movies = (Array.isArray(vods) ? vods : []).map(v => ({
                    id: v.stream_id, name: v.name, group: v.category_name || vodCatMap[v.category_id] || "Filmes", category_id: v.category_id,
                    type: "movies", logo: v.stream_icon, container_extension: v.container_extension || "mp4",
                    rating: v.rating, tmdb_id: v.tmdb_id,
                    url: mediaUrl("movie", v.stream_id, v.container_extension || "mp4")
                }));

                catalog.series = (Array.isArray(series) ? series : []).map(s => ({
                    id: s.series_id, name: s.name, group: s.category_name || serCatMap[s.category_id] || "Séries", category_id: s.category_id,
                    type: "series", logo: s.cover, rating: s.rating, tmdb_id: s.tmdb_id,
                    url: ""
                }));

                catalog.tv = (Array.isArray(live) ? live : []).map(l => ({
                    id: l.stream_id, name: l.name, group: l.category_name || liveCatMap[l.category_id] || "Canais", category_id: l.category_id,
                    type: "tv", logo: l.stream_icon, epg_id: l.epg_channel_id,
                    url: mediaUrl("live", l.stream_id, "ts")
                }));

                const hm = catalog.movies.filter(it => !isAdultItem(it)).slice(0, 8);
                const hs = catalog.series.filter(it => !isAdultItem(it)).slice(0, 8);
                heroItems = [];
                for (let i=0; i<Math.max(hm.length, hs.length); i++) {
                    if (hm[i]) heroItems.push(hm[i]);
                    if (hs[i]) heroItems.push(hs[i]);
                }
                if (heroItems.length > 0) renderHeroSlide(heroItems[0]);

                renderHomeCarousels();
            } catch (e) {
                console.error("Erro no carregamento Xtream:", e);
                alert("Falha ao comunicar com o servidor IPTV. Verifique o DNS e credenciais.");
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
                const r = await fetch(xtreamUrl("get_series_info", {series_id: it.id}));
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
                        const playUrl = mediaUrl("series", ep.id, epExt);

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
                    const r = await fetch(xtreamUrl("get_vod_info", {vod_id: it.id}));
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

        // MINIPLAYER PERSISTENTE E DRAWER DE CATEGORIAS (50% / 50%)
        function setTvMiniMode(enabled) {
            const modal = document.getElementById("player-modal");
            modal.classList.toggle("tv-list-open", enabled);
            document.body.classList.toggle("player-list-mode", enabled);
            document.body.classList.remove("player-controls-visible");
            if (enabled) {
                document.getElementById("player-dock-wrap").classList.remove("visible");
                document.getElementById("player-top-slim").classList.remove("visible");
                closeAllBubbles();
            }
        }
        function ensureSplitPlayer() {
            document.getElementById("player-modal").classList.add("active");
            document.body.classList.add("player-open");
            setTvMiniMode(true);
        }
        function expandTvPlayer() {
            if (!currentPlayingItem) return;
            closeDrawer();
            setTvMiniMode(false);
            resetDockTimer();
        }
        function showDrawer() {
            document.getElementById("drawer-backdrop").classList.add("active");
            document.getElementById("drawer-panel").classList.add("active");
        }
        // O backdrop transparente cobre o espaço vazio, inclusive a área do miniplayer.
        document.getElementById("drawer-backdrop").onclick = () => {
            if (document.getElementById("player-modal").classList.contains("tv-list-open")) expandTvPlayer();
            else closeDrawer();
        };
        // Tocar diretamente o vídeo também pode expandir quando o backdrop não interceptar.
        document.getElementById("player-modal").addEventListener("click", (e) => {
            if (document.getElementById("player-modal").classList.contains("tv-list-open") &&
                !e.target.closest(".player-dock-wrap,.player-top-slim")) expandTvPlayer();
        });
        function appendBackToCategories(listEl, tab) {
            const back = document.createElement("button");
            back.className = "drawer-item";
            back.textContent = "‹ Voltar às categorias";
            back.onclick = () => openCategoriesDrawer(tab);
            listEl.appendChild(back);
        }
        function openTvChannelList(catName) {
            activeTab = "tv";
            ensureSplitPlayer();
            const listEl = document.getElementById("drawer-list");
            listEl.replaceChildren();
            document.getElementById("drawer-title").textContent = `Canais • ${catName}`;
            appendBackToCategories(listEl, "tv");
            const filtered = catalog.tv.filter(it => (catName === "Todas" || it.group === catName) && (!isAdultItem(it) || adultUnlocked));
            filtered.forEach(it => {
                const b = document.createElement("button");
                b.className = "drawer-item";
                b.style.cssText = "padding:.5rem .35rem; gap:.45rem; justify-content:flex-start; min-height:50px;";
                const selected = currentPlayingItem && currentPlayingItem.type === "tv" && String(currentPlayingItem.id) === String(it.id);
                if (selected) b.classList.add("now-playing");
                b.innerHTML = `<img src="${escapeHtml(it.logo || '')}" alt="" style="width:32px;height:32px;object-fit:contain;flex-shrink:0;border-radius:5px" onerror="this.style.display='none'"><span style="font-size:.74rem;overflow:hidden;overflow-wrap:anywhere;text-align:left">${escapeHtml(it.name)}</span>`;
                b.onclick = (e) => {
                    e.stopPropagation();
                    // Somente segundo toque no MESMO canal expande. Troca de canal mantém lista.
                    if (currentPlayingItem && currentPlayingItem.type === "tv" && String(currentPlayingItem.id) === String(it.id)) {
                        expandTvPlayer();
                    } else {
                        openItemPlay(it);
                        listEl.querySelectorAll(".now-playing").forEach(x => x.classList.remove("now-playing"));
                        b.classList.add("now-playing");
                        showDrawer();
                        setTvMiniMode(true);
                    }
                };
                listEl.appendChild(b);
            });
            showDrawer();
        }
        function openMediaCategoryList(tab, catName) {
            ensureSplitPlayer();
            const listEl = document.getElementById("drawer-list");
            listEl.replaceChildren();
            document.getElementById("drawer-title").textContent = `${tab === 'movies' ? 'Filmes' : 'Séries'} • ${catName}`;
            appendBackToCategories(listEl, tab);
            catalog[tab].filter(it => (catName === "Todas" || it.group === catName) && (!isAdultItem(it) || adultUnlocked)).slice(0,500).forEach(it => {
                const b = document.createElement("button");
                b.className = "drawer-item";
                b.style.cssText = "gap:.5rem;justify-content:flex-start;min-height:50px;padding:.45rem .4rem";
                b.innerHTML = `<img src="${escapeHtml(it.logo || '')}" alt="" style="width:32px;height:40px;object-fit:contain;flex-shrink:0" onerror="this.style.display='none'"><span style="overflow-wrap:anywhere;font-size:.74rem;text-align:left">${escapeHtml(it.name)}</span>`;
                b.onclick = (e) => { e.stopPropagation(); if (tab === "movies") openTmdbDetails(it); else openSeriesDetails(it); };
                listEl.appendChild(b);
            });
            showDrawer();
        }
        function openCategoriesDrawer(forceTab = null) {
            if (forceTab) activeTab = forceTab;
            const tab = activeTab;
            if (!["tv", "movies", "series"].includes(tab)) return;
            const isSplit = document.getElementById("player-modal").classList.contains("active");
            if (isSplit) ensureSplitPlayer();
            const list = document.getElementById("drawer-list");
            list.replaceChildren();
            const cats = xtreamCategories[tab] || [];
            const label = tab === "tv" ? "TV ao Vivo" : tab === "movies" ? "Filmes" : "Séries";
            document.getElementById("drawer-title").textContent = `Categorias (${label})`;
            const makeCategory = (name) => {
                const button = document.createElement("button");
                button.className = `drawer-item ${activeCategory === name ? 'active' : ''}`;
                button.textContent = name === "Todas" ? "Todas as Categorias" : name;
                button.onclick = async (e) => {
                    e.stopPropagation();
                    if (isAdultCategory(name) && !adultUnlocked && !(await requestAdultAccess())) return;
                    activeCategory = name;
                    if (tab === "tv") openTvChannelList(name);
                    else if (isSplit) openMediaCategoryList(tab,name);
                    else { selectCategory(name); closeDrawer(); }
                };
                list.appendChild(button);
            };
            makeCategory("Todas");
            const adultCats = cats.filter(c => isAdultCategory(c.category_name));
            if (adultCats.length) {
                const folder = document.createElement("button");
                folder.className = "drawer-item";
                folder.textContent = "🔒 Pasta protegida (18+)";
                folder.onclick = async () => {
                    if (!adultUnlocked && !(await requestAdultAccess())) return;
                    list.replaceChildren();
                    appendBackToCategories(list, tab);
                    adultCats.forEach(c => makeCategory(c.category_name));
                };
                list.appendChild(folder);
            }
            cats.filter(c => !isAdultCategory(c.category_name)).forEach(c => makeCategory(c.category_name));
            showDrawer();
        }
        function closeDrawer() {
            document.getElementById("drawer-backdrop").classList.remove("active");
            document.getElementById("drawer-panel").classList.remove("active");
        }
        document.getElementById("btn-open-drawer").onclick = () => openCategoriesDrawer();
        document.getElementById("btn-close-drawer").onclick = () => {
            if (document.getElementById("player-modal").classList.contains("tv-list-open") && currentPlayingItem) expandTvPlayer();
            else if (document.getElementById("player-modal").classList.contains("tv-list-open")) exitPlayer();
            else closeDrawer();
        };
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
                    if (adult && !adultUnlocked) return false;
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
                    if (adult && !adultUnlocked) return false;
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
                div.onclick = () => {
                    if (currentPlayingItem && currentPlayingItem.type === 'tv' && String(currentPlayingItem.id) === String(it.id)) expandTvPlayer();
                    else { openItemPlay(it); openTvChannelList(it.group); }
                };
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
            document.body.classList.add("player-controls-visible");

            if (dockTimeout) clearTimeout(dockTimeout);
            dockTimeout = setTimeout(() => {
                if (!document.querySelector(".dock-bubble.open")) {
                    closeAllBubbles();
                    dockWrap.classList.remove("visible");
                    topSlim.classList.remove("visible");
                    document.body.classList.remove("player-controls-visible");
                }
            }, 3000);
        }

        document.getElementById("video-stage").addEventListener("click", (e) => {
            if (e.target.closest("#center-play-overlay")) return;
            if (document.getElementById("player-modal").classList.contains("tv-list-open")) {
                expandTvPlayer();
                return;
            }
            const dockWrap = document.getElementById("player-dock-wrap");
            if (dockWrap.classList.contains("visible")) {
                closeAllBubbles();
                dockWrap.classList.remove("visible");
                document.getElementById("player-top-slim").classList.remove("visible");
                document.body.classList.remove("player-controls-visible");
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

        // Monitoramento por aba/sessão: registra carregando, reproduzindo e buffering.
        const monitorSessionId = (() => {
            try {
                let id = sessionStorage.getItem("sf_monitor_tab_id");
                if (!id) {
                    id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + String(Math.random())).replace(/[^a-zA-Z0-9_-]/g, "");
                    sessionStorage.setItem("sf_monitor_tab_id", id);
                }
                return id;
            } catch (_) { return String(Date.now()) + Math.random().toString(36).slice(2); }
        })();
        let monitorStatus = "stopped";
        let monitorViewId = monitorSessionId;
        let monitorTickTime = Date.now();
        function monitorPayload(status) {
            if (!currentPlayingItem) return null;
            return {
                session_id: monitorViewId,
                mac: getOrCreateMac(),
                title: currentPlayingItem.name || "Sem título",
                type: currentPlayingItem.type,
                content_id: currentPlayingItem.id || currentPlayingItem.url || "",
                elapsed: currentPlayingItem.type !== "tv" && Number.isFinite(vEl.currentTime) ? Math.floor(vEl.currentTime) : playSecondsCounter,
                status
            };
        }
        function sendMonitorHeartbeat(status = monitorStatus, beacon = false) {
            const data = monitorPayload(status);
            if (!data) return;
            const body = JSON.stringify(data);
            if (beacon && navigator.sendBeacon) {
                navigator.sendBeacon("/api/monitor/heartbeat", new Blob([body], {type:"application/json"}));
            } else {
                fetch("/api/monitor/heartbeat", { method:"POST", headers:{"Content-Type":"application/json"}, body, keepalive: status === 'stopped' }).catch(()=>{});
            }
        }
        function setMonitorStatus(status) {
            if (!currentPlayingItem) return;
            if (monitorStatus !== status) {
                monitorStatus = status;
                sendMonitorHeartbeat(status);
            }
        }
        function startMonitor() {
            monitorTickTime = Date.now();
            if (monitorInterval) clearInterval(monitorInterval);
            monitorInterval = setInterval(() => {
                const now = Date.now();
                if (monitorStatus === 'playing' && currentPlayingItem && currentPlayingItem.type === 'tv')
                    playSecondsCounter += Math.min(10, Math.max(0, (now - monitorTickTime) / 1000));
                monitorTickTime = now;
                if (currentPlayingItem) sendMonitorHeartbeat();
            }, 7000);
        }
        window.addEventListener("pagehide", () => {
            if (currentPlayingItem) sendMonitorHeartbeat("stopped", true);
        });

        function setPlaybackStatus(message) {
             const el = document.getElementById("playback-status");
             el.textContent = message || "";
             el.classList.toggle("hidden", !message);
         }

         function clearPlaybackWatchdog() {
             if (playbackWatchdog) clearTimeout(playbackWatchdog);
             playbackWatchdog = null;
         }

        // Preserva os três mecanismos originais; todas as URLs são da mesma origem.
        function makePlaybackCandidates(item, requestedEngine) {
            const url = item.url || "";
            const isTs = /\.ts(?:[?#]|$)/i.test(url);
            const isHls = /\.m3u8(?:[?#]|$)/i.test(url);
            const hlsUrl = isTs ? url.replace(/\.ts(?=[?#]|$)/i, ".m3u8") : url;
            const choices = [];
            const add = (engine, link) => { if (link && !choices.some(c => c.engine === engine && c.url === link)) choices.push({engine,url:link}); };
            if (requestedEngine === "native") add("native", url);
            else if (requestedEngine === "hls") { add("hls", hlsUrl); if (isTs) add("mpegts",url); }
            else if (requestedEngine === "mpegts") { add("mpegts",url); add("native",url); }
            else if (isHls) { add("hls",url); add("native",url); }
            else if (item.type === "tv" && isTs) {
                // Em dispositivos Android, HLS costuma funcionar melhor que TS contínuo.
                add("hls",hlsUrl); add("mpegts",url); add("native",url);
            } else if (isTs) { add("mpegts",url); add("native",url); }
            else add("native",url);
            return choices;
        }

        let candidateFailure = null;
        function tryPlaybackCandidate() {
            clearPlaybackWatchdog();
            playbackSwitching = true;
            const token = ++playbackToken;
            // Desmontagem protegida: eventos de 'error' e 'pause' antigos não iniciam outro player.
            stopPlayerEngines(false);
            if (playbackCandidateIndex >= playbackCandidates.length) {
                playbackSwitching = false;
                setMonitorStatus("error");
                console.warn("StreamFlix: todas as tentativas de vídeo falharam", playbackLastError);
                // Apenas falha FINAL exibe aviso; nenhuma informação durante o carregamento.
                setPlaybackStatus("Não foi possível reproduzir este conteúdo. Toque na tela para ver os controles ou tente outro decodificador.");
                return;
            }
            const candidate = playbackCandidates[playbackCandidateIndex++];
            let failed = false;
            playbackAttemptStarted = Date.now();
            setPlaybackStatus("");
            setMonitorStatus("connecting");
            document.getElementById("center-play-overlay").classList.add("hidden");
            const fail = reason => {
                if (failed || token !== playbackToken) return;
                failed = true;
                playbackLastError = String(reason || "formato indisponível");
                playbackSwitching = true;
                clearPlaybackWatchdog();
                candidateFailure = null;
                setTimeout(() => { if (token === playbackToken) tryPlaybackCandidate(); }, 175);
            };
            candidateFailure = fail;
            const attemptPlay = () => {
                if (failed || token !== playbackToken) return;
                const result = vEl.play();
                if (result && typeof result.catch === "function") result.catch(error => {
                    if (failed || token !== playbackToken) return;
                    if (error && error.name === "NotAllowedError") {
                        // Bloqueio de autoplay: permite toque no botão central sem mudar de canal.
                        clearPlaybackWatchdog();
                        document.getElementById("center-play-overlay").classList.remove("hidden");
                        setMonitorStatus("paused");
                    } else if (error && error.name !== "AbortError") fail(error.name || "falha no play");
                });
            };
            playbackWatchdog = setTimeout(() => {
                if (token === playbackToken && vEl.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) fail("tempo limite para receber vídeo");
            }, 18000);
            playbackSwitching = false;
            try {
                if (candidate.engine === "hls") {
                    if (typeof Hls !== "undefined" && Hls.isSupported()) {
                        hlsInstance = new Hls({enableWorker:true, lowLatencyMode:currentPlayingItem.type === "tv", maxBufferLength:18, backBufferLength:25});
                        hlsInstance.on(Hls.Events.MANIFEST_PARSED, attemptPlay);
                        hlsInstance.on(Hls.Events.ERROR, (_, err) => {
                            if (err.fatal) fail(err.details || err.type || "erro HLS");
                        });
                        hlsInstance.attachMedia(vEl);
                        hlsInstance.loadSource(candidate.url);
                    } else if (vEl.canPlayType("application/vnd.apple.mpegurl")) {
                        vEl.src = candidate.url; attemptPlay();
                    } else fail("HLS não compatível com navegador");
                } else if (candidate.engine === "mpegts") {
                    if (typeof mpegts === "undefined" || !mpegts.isSupported()) { fail("MPEG-TS indisponível"); return; }
                    mpegtsInstance = mpegts.createPlayer({type:"mpegts", isLive:currentPlayingItem.type === "tv", url:candidate.url, cors:true},
                        {enableWorker:false, enableStashBuffer:currentPlayingItem.type !== "tv", stashInitialSize:384});
                    mpegtsInstance.on(mpegts.Events.ERROR, (type,detail) => fail(detail || type));
                    mpegtsInstance.attachMediaElement(vEl);
                    mpegtsInstance.load();
                    attemptPlay();
                } else {
                    vEl.src = candidate.url;
                    vEl.load();
                    attemptPlay();
                }
            } catch(error) {
                console.error("Falha no decodificador", error);
                fail(error.message || "erro interno");
            }
        }

        function openItemPlay(it, forceEngine = "auto") {
            if (!it || !it.url) { alert("Endereço de reprodução indisponível."); return; }
            const miniAlreadyOpen = document.getElementById("player-modal").classList.contains("tv-list-open");
            playbackToken++;
            playbackSwitching = true;
            stopPlayerEngines(true);
            currentPlayingItem = it;
            monitorViewId = monitorSessionId + "_" + Math.random().toString(36).slice(2,10);
            playSecondsCounter = 0;
            currentActiveEngine = forceEngine;
            document.getElementById("player-modal").classList.add("active");
            document.body.classList.add("player-open");
            // TV sempre permanece compacta após selecionar um canal; VOD segue
            // a modalidade anterior apenas se o usuário tiver aberto as categorias.
            setTvMiniMode(it.type === "tv" || miniAlreadyOpen);
            document.getElementById("player-title").textContent = it.name || "Reproduzindo";
            document.getElementById("center-play-overlay").classList.add("hidden");
            vEl.className = "";
            closeAllBubbles();
            document.getElementById("dock-btn-tv-list").classList.toggle("hidden", it.type !== "tv");
            document.getElementById("btn-player-vod-list").classList.toggle("hidden", it.type === "tv");
            document.getElementById("dock-btn-tv-list").onclick = event => {
                event.stopPropagation(); openTvChannelList(currentPlayingItem.group);
            };
            document.getElementById("dock-btn-categories").onclick = event => {
                event.stopPropagation();
                openCategoriesDrawer(currentPlayingItem.type);
            };
            document.getElementById("btn-player-vod-list").onclick = event => {
                event.stopPropagation();
                openCategoriesDrawer(currentPlayingItem.type);
            };
            const seek = document.getElementById("vod-seek-wrap");
            seek.classList.toggle("active", it.type !== "tv");
            document.getElementById("seek-cur-time").textContent = "00:00";
            document.getElementById("seek-total-time").textContent = "00:00";
            document.getElementById("seek-track-fill").style.width = "0%";
            document.getElementById("opt-vlc-ext").href = it.url;
            document.getElementById("epg-bar").classList.toggle("active", it.type === "tv");
            if (it.type === "tv") fetchEpg(it);
            playbackCandidates = makePlaybackCandidates(it, forceEngine);
            playbackCandidateIndex = 0;
            playbackLastError = "";
            monitorStatus = "connecting";
            sendMonitorHeartbeat("connecting");
            startMonitor();
            tryPlaybackCandidate();
            if (it.type !== "tv" && !miniAlreadyOpen) resetDockTimer();
        }

        document.getElementById("center-play-overlay").onclick = event => {
            event.stopPropagation();
            if (document.getElementById("player-modal").classList.contains("tv-list-open")) expandTvPlayer();
            document.getElementById("center-play-overlay").classList.add("hidden");
            vEl.play().catch(error => console.warn("Toque para reproduzir:", error.name));
            resetDockTimer();
        };

        // EPG DECODIFICADO CORRETAMENTE
        async function fetchEpg(it) {
            if (!currentAuth || !it.id) return;
            try {
                let dns = currentAuth.dns.replace(/\/$/, "");
                if (!dns.startsWith("http://") && !dns.startsWith("https://")) dns = "http://" + dns;
                const r = await fetch(xtreamUrl("get_short_epg", {stream_id: it.id}));
                const d = await r.json();
                const prog = (d.epg_listings && d.epg_listings[0]) || {};
                
                const rawTitle = prog.title || it.name;
                const decodedTitle = decodeEpgString(rawTitle);
                
                document.getElementById("epg-now").textContent = `Agora: ${decodedTitle}`;
                const onlyClock = (value) => {
                    if (!value) return "";
                    const match = String(value).match(/(?:T|\s)(\d{2}:\d{2})(?::\d{2})?/);
                    return match ? match[1] : (String(value).match(/\b\d{2}:\d{2}\b/) || [""])[0];
                };
                const startClock = onlyClock(prog.start);
                const endClock = onlyClock(prog.end);
                document.getElementById("epg-time").textContent = startClock && endClock ? `${startClock} - ${endClock}` : "Ao Vivo";
            } catch (e) {
                document.getElementById("epg-now").textContent = `Agora: ${it.name}`;
            }
        }

        function stopPlayerEngines(clearMonitoring = true) {
            clearPlaybackWatchdog();
            if (clearMonitoring && monitorInterval) { clearInterval(monitorInterval); monitorInterval = null; }
            if (clearMonitoring && currentPlayingItem) {
                monitorStatus = "stopped";
                sendMonitorHeartbeat("stopped");
            }
            playbackSwitching = true;
            candidateFailure = null;
            if (hlsInstance) { try { hlsInstance.destroy(); } catch (_) {} hlsInstance = null; }
            if (mpegtsInstance) { try { mpegtsInstance.destroy(); } catch (_) {} mpegtsInstance = null; }
            try { vEl.pause(); vEl.removeAttribute("src"); vEl.load(); } catch(_) {}
        }
        vEl.addEventListener("playing", () => {
            if (playbackSwitching || !currentPlayingItem) return;
            clearPlaybackWatchdog();
            setPlaybackStatus("");
            document.getElementById("center-play-overlay").classList.add("hidden");
            document.getElementById("dock-btn-play").textContent = "⏸";
            setMonitorStatus("playing");
            if (!monitorInterval) startMonitor();
        });
        vEl.addEventListener("waiting", () => { if (!playbackSwitching && currentPlayingItem) setMonitorStatus("buffering"); });
        vEl.addEventListener("stalled", () => { if (!playbackSwitching && currentPlayingItem) setMonitorStatus("buffering"); });
        vEl.addEventListener("pause", () => {
            if (playbackSwitching || !currentPlayingItem) return;
            document.getElementById("dock-btn-play").textContent = "▶";
            setMonitorStatus("paused");
        });
        vEl.addEventListener("error", () => {
            if (playbackSwitching || !currentPlayingItem || !document.getElementById("player-modal").classList.contains("active")) return;
            const err = vEl.error;
            if (Date.now() - playbackAttemptStarted < 250) return;
            if (candidateFailure) candidateFailure(err ? `erro HTMLMediaElement ${err.code}` : "falha de mídia");
        });
        function exitPlayer() {
            playbackToken++;
            playbackSwitching = true;
            stopPlayerEngines(true);
            currentPlayingItem = null;
            setPlaybackStatus("");
            document.getElementById("player-modal").classList.remove("active", "tv-list-open");
            document.body.classList.remove("player-open", "player-list-mode", "player-controls-visible");
            if (dockTimeout) clearTimeout(dockTimeout);
            document.getElementById("player-dock-wrap").classList.remove("visible");
            document.getElementById("player-top-slim").classList.remove("visible");
            closeDrawer();
        }
        document.getElementById("btn-player-back").onclick = e => { e.stopPropagation(); exitPlayer(); };

        // CONTROLES SLIM DO DOCK COM BOLHAS INVISÍVEIS DURANTE REPRODUÇÃO
        const btnPlay = document.getElementById("dock-btn-play");
        btnPlay.onclick = (e) => {
            e.stopPropagation();
            closeAllBubbles();
            if (vEl.paused) { vEl.play().catch(err => console.warn("Falha ao iniciar",err)); btnPlay.textContent = "⏸"; }
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
            vEl.className = "mode-vertical-extend";
            document.getElementById("video-stage").classList.add("vertical-fit");
            document.getElementById("opt-aspect-fill").classList.add("active");
            document.getElementById("opt-aspect-fit").classList.remove("active");
            btnAspect.classList.add("active");
            closeAllBubbles();
            resetDockTimer();
        };

        document.getElementById("opt-aspect-fit").onclick = (e) => {
            e.stopPropagation();
            vEl.className = "";
            document.getElementById("video-stage").classList.remove("vertical-fit");
            document.getElementById("opt-aspect-fit").classList.add("active");
            document.getElementById("opt-aspect-fill").classList.remove("active");
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
                // Sai de qualquer modo de reprodução uma única vez. Nunca mantém
                // backdrop/painel interceptando Início/TV/Filmes/Séries.
                if (document.getElementById("player-modal").classList.contains("active")) exitPlayer();
                closeDrawer();
                activeTab = btn.dataset.tab;
                adultUnlocked = false;
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
                }
                window.scrollTo({ top: 0, behavior: "smooth" });
            };
        });

        document.getElementById("btn-global-back").onclick = () => {
            document.querySelector('.bottom-nav .bnav-item[data-tab="home"]').click();
        };

        function fillConfigScreen() {
            if (!currentAuth) return;
            document.getElementById("cfg-user").textContent = currentAuth.username || "-";
            document.getElementById("cfg-pass").textContent = currentAuth.password || "-";
            document.getElementById("cfg-exp").textContent = currentAuth.expires_formatted || "Ativo";
            document.getElementById("cfg-created").textContent = currentAuth.created_formatted || "Servidor Oficial";
            document.getElementById("cfg-plan").textContent = currentAuth.plan_type || "Plano IPTV";
            document.getElementById("cfg-screens").textContent = `${currentAuth.max_connections || '1'} tela(s)`;
            const paymentLink = String(currentAuth.payment_link || "").trim();
            const paymentButton = document.getElementById("btn-config-pay");
            const paymentUnavailable = document.getElementById("cfg-payment-unavailable");
            if (paymentLink && /^https?:\/\//i.test(paymentLink)) {
                paymentButton.href = paymentLink;
                paymentButton.classList.remove("hidden");
                paymentUnavailable.classList.add("hidden");
            } else {
                paymentButton.removeAttribute("href");
                paymentButton.classList.add("hidden");
                paymentUnavailable.classList.remove("hidden");
            }
        }

        document.getElementById("btn-save-pin").onclick = async () => {
            const p = document.getElementById("cfg-adult-pin").value.trim();
            const r = await fetch("/api/update-adult-pin", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ new_pin: p }) });
            const d = await r.json();
            if (d.ok) { adultPin = p; adultUnlocked = false; alert("✅ Senha Adulto atualizada para esta conta!"); }
            else alert(d.error || "Erro ao salvar.");
        };

        document.getElementById("btn-theme-oled").onclick = () => document.body.className = "";
        document.getElementById("btn-theme-midnight").onclick = () => document.body.className = "theme-midnight";
        document.getElementById("btn-clear-cache").onclick = async () => {
            localStorage.clear();
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
            if (!d.ok) {
                if (d.expired) { showExpiredCard(d.payment_link); return; }
                alert(d.error || "Erro ao entrar."); return;
            }
            currentAuth = d;
            localStorage.setItem("sf_session", JSON.stringify(d));
            enterApp();
        };

        // GERAÇÃO DE TESTE GRÁTIS AUTOMÁTICO COM ANIMAÇÃO DE CARD
        document.getElementById("btn-trial").onclick = async () => {
            const btn = document.getElementById("btn-trial");
            btn.textContent = "⏳ Conectando ao servidor e gerando seu teste...";
            btn.disabled = true;

            try {
                const mac = getOrCreateMac();
                const r = await fetch("/api/request-trial", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ device_mac: mac }) });
                const d = await r.json();
                if (!d.ok) {
                    if (d.payment_link && d.already_used) showExpiredCard(d.payment_link);
                    btn.textContent = "⚡ Gerar Teste Grátis Automático";
                    btn.disabled = false;
                    return alert(d.error || "Não foi possível gerar o teste.");
                }

                const tr = d.trial;
                currentAuth = tr;
                localStorage.setItem("sf_session", JSON.stringify(tr));

                // Popula o link real de pagamento da API
                if (tr.payment_link) {
                    const payBtn = document.getElementById("btn-pay-link");
                    payBtn.classList.remove("hidden");
                    payBtn.href = tr.payment_link;
                } else {
                    document.getElementById("btn-pay-link").classList.add("hidden");
                }

                // Popula a mensagem formatada no bloco único
                const shareText = tr.raw_message || ("Usuário: " + tr.username + "\nSenha: " + tr.password);
                const trialBox = document.getElementById("tr-raw");
                trialBox.replaceChildren();
                const urlPattern = /(https?:\/\/[^\s<>"']+)/gi;
                let cursor = 0;
                let match;
                while ((match = urlPattern.exec(shareText)) !== null) {
                    trialBox.appendChild(document.createTextNode(shareText.slice(cursor, match.index)));
                    const cleanUrl = match[0].replace(/[),.;!?]+$/, "");
                    const link = document.createElement("a");
                    link.href = cleanUrl;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                    link.textContent = cleanUrl;
                    trialBox.appendChild(link);
                    if (cleanUrl.length < match[0].length) {
                        trialBox.appendChild(document.createTextNode(match[0].slice(cleanUrl.length)));
                    }
                    cursor = match.index + match[0].length;
                }
                trialBox.appendChild(document.createTextNode(shareText.slice(cursor)));

                document.getElementById("btn-copy-tr").onclick = () => {
                    navigator.clipboard.writeText(shareText);
                    alert("Dados copiados com sucesso!");
                };
                document.getElementById("btn-wa-tr").href = `https://wa.me/?text=${encodeURIComponent(shareText)}`;

                document.getElementById("trial-modal").classList.add("active");
            } catch (e) {
                alert("Erro de conexão ao gerar o teste.");
            } finally {
                btn.textContent = "⚡ Gerar Teste Grátis Automático";
                btn.disabled = false;
            }
        };

        document.getElementById("btn-proceed-tr").onclick = () => {
            document.getElementById("trial-modal").classList.remove("active");
            enterApp();
        };

        function showExpiredCard(link) {
            exitPlayer();
            document.getElementById("trial-modal").classList.remove("active");
            const payBtn = document.getElementById("btn-expired-pay");
            const info = document.getElementById("expired-pay-info");
            const safe = typeof link === "string" && /^https?:\/\//i.test(link);
            payBtn.href = safe ? link : "#";
            payBtn.style.pointerEvents = safe ? "auto" : "none";
            payBtn.style.opacity = safe ? "1" : ".45";
            info.classList.toggle("hidden", safe);
            document.getElementById("access-expired-modal").classList.add("active");
        }
        document.getElementById("btn-expired-switch").onclick = () => {
            currentAuth = null;
            localStorage.removeItem("sf_session");
            fetch("/api/logout", {method:"POST"}).catch(()=>{});
            document.getElementById("access-expired-modal").classList.remove("active");
            document.getElementById("screen-login").classList.add("active");
            document.getElementById("main-navbar").classList.add("hidden");
            document.getElementById("main-content").classList.add("hidden");
            document.getElementById("bottom-nav").classList.remove("active");
        };
        let sessionCheckInterval = null;
        async function validateCurrentSession() {
            if (!currentAuth || !currentAuth.username) return false;
            try {
                const check = await fetch("/api/check-session", {method:"POST"});
                const d = await check.json();
                if (d.expired) { showExpiredCard(d.payment_link || currentAuth.payment_link || serverConfig.default_payment_link); return false; }
                if (d.valid) {
                    if (d.payment_link) currentAuth.payment_link = d.payment_link;
                    if (d.expires_at) currentAuth.expires_at = d.expires_at;
                    return true;
                }
                // Cookie ausente após atualização/reinício: reautenticar usando
                // credenciais da sessão existente, sem criar assinatura artificial.
                const login = await fetch("/api/login", {method:"POST", headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({username:currentAuth.username,password:currentAuth.password,mac:getOrCreateMac()})});
                const result = await login.json();
                if (result.expired) { showExpiredCard(result.payment_link || currentAuth.payment_link); return false; }
                if (!result.ok) return false;
                currentAuth = result;
                localStorage.setItem("sf_session", JSON.stringify(result));
                return true;
            } catch (error) { console.warn("Validação da sessão:", error); return false; }
        }
        async function enterApp() {
            if (!currentAuth) return;
            document.getElementById("loading-cover").classList.add("active");
            const valid = await validateCurrentSession();
            document.getElementById("loading-cover").classList.remove("active");
            if (!valid) {
                if (!document.getElementById("access-expired-modal").classList.contains("active")) {
                    document.getElementById("screen-login").classList.add("active");
                    alert("Não foi possível validar sua conta. Faça login novamente.");
                }
                return;
            }
            document.getElementById("screen-login").classList.remove("active");
            document.getElementById("main-navbar").classList.remove("hidden");
            document.getElementById("main-content").classList.remove("hidden");
            document.getElementById("bottom-nav").classList.add("active");
            if (sessionCheckInterval) clearInterval(sessionCheckInterval);
            sessionCheckInterval = setInterval(async () => {
                if (!currentAuth) return;
                try {
                    const resp = await fetch("/api/check-session", {method:"POST"});
                    const d = await resp.json();
                    if (d.expired) { showExpiredCard(d.payment_link || currentAuth.payment_link); clearInterval(sessionCheckInterval); }
                } catch(_) {}
            }, 60000);
            fetchXtreamData();
        }

        document.getElementById("btn-logout-cfg").onclick = () => {
            localStorage.removeItem("sf_session");
            fetch("/api/logout", {method:"POST",keepalive:true}).finally(()=>location.reload());
        };

        // ROTA ADMINISTRATIVA (INVISÍVEL NA TELA DE LOGIN DO CLIENTE)
        let adminPolling = null;

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
                const actuallyPlaying = d.monitoring.filter(m => m.playback_status === 'playing').length;
                document.getElementById("live-count").textContent = `${actuallyPlaying} assistindo • ${d.monitoring.length} sessão(ões) online`;

                if (d.monitoring.length === 0) {
                    monTbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:1rem;">Nenhuma sessão ativa no momento.</td></tr>`;
                } else {
                    d.monitoring.forEach(m => {
                        const tr = document.createElement("tr");
                        tr.innerHTML = `
                            <td><strong>${escapeHtml(m.username)}</strong></td>
                            <td style="color:var(--gold);">▶ ${escapeHtml(m.content_title)}</td>
                            <td><span class="monitor-status ${escapeHtml(m.playback_status || 'connecting')}">${{playing:'▶ Reproduzindo',connecting:'⏳ Conectando',paused:'⏸ Pausado',buffering:'⌛ Carregando',error:'⚠️ Falha'}[m.playback_status] || '⏳ Conectando'}</span></td>
                            <td><span class="badge-live">${escapeHtml(m.elapsed_formatted)}</span></td>
                            <td>${escapeHtml(m.ip)}</td>
                            <td>${escapeHtml(m.location)}</td>
                            <td><code>${escapeHtml(m.mac)}</code></td>
                            <td>${escapeHtml(m.device)}</td>
                        `;
                        monTbody.appendChild(tr);
                    });
                }

                const historyTbody = document.getElementById("adm-history-tbody");
                historyTbody.replaceChildren();
                (d.history || []).forEach(h => {
                    const row = document.createElement('tr');
                    const seconds = Math.max(0, Number(h.elapsed_seconds || 0));
                    row.innerHTML = `<td>${escapeHtml(h.username)}</td><td>${escapeHtml(h.content_title)}</td>
                      <td>${escapeHtml(h.content_type)}</td><td>${escapeHtml(h.started_at)}</td>
                      <td>${formatTime(seconds)}</td><td>${escapeHtml(h.playback_status)}</td>`;
                    historyTbody.appendChild(row);
                });
                if (!historyTbody.children.length) historyTbody.innerHTML = '<tr><td colspan="6">Nenhum histórico ainda.</td></tr>';

                const trialTbody = document.getElementById("adm-trials-tbody");
                trialTbody.replaceChildren();
                if (!d.trials || !d.trials.length) {
                    trialTbody.innerHTML = '<tr><td colspan="5">Nenhum teste cadastrado.</td></tr>';
                } else {
                    d.trials.forEach(t => {
                        const tr = document.createElement("tr");
                        const exp = t.expires_at ? new Date(t.expires_at * 1000).toLocaleString('pt-BR') : 'Sem data';
                        tr.innerHTML = `<td>${Number(t.id)}</td><td>${escapeHtml(t.username)}</td><td>${escapeHtml(t.created_at)}</td><td>${escapeHtml(exp)}</td>
                            <td><button class="btn-pill btn-pill-outline" style="border-color:#ef4444;color:#fca5a5;padding:.3rem" data-del-trial="${Number(t.id)}">🗑️ Excluir</button></td>`;
                        tr.querySelector('[data-del-trial]').onclick = () => deleteTrialRecord(t.id, t.username);
                        trialTbody.appendChild(tr);
                    });
                }
                const devTbody = document.getElementById("adm-devices-tbody");
                devTbody.innerHTML = "";
                d.devices.forEach(dev => {
                    const tr = document.createElement("tr");
                    tr.innerHTML = `
                        <td><code>${escapeHtml(dev.mac)}</code></td>
                        <td>${escapeHtml(dev.ip)}</td>
                        <td>${escapeHtml(dev.location)}</td>
                        <td>${escapeHtml(dev.device)}</td>
                        <td><strong>${escapeHtml(dev.username)}</strong></td>
                        <td>${escapeHtml(dev.last_seen)}</td>
                        <td><button class="btn-pill btn-pill-outline" style="color:#fca5a5; border-color:#e50914; padding:0.25rem 0.65rem; font-size:0.7rem;" onclick="deleteDeviceRecord(${dev.id})">🗑️ Deletar</button></td>
                    `;
                    devTbody.appendChild(tr);
                });
            } catch (e) {}
        }

        async function deleteTrialRecord(id, user) {
            if (!confirm(`Excluir somente o teste #${id} (${user})? Outros registros não serão apagados.`)) return;
            const res = await fetch(`/api/admin/delete-trial/${id}`, {method:"DELETE"});
            if (!res.ok) { alert("Erro ao excluir teste."); return; }
            loadAdminDashboard();
        }
        document.getElementById("btn-admin-delete-all-trials").onclick = async () => {
            if (!confirm("ATENÇÃO: apagar TODOS os testes cadastrados? Esta ação não pode ser desfeita.")) return;
            const confirmation = prompt('Para confirmar a exclusão de todos os testes, digite EXCLUIR:');
            if (confirmation !== 'EXCLUIR') return;
            const res = await fetch("/api/admin/delete-all-trials", {method:"DELETE"});
            if (!res.ok) { alert("Falha na exclusão coletiva."); return; }
            const d = await res.json();
            alert(`${d.deleted} teste(s) excluído(s).`);
            loadAdminDashboard();
        };

        window.deleteDeviceRecord = async (id) => {
            if (!confirm("Excluir apenas este dispositivo? Os demais dispositivos e testes serão preservados.")) return;
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

@app.route("/api/version")
def api_version():
    return jsonify({"name": "StreamFlix Pro", "version": "2026-11-10-integrado-monitoramento-v2"})

@app.route("/")
def index():
    return Response(render_template_string(HTML_TEMPLATE), content_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store, max-age=0"})

@app.route("/admin")
def admin_direct():
    return Response(render_template_string(HTML_TEMPLATE), content_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store, max-age=0"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8095))
    app.run(host="0.0.0.0", port=port, debug=False)
