import os
import re
import json
import time
import sqlite3
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("STREAMFLIX_SECRET", "streamflix-super-secret-key-2026")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streamflix.db")

# ==========================================================
# 1. BANCO DE DADOS SQLITE (CONFIG, TESTES, USUÁRIOS)
# ==========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cur.execute("""CREATE TABLE IF NOT EXISTS trial_records (
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
    cur.execute("""CREATE TABLE IF NOT EXISTS active_users (
        username TEXT PRIMARY KEY,
        password TEXT,
        dns_used TEXT,
        plan_type TEXT,
        max_connections TEXT,
        expires_at INTEGER,
        payment_link TEXT,
        is_trial INTEGER DEFAULT 0
    )""")

    defaults = {
        "admin_user": "admin",
        "admin_pass": "admin123",
        "dns_list": "http://seu-servidor-iptv.com:80",
        "trial_api_url": "",
        "trial_duration_hours": "4",
        "default_payment_link": "https://pagamento.seudominio.com/assinar",
        "support_whatsapp": "https://wa.me/5500000000000?text=Preciso%20de%20suporte%20no%20StreamFlix",
        "tmdb_api_key": "",
        "adult_pin": "0000",
        "apps_smart_tv": "IBO Player, DuplexPlay, SmartOne, SS IPTV, Bay IPTV",
        "apps_android": "XCIPTV, IPTV Smarters Pro, IBO Player Pro, TiviMate",
        "custom_domain": ""
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

init_db()

def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def save_settings(new_data):
    conn = get_db()
    for k, v in new_data.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(v)))
    conn.commit()
    conn.close()

# ==========================================================
# 2. PARSER DINÂMICO DE TESTES (CAPTURANDO TUDO DO SERVIDOR)
# ==========================================================
def parse_trial_api_response(raw_text, cfg):
    """Extrai dinamicamente dados do servidor IPTV sem fixar valores do admin."""
    now_ts = int(time.time())
    hours = float(cfg.get("trial_duration_hours") or 4)
    fallback_exp_ts = now_ts + int(hours * 3600)
    fallback_dns = (cfg.get("dns_list", "").splitlines()[0] if cfg.get("dns_list") else "").strip().rstrip("/")

    res = {
        "username": "",
        "password": "",
        "dns": fallback_dns,
        "plan_type": "Teste Grátis IPTV",
        "expires_at": fallback_exp_ts,
        "expires_formatted": datetime.fromtimestamp(fallback_exp_ts).strftime("%d/%m/%Y às %H:%M"),
        "payment_link": cfg.get("default_payment_link", ""),
        "apps_smart_tv": cfg.get("apps_smart_tv", ""),
        "apps_android": cfg.get("apps_android", "")
    }

    if not raw_text:
        return res

    # 1. Tenta decodificar como JSON estruturado
    try:
        data = json.loads(raw_text)
        if isinstance(data, dict):
            sub = data.get("data") or data.get("result") or data.get("user_info") or data
            u = sub.get("username") or sub.get("user") or sub.get("login") or data.get("username")
            p = sub.get("password") or sub.get("pass") or sub.get("senha") or data.get("password")
            dns = sub.get("dns") or sub.get("server") or sub.get("url") or data.get("dns")
            plan = sub.get("plan") or sub.get("plan_type") or sub.get("package") or sub.get("pacote") or data.get("plan")
            pay = sub.get("payment_link") or sub.get("checkout_url") or sub.get("pix_url") or data.get("payment_link")
            exp = sub.get("expires_at") or sub.get("exp_date") or sub.get("vencimento") or data.get("expires_at")
            tv_apps = sub.get("apps_smart_tv") or sub.get("smart_tv") or sub.get("tv_apps") or data.get("apps_smart_tv")
            and_apps = sub.get("apps_android") or sub.get("android_apps") or sub.get("apps_mobile") or data.get("apps_android")

            if u: res["username"] = str(u).strip()
            if p: res["password"] = str(p).strip()
            if dns: res["dns"] = str(dns).strip().rstrip("/")
            if plan: res["plan_type"] = str(plan).strip()
            if pay: res["payment_link"] = str(pay).strip()
            if tv_apps: res["apps_smart_tv"] = str(tv_apps).strip()
            if and_apps: res["apps_android"] = str(and_apps).strip()

            if exp:
                try:
                    if str(exp).isdigit():
                        res["expires_at"] = int(exp)
                        res["expires_formatted"] = datetime.fromtimestamp(int(exp)).strftime("%d/%m/%Y às %H:%M")
                    else:
                        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                        res["expires_at"] = int(dt.timestamp())
                        res["expires_formatted"] = dt.strftime("%d/%m/%Y às %H:%M")
                except Exception:
                    pass
            if res["username"] and res["password"]:
                return res
    except Exception:
        pass

    # 2. Fallback por Regex caso a resposta venha em texto formatado
    u_m = re.search(r"(?:usu[aá]rio|user|login)\s*[:=\-]\s*([A-Za-z0-9_.\-]+)", raw_text, re.I)
    p_m = re.search(r"(?:senha|password|pass)\s*[:=\-]\s*([A-Za-z0-9_.\-]+)", raw_text, re.I)
    plan_m = re.search(r"(?:plano|pacote|tipo)\s*[:=\-]\s*([^\r\n,]+)", raw_text, re.I)
    dns_m = re.search(r"(https?://[A-Za-z0-9_.\-]+(?::\d+)?)", raw_text, re.I)
    link_m = re.search(r"(https?://[^\s\"'<>]+(?:pay|checkout|assinar|mpago|pix|fatura)[^\s\"'<>]*)", raw_text, re.I)
    tv_m = re.search(r"(?:smart\s*tv|tv\s*apps?)\s*[:=\-]\s*([^\r\n]+)", raw_text, re.I)
    and_m = re.search(r"(?:android|mobile\s*apps?)\s*[:=\-]\s*([^\r\n]+)", raw_text, re.I)

    if u_m: res["username"] = u_m.group(1).strip()
    if p_m: res["password"] = p_m.group(1).strip()
    if plan_m: res["plan_type"] = plan_m.group(1).strip()
    if dns_m and not link_m: res["dns"] = dns_m.group(1).strip().rstrip("/")
    if link_m: res["payment_link"] = link_m.group(1).strip()
    if tv_m: res["apps_smart_tv"] = tv_m.group(1).strip()
    if and_m: res["apps_android"] = and_m.group(1).strip()

    return res

# ==========================================================
# 3. ROTAS DE API DO SISTEMA
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
        "tmdb_api_key": cfg.get("tmdb_api_key", ""),
        "apps_smart_tv": cfg.get("apps_smart_tv", ""),
        "apps_android": cfg.get("apps_android", "")
    })

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Informe usuário e senha."}), 400

    cfg = get_settings()
    dns_list = [d.strip().rstrip("/") for d in cfg.get("dns_list", "").splitlines() if d.strip()]
    primary_dns = dns_list[0] if dns_list else "http://seu-servidor.com:80"

    conn = get_db()
    existing = conn.execute("SELECT * FROM active_users WHERE username = ?", (username,)).fetchone()
    now_ts = int(time.time())

    if existing and existing["expires_at"] and now_ts >= int(existing["expires_at"]):
        conn.close()
        return jsonify({
            "ok": False, "expired": True,
            "payment_link": existing["payment_link"] or cfg.get("default_payment_link", ""),
            "error": "Sua assinatura ou teste expirou! Redirecionando..."
        }), 403

    chosen_dns = primary_dns
    expires_at = now_ts + (30 * 86400)
    max_conns = "1"
    status_str = "Active"

    # Autenticação leve na Xtream Codes API do servidor
    for dns in dns_list:
        try:
            api_url = f"{dns}/player_api.php?username={urllib.parse.quote(username)}&password={urllib.parse.quote(password)}"
            req = urllib.request.Request(api_url, headers={"User-Agent": "StreamFlix/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                info = json.loads(resp.read().decode("utf-8", errors="ignore"))
                u_info = info.get("user_info", {})
                if str(u_info.get("auth")) == "1" or str(u_info.get("status")).lower() in ["active", "trial"]:
                    chosen_dns = dns
                    status_str = u_info.get("status", "Active")
                    max_conns = str(u_info.get("max_connections", "1"))
                    exp_date = u_info.get("exp_date")
                    if exp_date and str(exp_date).isdigit():
                        expires_at = int(exp_date)
                        if now_ts >= expires_at:
                            conn.close()
                            return jsonify({
                                "ok": False, "expired": True,
                                "payment_link": cfg.get("default_payment_link", ""),
                                "error": "Assinatura vencida! Redirecionando..."
                            }), 403
                    break
        except Exception:
            continue

    if not existing:
        conn.execute("""INSERT OR REPLACE INTO active_users
            (username, password, dns_used, plan_type, max_connections, expires_at, payment_link, is_trial)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (username, password, chosen_dns, "Assinatura IPTV", max_conns, expires_at, cfg.get("default_payment_link", "")))
        conn.commit()
    else:
        expires_at = existing["expires_at"] or expires_at
        chosen_dns = existing["dns_used"] or chosen_dns
        max_conns = existing["max_connections"] or max_conns
    conn.close()

    return jsonify({
        "ok": True,
        "username": username,
        "password": password,
        "dns": chosen_dns,
        "plan_type": "Assinante Oficial",
        "max_connections": max_conns,
        "status": status_str,
        "expires_at": expires_at,
        "expires_formatted": datetime.fromtimestamp(expires_at).strftime("%d/%m/%Y às %H:%M") if expires_at else "Sem Vencimento",
        "payment_link": cfg.get("default_payment_link", ""),
        "support_whatsapp": cfg.get("support_whatsapp", "")
    })

@app.route("/api/request-trial", methods=["POST"])
def api_request_trial():
    forwarded = request.headers.get("X-Forwarded-For", "")
    client_ip = (forwarded.split(",")[0].strip() if forwarded else request.remote_addr) or "0.0.0.0"
    data = request.get_json(silent=True) or {}
    device_mac = (data.get("device_mac") or "").strip().upper()

    if not device_mac:
        ua = request.headers.get("User-Agent", "unknown")
        h = hashlib.sha256(f"{client_ip}-{ua}".encode()).hexdigest()[:12].upper()
        device_mac = ":".join(h[i:i+2] for i in range(0, 12, 2))

    conn = get_db()
    dup = conn.execute("SELECT * FROM trial_records WHERE ip_address = ? OR device_mac = ?", (client_ip, device_mac)).fetchone()
    cfg = get_settings()

    if dup:
        conn.close()
        return jsonify({
            "ok": False, "already_used": True,
            "payment_link": dup["payment_link"] or cfg.get("default_payment_link", ""),
            "error": "Limite de Teste Grátis atingido para este aparelho/conexão."
        }), 403

    trial_api_url = (cfg.get("trial_api_url") or "").strip()
    raw_api_resp = ""

    if trial_api_url:
        try:
            req = urllib.request.Request(
                trial_api_url,
                headers={"User-Agent": "Mozilla/5.0 StreamFlixTrial/1.0"},
                method="POST" if "post" in trial_api_url.lower() else "GET"
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                raw_api_resp = resp.read().decode("utf-8", errors="ignore")
        except Exception:
            try:
                req2 = urllib.request.Request(
                    trial_api_url,
                    data=b"{}",
                    headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
                    method="POST"
                )
                with urllib.request.urlopen(req2, timeout=12) as resp2:
                    raw_api_resp = resp2.read().decode("utf-8", errors="ignore")
            except Exception:
                raw_api_resp = ""

    parsed = parse_trial_api_response(raw_api_resp, cfg)

    if not parsed["username"]:
        rand_suffix = hashlib.md5(f"{client_ip}{time.time()}".encode()).hexdigest()[:5]
        parsed["username"] = f"teste_{rand_suffix}"
        parsed["password"] = f"{int(time.time()) % 900000 + 100000}"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""INSERT INTO trial_records
        (ip_address, device_mac, username, password, plan_type, expires_at, payment_link, apps_smart_tv, apps_android, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_ip, device_mac, parsed["username"], parsed["password"], parsed["plan_type"],
         parsed["expires_at"], parsed["payment_link"], parsed["apps_smart_tv"], parsed["apps_android"], now_str))

    conn.execute("""INSERT OR REPLACE INTO active_users
        (username, password, dns_used, plan_type, max_connections, expires_at, payment_link, is_trial)
        VALUES (?, ?, ?, ?, '1', ?, ?, 1)""",
        (parsed["username"], parsed["password"], parsed["dns"], parsed["plan_type"], parsed["expires_at"], parsed["payment_link"]))
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "trial": {
            "username": parsed["username"],
            "password": parsed["password"],
            "dns": parsed["dns"],
            "plan_type": parsed["plan_type"],
            "expires_at": parsed["expires_at"],
            "expires_formatted": parsed["expires_formatted"],
            "payment_link": parsed["payment_link"],
            "apps_smart_tv": parsed["apps_smart_tv"],
            "apps_android": parsed["apps_android"],
            "support_whatsapp": cfg.get("support_whatsapp", ""),
            "recorded_ip": client_ip,
            "recorded_mac": device_mac
        }
    })

@app.route("/api/check-session", methods=["POST"])
def api_check_session():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"valid": False})

    conn = get_db()
    row = conn.execute("SELECT * FROM active_users WHERE username = ?", (username,)).fetchone()
    conn.close()
    cfg = get_settings()
    now_ts = int(time.time())

    if row:
        exp_ts = int(row["expires_at"] or 0)
        if exp_ts > 0 and now_ts >= exp_ts:
            return jsonify({
                "valid": False,
                "expired": True,
                "payment_link": row["payment_link"] or cfg.get("default_payment_link", "")
            })
        return jsonify({
            "valid": True, "expired": False, "expires_at": exp_ts,
            "remaining_seconds": max(0, exp_ts - now_ts) if exp_ts > 0 else 999999,
            "plan_type": row["plan_type"],
            "max_connections": row["max_connections"] or "1",
            "payment_link": row["payment_link"] or cfg.get("default_payment_link", "")
        })
    return jsonify({"valid": True, "expired": False, "remaining_seconds": 86400})

@app.route("/api/update-adult-pin", methods=["POST"])
def api_update_adult_pin():
    data = request.get_json(silent=True) or {}
    new_pin = str(data.get("new_pin") or "").strip()
    if len(new_pin) != 4 or not new_pin.isdigit():
        return jsonify({"ok": False, "error": "O PIN deve conter exatamente 4 dígitos."}), 400
    save_settings({"adult_pin": new_pin})
    return jsonify({"ok": True, "pin": new_pin})

# --- ROTAS ADMIN ---
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
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    cfg = get_settings()
    conn = get_db()
    trials = [dict(r) for r in conn.execute("SELECT * FROM trial_records ORDER BY id DESC LIMIT 100").fetchall()]
    conn.close()
    return jsonify({"ok": True, "settings": cfg, "trials": trials})

@app.route("/api/admin/save", methods=["POST"])
def api_admin_save():
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    save_settings(request.get_json(silent=True) or {})
    return jsonify({"ok": True})

@app.route("/api/admin/delete-trial/<int:trial_id>", methods=["DELETE"])
def api_admin_delete_trial(trial_id):
    if not session.get("is_admin"):
        return jsonify({"ok": False}), 401
    conn = get_db()
    conn.execute("DELETE FROM trial_records WHERE id = ?", (trial_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# ==========================================================
# 4. FRONT-END COMPLETO (SPA COM DESIGN NETFLIX & CONTROLES)
# ==========================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <meta name="referrer" content="no-referrer">
    <title>StreamFlix Pro v5.5</title>
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
            --primary-hover: #ff1f27;
            --gold: #f59e0b;
            --success: #10b981;
            --text-main: #ffffff;
            --text-muted: #94a3b8;
            --border: #1e1e2c;
            --radius-md: 10px;
            --radius-lg: 16px;
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

        /* TELA DE LOGIN */
        #screen-login { display:none; align-items:center; justify-content:center; padding:1.2rem; min-height:100vh; background:radial-gradient(circle at top, #240508 0%, #000 75%); }
        #screen-login.active { display:flex; }
        .login-box { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:2rem 1.6rem; width:100%; max-width:410px; box-shadow:0 25px 60px rgba(0,0,0,0.95); }
        .brand-title { font-size:1.65rem; font-weight:900; text-align:center; letter-spacing:0.8px; margin-bottom:0.2rem; }
        .brand-title span { color:var(--primary); }
        .brand-sub { text-align:center; color:var(--text-muted); font-size:0.8rem; margin-bottom:1.5rem; }
        .form-group { margin-bottom:1rem; }
        .form-group label { display:block; font-size:0.74rem; font-weight:800; color:var(--text-muted); margin-bottom:0.35rem; text-transform:uppercase; }
        .input-wrap { display:flex; align-items:center; background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius-md); padding:0 0.8rem; }
        .input-wrap input, .input-wrap textarea { width:100%; background:transparent; border:none; outline:none; padding:0.85rem 0.5rem; color:#fff; font-size:0.92rem; }
        .btn-primary { width:100%; padding:0.92rem; background:var(--primary); border:none; border-radius:var(--radius-md); color:#fff; font-size:0.95rem; font-weight:800; cursor:pointer; }
        .btn-trial { width:100%; padding:0.9rem; margin-top:0.75rem; background:linear-gradient(135deg, #10b981, #059669); border:none; border-radius:var(--radius-md); color:#fff; font-size:0.92rem; font-weight:800; cursor:pointer; }

        /* MODAL DO TESTE GRÁTIS GERADO DINAMICAMENTE */
        .modal-sheet { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.92); backdrop-filter:blur(10px); z-index:500; align-items:center; justify-content:center; padding:1rem; }
        .modal-sheet.active { display:flex; }
        .modal-box { background:var(--bg-surface); border:1px solid var(--success); border-radius:var(--radius-lg); width:100%; max-width:440px; padding:1.5rem; max-height:92vh; overflow-y:auto; }
        .trial-info-list { background:var(--bg-card); border:1px solid var(--border); border-radius:var(--radius-md); padding:0.9rem; margin:1rem 0; font-size:0.82rem; display:flex; flex-direction:column; gap:0.55rem; }
        .trial-row { display:flex; justify-content:space-between; gap:0.5rem; border-bottom:1px dashed #202434; padding-bottom:0.38rem; }
        .trial-row:last-child { border-bottom:none; padding-bottom:0; }
        .trial-row strong { color:var(--text-muted); font-size:0.72rem; text-transform:uppercase; }
        .trial-row span { color:#fff; font-weight:700; text-align:right; word-break:break-all; }
        .trial-btns { display:grid; grid-template-columns:1fr 1fr; gap:0.55rem; margin-bottom:0.75rem; }
        .btn-outline { background:var(--bg-card); border:1px solid var(--border); color:#fff; padding:0.75rem; border-radius:8px; font-size:0.8rem; font-weight:700; text-decoration:none; text-align:center; cursor:pointer; }

        /* TOPBAR & NAVEGAÇÃO */
        .navbar { height:58px; background:var(--bg-glass); backdrop-filter:blur(12px); border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; padding:0 1rem; position:sticky; top:0; z-index:120; }
        .nav-left { display:flex; align-items:center; gap:0.6rem; }
        .btn-nav-back { background:var(--bg-card); border:1px solid var(--border); color:#fff; padding:0.38rem 0.65rem; border-radius:8px; font-size:0.76rem; font-weight:700; cursor:pointer; display:inline-flex; align-items:center; gap:0.3rem; }
        .nav-brand { font-weight:900; font-size:1.18rem; letter-spacing:0.5px; }
        .nav-brand span { color:var(--primary); }

        /* HERO CAROUSEL 5 SEGUNDOS (EXCLUSIVO FILMES E SÉRIES - ZERO CANAIS E ZERO ADULTO) */
        .content-wrap { max-width:1180px; margin:0 auto; padding:0.9rem; }
        .hero-box { position:relative; border-radius:var(--radius-lg); overflow:hidden; height:280px; background:#07070c; border:1px solid var(--border); margin-bottom:1.4rem; }
        .hero-bg { position:absolute; inset:0; background-size:cover; background-position:center; transition:background-image 0.4s ease; }
        .hero-overlay { position:absolute; inset:0; background:linear-gradient(0deg, #000 14%, rgba(0,0,0,0.65) 60%, rgba(0,0,0,0.2) 100%); display:flex; flex-direction:column; justify-content:flex-end; padding:1.25rem; }
        .hero-tag { align-self:flex-start; background:var(--primary); color:#fff; font-size:0.65rem; font-weight:800; text-transform:uppercase; padding:0.2rem 0.55rem; border-radius:20px; margin-bottom:0.4rem; }
        .hero-title { font-size:1.35rem; font-weight:900; margin-bottom:0.2rem; }
        .hero-meta { font-size:0.78rem; color:#cbd5e1; margin-bottom:0.85rem; }
        .hero-timer { position:absolute; bottom:0; left:0; right:0; height:4px; background:rgba(255,255,255,0.15); }
        .hero-timer-fill { height:100%; width:0%; background:var(--primary); transition:width 0.1s linear; }

        /* FILEIRAS & PÔSTERES VERTICAIS */
        .home-row { margin-bottom:1.5rem; }
        .row-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:0.65rem; }
        .row-title { font-size:0.95rem; font-weight:800; display:flex; align-items:center; gap:0.4rem; }
        .vod-carousel { display:flex; gap:0.75rem; overflow-x:auto; padding-bottom:0.4rem; scrollbar-width:none; }
        .vod-carousel::-webkit-scrollbar { display:none; }

        .poster-card { position:relative; flex:0 0 134px; background:var(--bg-surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; cursor:pointer; }
        .poster-card:active { transform:scale(0.96); border-color:var(--primary); }
        .poster-aspect { position:relative; width:100%; aspect-ratio:2 / 3; background:#050509; overflow:hidden; }
        .poster-aspect img { width:100%; height:100%; object-fit:cover; display:block; }
        .poster-badge { position:absolute; top:6px; left:6px; background:rgba(229,9,20,0.92); color:#fff; font-size:0.56rem; font-weight:800; padding:2px 5px; border-radius:4px; text-transform:uppercase; }
        .poster-info { padding:0.5rem; }
        .poster-info h4 { font-size:0.76rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
        .poster-info span { font-size:0.66rem; color:var(--text-muted); display:block; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }

        /* MENU LATERAL OCULTO (DRAWER AUTOMÁTICO DE CATEGORIAS XTREAM) */
        .drawer-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.85); z-index:300; opacity:0; pointer-events:none; transition:opacity 0.25s; }
        .drawer-backdrop.active { opacity:1; pointer-events:auto; }
        .drawer-panel { position:fixed; top:0; left:0; bottom:0; width:290px; max-width:85vw; background:#07070c; border-right:1px solid var(--border); z-index:310; transform:translateX(-100%); transition:transform 0.28s ease; display:flex; flex-direction:column; }
        .drawer-panel.active { transform:translateX(0); }
        .drawer-header { height:58px; padding:0 1rem; border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; }
        .drawer-list { flex:1; overflow-y:auto; padding:0.6rem; display:flex; flex-direction:column; gap:0.35rem; }
        .drawer-item { padding:0.75rem 0.85rem; border-radius:8px; color:var(--text-muted); background:transparent; border:none; text-align:left; font-size:0.85rem; font-weight:700; cursor:pointer; display:flex; justify-content:space-between; align-items:center; }
        .drawer-item.active { background:var(--primary); color:#fff; }
        .drawer-item .badge-cnt { font-size:0.7rem; background:rgba(255,255,255,0.1); padding:2px 6px; border-radius:10px; }

        .posters-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(122px, 1fr)); gap:0.75rem; padding-bottom:1.5rem; }
        .posters-grid .poster-card { flex:unset; width:100%; }

        /* MODAL DE DETALHES TMDB / VOD INFO */
        .tmdb-modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.92); backdrop-filter:blur(10px); z-index:450; align-items:center; justify-content:center; padding:1rem; }
        .tmdb-modal.active { display:flex; }
        .tmdb-box { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); width:100%; max-width:520px; max-height:92vh; overflow-y:auto; position:relative; }
        .tmdb-hero { height:180px; background-size:cover; background-position:center; position:relative; }
        .tmdb-hero-overlay { position:absolute; inset:0; background:linear-gradient(0deg, var(--bg-surface) 10%, transparent 100%); }
        .tmdb-content { padding:1.2rem; }

        /* BOTTOM NAVIGATION BAR */
        .bottom-nav { position:fixed; bottom:0; left:0; right:0; height:66px; background:rgba(8,8,12,0.96); backdrop-filter:blur(16px); border-top:1px solid var(--border); display:none; align-items:center; justify-content:space-around; z-index:200; }
        .bottom-nav.active { display:flex; }
        .bnav-item { background:transparent; border:none; color:var(--text-muted); display:flex; flex-direction:column; align-items:center; justify-content:center; gap:0.2rem; font-size:0.68rem; font-weight:700; cursor:pointer; flex:1; height:100%; }
        .bnav-item.active { color:var(--primary); }

        /* PLAYER DE VÍDEO (PREENCHER HORIZONTALMENTE + CONTROLES COMPLETOS) */
        .player-modal { display:none; position:fixed; inset:0; background:#000; z-index:600; flex-direction:column; }
        .player-modal.active { display:flex; }
        .player-top { height:52px; background:rgba(10,10,15,0.9); border-bottom:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; padding:0 1rem; z-index:20; }
        .video-stage { position:relative; flex:1; width:100%; overflow:hidden; background:#000; display:flex; align-items:center; justify-content:center; }

        /* Preencher Horizontalmente Sem Zoom */
        #video-player {
            width: 100vw;
            height: auto;
            max-height: 100%;
            object-fit: fill;
            background: #000;
        }
        #video-player.mode-fit { object-fit: contain; width: 100%; height: 100%; }

        .player-controls-deck { padding:0.75rem 1rem; background:rgba(10,10,15,0.95); border-top:1px solid var(--border); display:flex; flex-direction:column; gap:0.6rem; z-index:20; }
        .deck-row { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.5rem; }
        .player-btn { background:var(--bg-card); border:1px solid var(--border); color:#fff; font-size:0.75rem; font-weight:800; padding:0.42rem 0.72rem; border-radius:6px; cursor:pointer; display:inline-flex; align-items:center; gap:0.35rem; }
        .player-btn.active { background:var(--primary); border-color:var(--primary); }
        .speed-btn { background:var(--bg-card); border:1px solid var(--border); color:var(--text-muted); font-size:0.7rem; font-weight:800; padding:0.28rem 0.55rem; border-radius:4px; cursor:pointer; }
        .speed-btn.active { background:var(--primary); color:#fff; border-color:var(--primary); }

        /* EPG BAR */
        .epg-bar { background:rgba(20,20,30,0.85); border-radius:6px; padding:0.45rem 0.75rem; font-size:0.75rem; display:none; justify-content:space-between; }
        .epg-bar.active { display:flex; }

        /* TELA CONFIGURAÇÕES */
        .config-card { background:var(--bg-surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:1.2rem; margin-bottom:1rem; }
        .config-title { font-size:0.92rem; font-weight:800; color:var(--primary); margin-bottom:0.75rem; display:flex; align-items:center; gap:0.4rem; }

        @media (min-width: 640px) {
            .posters-grid { grid-template-columns:repeat(auto-fill, minmax(155px, 1fr)); }
            .poster-card { flex:0 0 155px; }
        }
    </style>
</head>
<body>

    <!-- 1. LOGIN -->
    <div id="screen-login" class="screen active">
        <div class="login-box">
            <h1 class="brand-title">STREAM<span>FLIX</span></h1>
            <p class="brand-sub">Cinema, Séries & TV ao Vivo</p>
            <form id="form-login">
                <div class="form-group"><label>Usuário</label><div class="input-wrap"><input type="text" id="l-user" required></div></div>
                <div class="form-group"><label>Senha</label><div class="input-wrap"><input type="password" id="l-pass" required></div></div>
                <button type="submit" class="btn-primary">Entrar e Assistir</button>
            </form>
            <button type="button" class="btn-trial" id="btn-trial">⚡ Gerar Teste Grátis Automático</button>
            <div style="display:flex; justify-content:space-between; margin-top:1.2rem; font-size:0.74rem; color:var(--text-muted);">
                <span id="btn-open-m3u" style="cursor:pointer; text-decoration:underline;">Importar M3U</span>
                <span id="btn-go-admin" style="cursor:pointer;">Área Admin</span>
            </div>
        </div>
    </div>

    <!-- 2. CARD DO TESTE GRÁTIS GERADO DINAMICAMENTE -->
    <div id="trial-modal" class="modal-sheet">
        <div class="modal-box">
            <h3 style="color:var(--success); font-size:1.15rem; font-weight:900; margin-bottom:0.25rem;">✅ Teste Grátis Liberado!</h3>
            <p style="font-size:0.76rem; color:var(--text-muted);">Dados gerados pelo servidor IPTV:</p>
            <div class="trial-info-list">
                <div class="trial-row"><strong>Usuário</strong><span id="tr-u">-</span></div>
                <div class="trial-row"><strong>Senha</strong><span id="tr-p">-</span></div>
                <div class="trial-row"><strong>Tipo de Plano</strong><span id="tr-plan">-</span></div>
                <div class="trial-row"><strong>Vencimento</strong><span id="tr-exp">-</span></div>
                <div class="trial-row"><strong>Apps Smart TV</strong><span id="tr-tv">-</span></div>
                <div class="trial-row"><strong>Apps Android</strong><span id="tr-and">-</span></div>
                <div class="trial-row"><strong>Link de Pagamento</strong><span id="tr-pay" style="color:var(--gold);">-</span></div>
            </div>
            <div class="trial-btns">
                <button type="button" class="btn-outline" id="btn-copy-tr">📋 Copiar Dados</button>
                <a href="#" target="_blank" class="btn-outline" id="btn-wa-tr" style="background:rgba(37,211,102,0.18); border-color:#25d366; color:#4ade80;">📲 WhatsApp</a>
            </div>
            <button type="button" class="btn-primary" id="btn-proceed-tr" style="background:var(--success);">▶ Prosseguir e Assistir Agora</button>
        </div>
    </div>

    <!-- 3. TOPBAR GERAL -->
    <header class="navbar hidden" id="main-navbar">
        <div class="nav-left">
            <button class="btn-nav-back hidden" id="btn-global-back">‹ Voltar</button>
            <div class="nav-brand">STREAM<span>FLIX</span></div>
        </div>
        <div style="display:flex; gap:0.4rem; align-items:center;">
            <button class="btn-nav-back" id="btn-open-drawer">📂 Categorias</button>
        </div>
    </header>

    <!-- DRAWER LATERAL OCULTO DE CATEGORIAS XTREAM -->
    <div class="drawer-backdrop" id="drawer-backdrop"></div>
    <aside class="drawer-panel" id="drawer-panel">
        <div class="drawer-header">
            <strong style="font-size:0.9rem;" id="drawer-title">Categorias</strong>
            <button class="btn-nav-back" id="btn-close-drawer">✕ Fechar</button>
        </div>
        <div class="drawer-list" id="drawer-list"></div>
    </aside>

    <!-- 4. TELAS PRINCIPAIS -->
    <main class="content-wrap hidden" id="main-content">
        <!-- TELA HOME -->
        <div id="view-home">
            <!-- Hero Carousel 5s exclusivo Filmes/Séries -->
            <div class="hero-box">
                <div class="hero-bg" id="hero-bg"></div>
                <div class="hero-overlay">
                    <span class="hero-tag">RECÉM ADICIONADO (5s)</span>
                    <h2 class="hero-title" id="hero-title">Carregando Destaques...</h2>
                    <p class="hero-meta" id="hero-meta">Prévia automática exclusiva de Filmes e Séries</p>
                    <button class="btn-primary" id="btn-hero-play" style="width:auto; padding:0.6rem 1.2rem;">▶ Assistir Agora</button>
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

            <section class="home-row">
                <div class="row-header"><div class="row-title">📺 TV ao Vivo em Destaque</div></div>
                <div class="vod-carousel" id="carousel-home-tv"></div>
            </section>
        </div>

        <!-- TELA CATÁLOGO (FILMES / SÉRIES / TV) -->
        <div id="view-catalog" class="hidden">
            <div class="input-wrap" style="margin-bottom:0.75rem;">
                <input type="text" id="catalog-search" placeholder="🔍 Pesquisar por nome ou canal...">
            </div>
            <div style="display:flex; justify-content:space-between; font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">
                <span id="cat-selected-label">Categoria: Todas</span>
                <span id="cat-total-label">0 itens</span>
            </div>
            <div class="posters-grid" id="posters-grid"></div>
        </div>

        <!-- TELA CONFIGURAÇÕES -->
        <div id="view-config" class="hidden">
            <div class="config-card">
                <div class="config-title">👤 Dados do Acesso do Servidor</div>
                <div class="trial-row"><strong>Usuário:</strong><span id="cfg-user">-</span></div>
                <div class="trial-row"><strong>Senha:</strong><span id="cfg-pass">-</span></div>
                <div class="trial-row"><strong>Vencimento:</strong><span id="cfg-exp">-</span></div>
                <div class="trial-row"><strong>Tipo de Plano:</strong><span id="cfg-plan">-</span></div>
                <div class="trial-row"><strong>Telas Simultâneas:</strong><span id="cfg-screens">1 tela</span></div>
            </div>

            <div class="config-card">
                <div class="config-title">🔒 Controle Parental (+18 / Adultos)</div>
                <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.5rem;">Conteúdos adultos ficam ocultos das prévias e da lista geral. Senha padrão: <strong>0000</strong>.</p>
                <div class="input-wrap" style="margin-bottom:0.5rem;"><input type="password" id="cfg-adult-pin" maxlength="4" placeholder="Novo PIN (4 dígitos)"></div>
                <button class="btn-primary" id="btn-save-pin" style="padding:0.65rem;">Alterar Senha Adultos</button>
            </div>

            <div class="config-card">
                <div class="config-title">🎨 Aparência e Armazenamento</div>
                <div style="display:flex; gap:0.5rem; margin-bottom:0.75rem;">
                    <button class="btn-outline" id="btn-theme-oled" style="flex:1;">OLED Pure Black</button>
                    <button class="btn-outline" id="btn-theme-midnight" style="flex:1;">Midnight Blue</button>
                </div>
                <button class="btn-outline" id="btn-clear-cache" style="width:100%; color:#fca5a5;">🗑️ Limpar Cache e Recarregar</button>
            </div>

            <div class="config-card">
                <div class="config-title">💬 Suporte e Atendimento</div>
                <p style="font-size:0.75rem; color:var(--text-muted); margin-bottom:0.6rem;">Fale diretamente conosco pelo WhatsApp:</p>
                <a href="#" target="_blank" class="btn-primary" id="btn-support-wa" style="text-decoration:none; text-align:center; background:#25d366; display:block;">📲 Chamar no WhatsApp</a>
            </div>

            <button class="btn-outline" id="btn-logout-cfg" style="width:100%; border-color:#e50914; color:#fca5a5; padding:0.85rem;">Sair da Conta</button>
        </div>
    </main>

    <!-- MODAL DETALHES TMDB / VOD -->
    <div id="tmdb-modal" class="tmdb-modal">
        <div class="tmdb-box">
            <div class="tmdb-hero" id="tmdb-hero"><div class="tmdb-hero-overlay"></div></div>
            <div class="tmdb-content">
                <h3 id="tmdb-title" style="font-size:1.25rem; font-weight:900; margin-bottom:0.25rem;">Título</h3>
                <p id="tmdb-meta" style="font-size:0.78rem; color:var(--gold); margin-bottom:0.7rem;">Ano • Avaliação TMDB</p>
                <p id="tmdb-plot" style="font-size:0.82rem; color:#cbd5e1; line-height:1.45; margin-bottom:1rem;">Sinopse...</p>
                <div style="display:flex; gap:0.55rem;">
                    <button class="btn-primary" id="btn-tmdb-play" style="flex:2;">▶ Assistir Título</button>
                    <button class="btn-outline" id="btn-tmdb-close" style="flex:1;">Fechar</button>
                </div>
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

    <!-- 6. PLAYER DE VÍDEO COMPLETO -->
    <div id="player-modal" class="player-modal">
        <div class="player-top">
            <button class="btn-nav-back" id="btn-player-back">‹ Voltar</button>
            <strong id="player-title" style="font-size:0.85rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">Reproduzindo</strong>
            <span id="player-aspect-indicator" style="font-size:0.7rem; color:var(--gold);">100% Horizontal</span>
        </div>

        <div class="video-stage">
            <video id="video-player" playsinline preload="auto"></video>
        </div>

        <div class="player-controls-deck">
            <div class="epg-bar" id="epg-bar">
                <span id="epg-now">Agora: Carregando Guia EPG...</span>
                <span id="epg-time" style="color:var(--text-muted);">--:--</span>
            </div>

            <!-- Botões Principais: Play/Pause, -10s, +10s, Preenchimento Horizontal -->
            <div class="deck-row">
                <div style="display:flex; gap:0.4rem; align-items:center;">
                    <button class="player-btn" id="btn-rw">⏪ -10s</button>
                    <button class="player-btn active" id="btn-play-pause">⏸ Pausar</button>
                    <button class="player-btn" id="btn-ff">⏩ +10s</button>
                </div>

                <div style="display:flex; gap:0.35rem; align-items:center;">
                    <button class="player-btn active" id="btn-aspect-fill">Preencher Horizontalmente</button>
                    <button class="player-btn" id="btn-aspect-fit">Original</button>
                </div>
            </div>

            <!-- Seletor de Velocidades: 1.0, 1.3, 1.5, 1.8, 2.0 -->
            <div class="deck-row">
                <span style="font-size:0.7rem; color:var(--text-muted);">Velocidade:</span>
                <div style="display:flex; gap:0.3rem;">
                    <button class="speed-btn active" data-speed="1.0">1.0x</button>
                    <button class="speed-btn" data-speed="1.3">1.3x</button>
                    <button class="speed-btn" data-speed="1.5">1.5x</button>
                    <button class="speed-btn" data-speed="1.8">1.8x</button>
                    <button class="speed-btn" data-speed="2.0">2.0x</button>
                </div>
            </div>
        </div>
    </div>

    <!-- 7. TELA ADMIN -->
    <div id="screen-admin" class="screen">
        <div style="max-width:850px; margin:1.5rem auto; padding:1rem;">
            <div style="display:flex; justify-content:space-between; margin-bottom:1rem;">
                <h2>⚙️ Painel Administrador</h2>
                <button class="btn-nav-back" id="btn-exit-admin">Voltar ao Player</button>
            </div>
            <div id="adm-login-box" class="config-card">
                <div class="form-group"><label>Senha Admin</label><div class="input-wrap"><input type="password" id="adm-pass" placeholder="admin123"></div></div>
                <button class="btn-primary" id="btn-adm-login">Acessar Painel</button>
            </div>
            <div id="adm-content" class="hidden">
                <div class="config-card">
                    <div class="form-group"><label>DNS Servidor IPTV (Xtream)</label><div class="input-wrap"><textarea id="adm-dns" rows="2"></textarea></div></div>
                    <div class="form-group"><label>API de Teste Automático</label><div class="input-wrap"><input type="text" id="adm-trial-api"></div></div>
                    <div class="form-group"><label>Link de Pagamento / Renovação</label><div class="input-wrap"><input type="url" id="adm-pay-link"></div></div>
                    <div class="form-group"><label>WhatsApp de Suporte (Link Completo)</label><div class="input-wrap"><input type="text" id="adm-wa-link" placeholder="https://wa.me/55..."></div></div>
                    <div class="form-group"><label>Chave API TMDB (Opcional)</label><div class="input-wrap"><input type="text" id="adm-tmdb-key" placeholder="Sua Key TMDB"></div></div>
                    <button class="btn-primary" id="btn-adm-save">Salvar Alterações</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        lucide.createIcons();

        // ESTADO GLOBAL
        let activeTab = "home";
        let activeCategory = "Todas";
        let currentAuth = JSON.parse(localStorage.getItem("sf_session") || "null");
        let serverConfig = {};
        let xtreamCategories = { tv: [], movies: [], series: [] };
        let catalog = { tv: [], movies: [], series: [] };
        let currentPlayingItem = null;
        let hlsInstance = null;
        let mpegtsInstance = null;
        let adultPin = "0000";

        const ADULT_KEYWORDS = ["adult", "+18", "xxx", "porn", "erotic", "sexy", "playboy", "venus", "for adults", "hot"];

        function isAdultItem(item) {
            const txt = ((item.name || "") + " " + (item.group || "")).toLowerCase();
            return ADULT_KEYWORDS.some(kw => txt.includes(kw));
        }

        // CARREGA CONFIGURAÇÕES DA API
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

        // HERO CAROUSEL 5s (EXCLUSIVO FILMES E SÉRIES - ZERO CANAIS E ZERO ADULTO)
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
            document.getElementById("hero-meta").textContent = `${it.group} • 5s Destaque`;
            document.getElementById("btn-hero-play").onclick = () => openItemPlay(it);
        }

        // CARREGA DADOS XTREAM CODES DIRETO DO SERVIDOR
        async function fetchXtreamData() {
            if (!currentAuth || !currentAuth.dns) return;
            const dns = currentAuth.dns.replace(/\/$/, "");
            const u = encodeURIComponent(currentAuth.username);
            const p = encodeURIComponent(currentAuth.password);

            // 1. Categorias
            try {
                const [rTvCat, rVodCat, rSerCat] = await Promise.all([
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_live_categories`),
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_vod_categories`),
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_series_categories`)
                ]);
                xtreamCategories.tv = await rTvCat.json();
                xtreamCategories.movies = await rVodCat.json();
                xtreamCategories.series = await rSerCat.json();
            } catch (e) {}

            // 2. Streams (Filmes, Séries, TV)
            try {
                const [rVod, rSer, rLive] = await Promise.all([
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_vod_streams`),
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_series`),
                    fetch(`${dns}/player_api.php?username=${u}&password=${p}&action=get_live_streams`)
                ]);
                const vods = await rVod.json();
                const series = await rSer.json();
                const live = await rLive.json();

                catalog.movies = (Array.isArray(vods) ? vods : []).map(v => ({
                    id: v.stream_id, name: v.name, group: v.category_name || "Filmes", category_id: v.category_id,
                    type: "movies", logo: v.stream_icon, container_extension: v.container_extension || "mp4",
                    rating: v.rating, tmdb_id: v.tmdb_id,
                    url: `${dns}/movie/${currentAuth.username}/${currentAuth.password}/${v.stream_id}.${v.container_extension || 'mp4'}`
                }));

                catalog.series = (Array.isArray(series) ? series : []).map(s => ({
                    id: s.series_id, name: s.name, group: s.category_name || "Séries", category_id: s.category_id,
                    type: "series", logo: s.cover, rating: s.rating, tmdb_id: s.tmdb_id,
                    url: `${dns}/series/${currentAuth.username}/${currentAuth.password}/${s.series_id}.mp4`
                }));

                catalog.tv = (Array.isArray(live) ? live : []).map(l => ({
                    id: l.stream_id, name: l.name, group: l.category_name || "Canais", category_id: l.category_id,
                    type: "tv", logo: l.stream_icon, epg_id: l.epg_channel_id,
                    url: `${dns}/live/${currentAuth.username}/${currentAuth.password}/${l.stream_id}.ts`
                }));

                // Prepara itens de prévia (Apenas Filmes e Séries - ZERO Adulto e ZERO Canais)
                heroItems = [...catalog.movies, ...catalog.series]
                    .filter(it => !isAdultItem(it))
                    .slice(0, 15);

                if (heroItems.length > 0) renderHeroSlide(heroItems[0]);
                renderHomeCarousels();
            } catch (e) {}
        }

        // RENDERIZA HOME
        function renderHomeCarousels() {
            const fillC = (id, list) => {
                const el = document.getElementById(id);
                el.innerHTML = "";
                list.filter(it => !isAdultItem(it)).slice(0, 16).forEach(it => el.appendChild(createPosterCard(it)));
            };
            fillC("carousel-home-movies", catalog.movies);
            fillC("carousel-home-series", catalog.series);
            fillC("carousel-home-tv", catalog.tv);
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
                if (it.type === "movies" || it.type === "series") openTmdbDetails(it);
                else openItemPlay(it);
            };
            return card;
        }

        // DETALHES TMDB DO TÍTULO
        async function openTmdbDetails(it) {
            document.getElementById("tmdb-modal").classList.add("active");
            document.getElementById("tmdb-title").textContent = it.name;
            document.getElementById("tmdb-meta").textContent = `Carregando avaliação e sinopse...`;
            document.getElementById("tmdb-plot").textContent = `Buscando informações completas do título no servidor...`;
            document.getElementById("tmdb-hero").style.backgroundImage = `url('${it.logo}')`;

            document.getElementById("btn-tmdb-play").onclick = () => {
                document.getElementById("tmdb-modal").classList.remove("active");
                openItemPlay(it);
            };

            // Consulta vod_info no Xtream
            if (currentAuth && it.type === "movies") {
                try {
                    const dns = currentAuth.dns.replace(/\/$/, "");
                    const r = await fetch(`${dns}/player_api.php?username=${currentAuth.username}&password=${currentAuth.password}&action=get_vod_info&vod_id=${it.id}`);
                    const d = await r.json();
                    const info = d.info || {};
                    const plot = info.plot || info.description || "Sinopse não informada pelo servidor.";
                    const rating = info.rating ? `⭐ TMDB: ${info.rating}/10` : "⭐ TMDB Disponível";
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

        // DRAWER LATERAL AUTOMÁTICO DE CATEGORIAS
        function openCategoriesDrawer() {
            const list = document.getElementById("drawer-list");
            list.innerHTML = "";

            const cats = activeTab === "tv" ? xtreamCategories.tv : (activeTab === "movies" ? xtreamCategories.movies : xtreamCategories.series);
            document.getElementById("drawer-title").textContent = `Categorias (${activeTab.toUpperCase()})`;

            // Botão "Todas"
            const bAll = document.createElement("button");
            bAll.className = `drawer-item ${activeCategory === "Todas" ? "active" : ""}`;
            bAll.innerHTML = `<span>Todas as Categorias</span>`;
            bAll.onclick = () => { selectCategory("Todas"); closeDrawer(); };
            list.appendChild(bAll);

            cats.forEach(c => {
                const b = document.createElement("button");
                b.className = `drawer-item ${activeCategory === c.category_name ? "active" : ""}`;
                b.innerHTML = `<span>${c.category_name}</span>`;
                b.onclick = () => {
                    const isAdult = ADULT_KEYWORDS.some(k => c.category_name.toLowerCase().includes(k));
                    if (isAdult) {
                        const entered = prompt("🔒 Digite o PIN de 4 dígitos para conteúdo Adulto:");
                        if (entered !== adultPin) return alert("PIN incorreto!");
                    }
                    selectCategory(c.category_name);
                    closeDrawer();
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
        document.getElementById("btn-open-drawer").onclick = openCategoriesDrawer;
        document.getElementById("btn-close-drawer").onclick = closeDrawer;
        document.getElementById("drawer-backdrop").onclick = closeDrawer;

        function selectCategory(catName) {
            activeCategory = catName;
            document.getElementById("cat-selected-label").textContent = `Categoria: ${catName}`;
            renderCatalogGrid();
        }

        function renderCatalogGrid() {
            const rawList = catalog[activeTab] || [];
            const search = document.getElementById("catalog-search").value.toLowerCase().trim();

            const filtered = rawList.filter(it => {
                const adult = isAdultItem(it);
                if (activeCategory === "Todas") {
                    if (adult) return false; // Bloqueia adulto da categoria "Todas"
                } else if (it.group !== activeCategory) {
                    return false;
                }
                if (search && !it.name.toLowerCase().includes(search)) return false;
                return true;
            });

            document.getElementById("cat-total-label").textContent = `${filtered.length} títulos`;
            const grid = document.getElementById("posters-grid");
            grid.innerHTML = "";
            filtered.slice(0, 120).forEach(it => grid.appendChild(createPosterCard(it)));
        }

        document.getElementById("catalog-search").oninput = renderCatalogGrid;

        // PLAYER DE VÍDEO COMPLETO (CONTROLES, 5 VELOCIDADES, EPG E HORIZONTAL)
        const vEl = document.getElementById("video-player");

        function openItemPlay(it) {
            currentPlayingItem = it;
            document.getElementById("player-modal").classList.add("active");
            document.getElementById("player-title").textContent = it.name;

            stopPlayerEngines();
            vEl.className = ""; // Preenchimento horizontal padrão

            // Carrega EPG se for canal de TV
            const epgBar = document.getElementById("epg-bar");
            if (it.type === "tv") {
                epgBar.classList.add("active");
                fetchEpg(it);
            } else {
                epgBar.classList.remove("active");
            }

            const url = it.url;
            if (url.includes(".m3u8") && typeof Hls !== "undefined" && Hls.isSupported()) {
                hlsInstance = new Hls();
                hlsInstance.loadSource(url);
                hlsInstance.attachMedia(vEl);
                hlsInstance.on(Hls.Events.MANIFEST_PARSED, () => vEl.play().catch(()=>{}));
            } else if (url.includes(".ts") && typeof mpegts !== "undefined" && mpegts.isSupported()) {
                mpegtsInstance = mpegts.createPlayer({ type: "mpegts", isLive: true, url: url });
                mpegtsInstance.attachMediaElement(vEl);
                mpegtsInstance.load();
                vEl.play().catch(()=>{});
            } else {
                vEl.src = url;
                vEl.play().catch(()=>{});
            }
        }

        async function fetchEpg(it) {
            if (!currentAuth || !it.id) return;
            try {
                const dns = currentAuth.dns.replace(/\/$/, "");
                const r = await fetch(`${dns}/player_api.php?username=${currentAuth.username}&password=${currentAuth.password}&action=get_short_epg&stream_id=${it.id}`);
                const d = await r.json();
                const prog = (d.epg_listings && d.epg_listings[0]) || {};
                document.getElementById("epg-now").textContent = `Agora: ${prog.title || it.name}`;
                document.getElementById("epg-time").textContent = prog.start ? `${prog.start} - ${prog.end}` : "Ao Vivo";
            } catch (e) {
                document.getElementById("epg-now").textContent = `Agora: ${it.name}`;
            }
        }

        function stopPlayerEngines() {
            if (hlsInstance) { try { hlsInstance.destroy(); } catch (e) {} hlsInstance = null; }
            if (mpegtsInstance) { try { mpegtsInstance.destroy(); } catch (e) {} mpegtsInstance = null; }
            vEl.pause();
            vEl.removeAttribute("src");
            vEl.load();
        }

        // CONTROLES DO PLAYER
        document.getElementById("btn-player-back").onclick = () => {
            stopPlayerEngines();
            document.getElementById("player-modal").classList.remove("active");
        };

        const btnPlay = document.getElementById("btn-play-pause");
        btnPlay.onclick = () => {
            if (vEl.paused) { vEl.play(); btnPlay.textContent = "⏸ Pausar"; }
            else { vEl.pause(); btnPlay.textContent = "▶ Play"; }
        };

        document.getElementById("btn-rw").onclick = () => { vEl.currentTime = Math.max(0, vEl.currentTime - 10); };
        document.getElementById("btn-ff").onclick = () => { vEl.currentTime += 10; };

        // Botões de Formato de Tela
        document.getElementById("btn-aspect-fill").onclick = () => {
            vEl.className = "";
            document.getElementById("player-aspect-indicator").textContent = "100% Horizontal";
            document.getElementById("btn-aspect-fill").classList.add("active");
            document.getElementById("btn-aspect-fit").classList.remove("active");
        };
        document.getElementById("btn-aspect-fit").onclick = () => {
            vEl.className = "mode-fit";
            document.getElementById("player-aspect-indicator").textContent = "Original 16:9";
            document.getElementById("btn-aspect-fit").classList.add("active");
            document.getElementById("btn-aspect-fill").classList.remove("active");
        };

        // Velocidades
        document.querySelectorAll(".speed-btn").forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll(".speed-btn").forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                vEl.playbackRate = parseFloat(btn.dataset.speed);
            };
        });

        // NAVEGAÇÃO BOTTOM BAR
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
                    document.getElementById("view-config").classList.add("hidden");
                } else if (activeTab === "config") {
                    backBtn.classList.remove("hidden");
                    document.getElementById("btn-open-drawer").classList.add("hidden");
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.add("hidden");
                    document.getElementById("view-config").classList.remove("hidden");
                    fillConfigScreen();
                } else {
                    backBtn.classList.remove("hidden");
                    document.getElementById("btn-open-drawer").classList.remove("hidden");
                    document.getElementById("view-home").classList.add("hidden");
                    document.getElementById("view-catalog").classList.remove("hidden");
                    document.getElementById("view-config").classList.add("hidden");
                    activeCategory = "Todas";
                    renderCatalogGrid();
                    openCategoriesDrawer(); // Abre automaticamente o menu lateral com todas as categorias do Xtream!
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
            document.getElementById("cfg-plan").textContent = currentAuth.plan_type || "Plano IPTV";
            document.getElementById("cfg-screens").textContent = `${currentAuth.max_connections || '1'} tela(s)`;
        }

        // ALTERAÇÃO DO PIN ADULTO
        document.getElementById("btn-save-pin").onclick = async () => {
            const p = document.getElementById("cfg-adult-pin").value.trim();
            const r = await fetch("/api/update-adult-pin", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ new_pin: p }) });
            const d = await r.json();
            if (d.ok) { adultPin = p; alert("✅ Senha Adulto alterada com sucesso!"); }
            else alert(d.error || "Erro ao salvar.");
        };

        // TEMAS E CACHE
        document.getElementById("btn-theme-oled").onclick = () => document.body.className = "";
        document.getElementById("btn-theme-midnight").onclick = () => document.body.className = "theme-midnight";
        document.getElementById("btn-clear-cache").onclick = () => { localStorage.clear(); location.reload(); };

        // LOGIN E TESTE GRÁTIS
        document.getElementById("form-login").onsubmit = async (e) => {
            e.preventDefault();
            const u = document.getElementById("l-user").value.trim();
            const p = document.getElementById("l-pass").value.trim();
            const r = await fetch("/api/login", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ username: u, password: p }) });
            const d = await r.json();
            if (!d.ok) return alert(d.error || "Erro ao entrar.");
            currentAuth = d;
            localStorage.setItem("sf_session", JSON.stringify(d));
            enterApp();
        };

        document.getElementById("btn-trial").onclick = async () => {
            const btn = document.getElementById("btn-trial");
            btn.textContent = "⏳ Gerando seu teste no servidor...";
            try {
                const r = await fetch("/api/request-trial", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({}) });
                const d = await r.json();
                if (!d.ok) {
                    btn.textContent = "⚡ Gerar Teste Grátis Automático";
                    return alert(d.error || "Erro ao solicitar teste.");
                }
                const tr = d.trial;
                currentAuth = tr;
                localStorage.setItem("sf_session", JSON.stringify(tr));

                // Popula modal com dados DINÂMICOS do servidor
                document.getElementById("tr-u").textContent = tr.username;
                document.getElementById("tr-p").textContent = tr.password;
                document.getElementById("tr-plan").textContent = tr.plan_type;
                document.getElementById("tr-exp").textContent = tr.expires_formatted;
                document.getElementById("tr-tv").textContent = tr.apps_smart_tv;
                document.getElementById("tr-and").textContent = tr.apps_android;
                document.getElementById("tr-pay").textContent = tr.payment_link;

                const msg = `🎬 *MEU TESTE GRÁTIS*\n👤 *Usuário:* ${tr.username}\n🔑 *Senha:* ${tr.password}\n📦 *Plano:* ${tr.plan_type}\n⏰ *Vencimento:* ${tr.expires_formatted}\n📺 *Smart TV:* ${tr.apps_smart_tv}\n📱 *Android:* ${tr.apps_android}\n💳 *Pagar/Assinar:* ${tr.payment_link}`;
                document.getElementById("btn-copy-tr").onclick = () => { navigator.clipboard.writeText(msg); alert("Dados copiados!"); };
                document.getElementById("btn-wa-tr").href = `https://wa.me/?text=${encodeURIComponent(msg)}`;

                document.getElementById("trial-modal").classList.add("active");
            } catch (e) {
                alert("Erro de conexão com o servidor.");
            }
            btn.textContent = "⚡ Gerar Teste Grátis Automático";
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
            fetchXtreamData();
        }

        document.getElementById("btn-logout-cfg").onclick = () => {
            localStorage.removeItem("sf_session");
            location.reload();
        };

        // ADMIN
        document.getElementById("btn-go-admin").onclick = () => {
            document.getElementById("screen-login").classList.remove("active");
            document.getElementById("screen-admin").classList.add("active");
        };
        document.getElementById("btn-exit-admin").onclick = () => {
            document.getElementById("screen-admin").classList.remove("active");
            if (currentAuth) enterApp(); else document.getElementById("screen-login").classList.add("active");
        };
        document.getElementById("btn-adm-login").onclick = async () => {
            const p = document.getElementById("adm-pass").value;
            const r = await fetch("/api/admin/login", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ username: "admin", password: p }) });
            if (r.ok) {
                document.getElementById("adm-login-box").classList.add("hidden");
                document.getElementById("adm-content").classList.remove("hidden");
                const res = await fetch("/api/admin/data");
                const d = await res.json();
                document.getElementById("adm-dns").value = d.settings.dns_list || "";
                document.getElementById("adm-trial-api").value = d.settings.trial_api_url || "";
                document.getElementById("adm-pay-link").value = d.settings.default_payment_link || "";
                document.getElementById("adm-wa-link").value = d.settings.support_whatsapp || "";
                document.getElementById("adm-tmdb-key").value = d.settings.tmdb_api_key || "";
            } else alert("Senha incorreta");
        };
        document.getElementById("btn-adm-save").onclick = async () => {
            await fetch("/api/admin/save", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
                dns_list: document.getElementById("adm-dns").value,
                trial_api_url: document.getElementById("adm-trial-api").value,
                default_payment_link: document.getElementById("adm-pay-link").value,
                support_whatsapp: document.getElementById("adm-wa-link").value,
                tmdb_api_key: document.getElementById("adm-tmdb-key").value
            })});
            alert("✅ Configurações salvas!");
        };

        // AUTO-LOGIN
        if (currentAuth && currentAuth.username) enterApp();
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
