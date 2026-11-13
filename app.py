import os
import re
import json
import sqlite3
import requests
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for, Response

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "streamflix-singlefile-secret-2026")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.db")

DEFAULT_SETTINGS = {
    "dns": "http://trevcdn.online",
    "trial_api": "https://infinity.panelbr.site/api/chatbot/lKWOwGbWzo/boQ1Ye1ONY",
    "default_pay_url": "https://infinity.panelbr.site/#/checkout/VrW8rBwLKa/8K1x0zlQ1v",
    "tmdb_key": "62ed6d70a1b5b49b0e4c806c976db35b",
    "admin_pass": "admin123"
}

# ==============================================================================
# BANCO DE DADOS SQLITE
# ==============================================================================
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users_pay (
            username TEXT PRIMARY KEY,
            password TEXT,
            pay_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for k, v in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def get_settings():
    init_db()
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    data = dict(DEFAULT_SETTINGS)
    for r in rows:
        data[r["key"]] = r["value"]
    return data

def clean_dns(dns_url):
    d = (dns_url or "").strip().rstrip("/")
    if not d:
        return ""
    if not d.startswith("http://") and not d.startswith("https://"):
        d = "http://" + d
    d = re.sub(r":80$", "", d)
    return d

def parse_trial_response(raw_text):
    username = ""
    password = ""
    pay_url = ""
    dns = ""

    try:
        data = json.loads(raw_text)
        def scan(obj):
            nonlocal username, password, pay_url, dns
            if isinstance(obj, dict):
                for k, v in obj.items():
                    kl = k.lower()
                    if isinstance(v, (str, int)):
                        val = str(v).strip()
                        if not username and kl in ("username", "user", "login", "usuario") and " " not in val:
                            username = val
                        elif not password and kl in ("password", "pass", "senha") and " " not in val:
                            password = val
                        elif not pay_url and any(x in kl for x in ("pay", "checkout", "fatura")) and val.startswith("http"):
                            pay_url = val
                        elif not dns and kl in ("dns", "server", "url", "host", "servidor") and val.startswith("http") and "panelbr.site/api" not in val:
                            dns = val
                    elif isinstance(v, (dict, list)):
                        scan(v)
            elif isinstance(obj, list):
                for item in obj:
                    scan(item)
        scan(data)
    except Exception:
        pass

    flat = raw_text.replace("\\n", "\n").replace("\\/", "/")

    m3u_match = re.search(r"(https?://[^\s\"'<>]+/get\.php\?username=([a-zA-Z0-9._-]+)&password=([a-zA-Z0-9._-]+))", flat, re.I)
    if m3u_match:
        if not username:
            username = m3u_match.group(2)
        if not password:
            password = m3u_match.group(3)

    if not username:
        u_m = re.search(r"(?:usu[aá]rio|user|login)\s*[*_:-]*\s*([a-zA-Z0-9._-]{3,30})", flat, re.I)
        if u_m:
            username = u_m.group(1)

    if not password:
        p_m = re.search(r"(?:senha|password|pass)\s*[*_:-]*\s*([a-zA-Z0-9._-]{3,30})", flat, re.I)
        if p_m:
            password = p_m.group(1)

    if not pay_url:
        pay_m = re.search(r"https?://[^\s\"'<>]+(?:checkout|pay|pagamento|fatura)[^\s\"'<>]*", flat, re.I)
        if pay_m:
            pay_url = pay_m.group(0)

    return {
        "username": username,
        "password": password,
        "pay_url": pay_url,
        "dns": clean_dns(dns) if dns else ""
    }

# ==============================================================================
# TEMPLATES HTML EMBUTIDOS (ARQUIVO ÚNICO PARA GITHUB)
# ==============================================================================
PLAYER_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>StreamFlix - WebPlayer IPTV</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <script src="https://cdn.jsdelivr.net/npm/mpegts.js@latest/dist/mpegts.js"></script>
    <script src="https://unpkg.com/lucide@latest"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        body { background-color: #07070a; color: #f8fafc; font-family: 'Inter', sans-serif; overflow-x: hidden; }
        .glass { background: rgba(15, 16, 22, 0.88); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
        .poster-card { transition: transform 0.2s ease, box-shadow 0.2s ease; }
        .poster-card:hover { transform: translateY(-4px); box-shadow: 0 12px 24px -8px rgba(99, 102, 241, 0.3); }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: #07070a; }
        ::-webkit-scrollbar-thumb { background: #252736; border-radius: 9999px; }
    </style>
</head>
<body class="min-h-screen flex flex-col">

    <!-- TOAST -->
    <div id="toast" class="fixed top-5 right-5 z-50 hidden">
        <div class="glass px-4 py-3 rounded-xl shadow-2xl border-l-4 border-indigo-500 flex items-center gap-3">
            <span id="toast-msg" class="text-xs font-medium text-slate-100"></span>
        </div>
    </div>

    <!-- 1. TELA DE LOGIN -->
    <section id="view-login" class="min-h-screen flex items-center justify-center p-4">
        <div class="glass w-full max-w-md rounded-3xl p-8 shadow-2xl">
            <div class="flex flex-col items-center text-center mb-6">
                <div class="w-14 h-14 rounded-2xl bg-indigo-600 flex items-center justify-center shadow-lg shadow-indigo-600/30 mb-3">
                    <i data-lucide="tv" class="w-7 h-7 text-white"></i>
                </div>
                <h1 class="text-2xl font-bold text-white">StreamFlix <span class="text-indigo-400 font-light">WebPlayer</span></h1>
                <p class="text-xs text-slate-400 mt-1">Acesse com seu usuário ou gere um teste automático</p>
            </div>

            <form onsubmit="handleLogin(event)" class="space-y-4">
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Usuário</label>
                    <input type="text" id="login-user" required placeholder="Digite seu usuário"
                        class="w-full bg-slate-900 border border-white/10 focus:border-indigo-500 rounded-xl px-4 py-3 text-sm text-white outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold uppercase text-slate-400 mb-1">Senha</label>
                    <input type="password" id="login-pass" required placeholder="••••••••"
                        class="w-full bg-slate-900 border border-white/10 focus:border-indigo-500 rounded-xl px-4 py-3 text-sm text-white outline-none">
                </div>
                <button type="submit" id="btn-login" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-3.5 rounded-xl transition cursor-pointer">
                    Entrar no WebPlayer
                </button>
            </form>

            <div class="relative my-5 flex items-center justify-center">
                <div class="border-t border-white/10 w-full"></div>
                <span class="bg-[#0f1016] px-3 text-[11px] uppercase text-slate-500">Teste Grátis</span>
                <div class="border-t border-white/10 w-full"></div>
            </div>

            <button type="button" onclick="generateTrial()" id="btn-trial" class="w-full bg-emerald-500/15 hover:bg-emerald-500/25 border border-emerald-500/40 text-emerald-300 font-semibold py-3.5 rounded-xl transition cursor-pointer">
                Gerar Teste Automático & Entrar
            </button>

            <div class="mt-6 pt-4 border-t border-white/5 flex justify-between items-center text-xs text-slate-500">
                <span>Proxy VPS Ativo</span>
                <a href="/admin" class="text-slate-400 hover:text-white underline">Painel Admin</a>
            </div>
        </div>
    </section>

    <!-- 2. TELA DO WEBPLAYER -->
    <section id="view-player" class="hidden min-h-screen flex flex-col">
        <header class="fixed top-0 inset-x-0 z-30 glass h-16 px-4 lg:px-8 flex items-center justify-between gap-2">
            <div class="flex items-center gap-3">
                <button type="button" onclick="toggleDrawer()" class="p-2.5 rounded-xl bg-white/5 hover:bg-white/10 text-white border border-white/10 flex items-center gap-2 cursor-pointer">
                    <i data-lucide="menu" class="w-5 h-5 text-indigo-400"></i>
                    <span class="text-xs font-semibold hidden sm:inline">Categorias</span>
                </button>

                <div class="flex items-center gap-1 bg-slate-900 p-1 rounded-xl border border-white/5">
                    <button type="button" onclick="switchMediaTab('live')" id="tab-live" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 text-white cursor-pointer">Ao Vivo</button>
                    <button type="button" onclick="switchMediaTab('vod')" id="tab-vod" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white cursor-pointer">Filmes</button>
                    <button type="button" onclick="switchMediaTab('series')" id="tab-series" class="px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white cursor-pointer">Séries</button>
                </div>
            </div>

            <div class="flex items-center gap-2.5">
                <input type="text" id="search-box" oninput="renderItems()" placeholder="Buscar..."
                    class="hidden sm:block bg-slate-900 border border-white/10 rounded-xl px-3.5 py-1.5 text-xs text-white outline-none w-44 lg:w-64">

                <a id="btn-pay" href="{{ default_pay_url }}" target="_blank" rel="noopener noreferrer"
                    class="bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-bold px-3.5 py-2 rounded-xl shadow-lg transition flex items-center gap-1.5">
                    <i data-lucide="credit-card" class="w-4 h-4"></i>
                    <span>Fazer Pagamento</span>
                </a>

                <button type="button" onclick="logoutUser()" class="p-2 rounded-xl bg-white/5 hover:bg-rose-500/20 text-slate-400 hover:text-rose-400 cursor-pointer" title="Sair">
                    <i data-lucide="log-out" class="w-4 h-4"></i>
                </button>
            </div>
        </header>

        <!-- MENU LATERAL DE CATEGORIAS OCULTO -->
        <div id="drawer-backdrop" onclick="toggleDrawer()" class="fixed inset-0 bg-black/75 z-40 hidden"></div>
        <aside id="drawer-menu" class="fixed top-0 left-0 bottom-0 w-72 glass z-50 transform -translate-x-full transition-transform duration-300 flex flex-col">
            <div class="p-4 border-b border-white/10 flex items-center justify-between">
                <span class="font-bold text-sm uppercase tracking-wider text-white">Categorias</span>
                <button type="button" onclick="toggleDrawer()" class="p-1.5 rounded-lg bg-white/5 text-slate-400 cursor-pointer">
                    <i data-lucide="x" class="w-4 h-4"></i>
                </button>
            </div>
            <div class="p-3 border-b border-white/5">
                <input type="text" id="cat-filter" oninput="renderCategoryList()" placeholder="Filtrar categoria..."
                    class="w-full bg-slate-900 border border-white/10 rounded-lg px-3 py-2 text-xs text-white outline-none">
            </div>
            <div id="category-container" class="flex-1 overflow-y-auto p-3 space-y-1"></div>
        </aside>

        <!-- CONTEÚDO -->
        <main class="flex-1 pt-20 px-4 lg:px-8 pb-16">
            <div class="flex items-center justify-between mb-5">
                <div class="flex items-center gap-3">
                    <h2 id="section-title" class="text-lg font-bold text-white">Canais Ao Vivo</h2>
                    <span id="items-count" class="text-xs px-2.5 py-0.5 rounded-full bg-white/5 text-slate-400">0 itens</span>
                </div>
                <button type="button" onclick="toggleDrawer()" class="text-xs text-indigo-400 hover:underline cursor-pointer">Trocar Categoria</button>
            </div>

            <div id="items-grid" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 xl:grid-cols-7 gap-4"></div>
        </main>
    </section>

    <!-- 3. MODAL DE SÉRIES (EPISÓDIOS) -->
    <div id="series-modal" class="fixed inset-0 z-50 bg-black/90 hidden items-center justify-center p-4">
        <div class="glass w-full max-w-2xl rounded-2xl overflow-hidden flex flex-col max-h-[80vh]">
            <div class="p-4 border-b border-white/10 flex items-center justify-between">
                <h3 id="series-title" class="font-bold text-white text-base">Episódios</h3>
                <button type="button" onclick="closeSeriesModal()" class="p-1.5 rounded-lg bg-white/10 text-white cursor-pointer">✕</button>
            </div>
            <div id="series-episodes" class="p-4 overflow-y-auto space-y-2 flex-1"></div>
        </div>
    </div>

    <!-- 4. MODAL DO PLAYER DE VÍDEO (VELOCIDADES VOD 1.0, 1.3, 1.5, 1.8, 2.0) -->
    <div id="video-modal" class="fixed inset-0 z-50 bg-black/95 hidden flex-col justify-between p-4 lg:p-6">
        <div class="flex flex-wrap items-center justify-between gap-3 max-w-6xl w-full mx-auto">
            <div class="flex items-center gap-3">
                <button type="button" onclick="closePlayer()" class="p-2.5 rounded-xl bg-white/10 hover:bg-white/20 text-white cursor-pointer">
                    <i data-lucide="arrow-left" class="w-5 h-5"></i>
                </button>
                <h3 id="player-title" class="font-bold text-white text-sm lg:text-base truncate max-w-md">Reproduzindo</h3>
            </div>

            <div class="flex items-center gap-2">
                <button type="button" id="btn-alt-ts" onclick="switchLiveFormat()" class="hidden px-3 py-1.5 rounded-xl bg-amber-500/20 border border-amber-500/40 text-amber-300 text-xs font-semibold cursor-pointer">
                    Trocar Formato (.m3u8 / .ts)
                </button>

                <div id="vod-speeds" class="flex items-center gap-1 glass px-3 py-1.5 rounded-xl">
                    <span class="text-[11px] text-slate-400 mr-1">Velocidade:</span>
                    <button type="button" onclick="setSpeed(1.0)" data-rate="1" class="spd-btn px-2 py-1 rounded text-xs font-bold bg-indigo-600 text-white cursor-pointer">1.0x</button>
                    <button type="button" onclick="setSpeed(1.3)" data-rate="1.3" class="spd-btn px-2 py-1 rounded text-xs font-bold bg-white/5 text-slate-300 cursor-pointer">1.3x</button>
                    <button type="button" onclick="setSpeed(1.5)" data-rate="1.5" class="spd-btn px-2 py-1 rounded text-xs font-bold bg-white/5 text-slate-300 cursor-pointer">1.5x</button>
                    <button type="button" onclick="setSpeed(1.8)" data-rate="1.8" class="spd-btn px-2 py-1 rounded text-xs font-bold bg-white/5 text-slate-300 cursor-pointer">1.8x</button>
                    <button type="button" onclick="setSpeed(2.0)" data-rate="2" class="spd-btn px-2 py-1 rounded text-xs font-bold bg-white/5 text-slate-300 cursor-pointer">2.0x</button>
                </div>
            </div>
        </div>

        <div class="my-auto max-w-6xl w-full mx-auto aspect-video bg-black rounded-2xl overflow-hidden border border-white/10 flex items-center justify-center">
            <video id="video-el" controls playsinline class="w-full h-full object-contain"></video>
        </div>

        <div class="max-w-6xl w-full mx-auto text-center text-[11px] text-slate-500">
            StreamFlix Proxy Engine • HLS & MPEG-TS
        </div>
    </div>

    <script>
        let currentUser = null;
        let currentTab = 'live';
        let currentCategoryId = 'all';
        let categoriesList = [];
        let streamsList = [];
        let hls = null;
        let mpegtsPlayer = null;
        let activeStreamObj = null;

        window.addEventListener('DOMContentLoaded', () => {
            if (window.lucide) lucide.createIcons();
            const saved = localStorage.getItem('sf_vps_user');
            if (saved) {
                try {
                    const parsed = JSON.parse(saved);
                    if (parsed.username && parsed.password) {
                        document.getElementById('login-user').value = parsed.username;
                        document.getElementById('login-pass').value = parsed.password;
                    }
                } catch (e) {}
            }
        });

        function toast(msg) {
            const t = document.getElementById('toast');
            document.getElementById('toast-msg').textContent = msg;
            t.classList.remove('hidden');
            clearTimeout(window._tTimer);
            window._tTimer = setTimeout(() => t.classList.add('hidden'), 4000);
        }

        function toggleDrawer() {
            const menu = document.getElementById('drawer-menu');
            const back = document.getElementById('drawer-backdrop');
            const isOpen = !menu.classList.contains('-translate-x-full');
            if (isOpen) {
                menu.classList.add('-translate-x-full');
                back.classList.add('hidden');
            } else {
                menu.classList.remove('-translate-x-full');
                back.classList.remove('hidden');
            }
        }

        async function generateTrial() {
            const btn = document.getElementById('btn-trial');
            btn.textContent = 'Gerando teste na API...';
            btn.disabled = true;
            try {
                const res = await fetch('/api/trial', { method: 'POST' });
                const data = await res.json();
                if (!data.ok) {
                    toast(data.error || 'Erro ao gerar teste.');
                    return;
                }
                document.getElementById('login-user').value = data.username;
                document.getElementById('login-pass').value = data.password;
                toast('Teste criado: ' + data.username);
                await doLogin(data.username, data.password);
            } catch (e) {
                toast('Falha ao comunicar com o servidor.');
            } finally {
                btn.textContent = 'Gerar Teste Automático & Entrar';
                btn.disabled = false;
            }
        }

        async function handleLogin(e) {
            e.preventDefault();
            const u = document.getElementById('login-user').value.trim();
            const p = document.getElementById('login-pass').value.trim();
            await doLogin(u, p);
        }

        async function doLogin(username, password) {
            const btn = document.getElementById('btn-login');
            btn.textContent = 'Conectando ao servidor IPTV...';
            btn.disabled = true;
            try {
                const res = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username, password })
                });
                const data = await res.json();
                if (!data.ok) {
                    toast(data.error || 'Falha no login');
                    return;
                }
                currentUser = data;
                localStorage.setItem('sf_vps_user', JSON.stringify(data));
                if (data.pay_url) {
                    document.getElementById('btn-pay').href = data.pay_url;
                }
                document.getElementById('view-login').classList.add('hidden');
                document.getElementById('view-player').classList.remove('hidden');
                await switchMediaTab('live');
            } catch (e) {
                toast('Erro de conexão com a VPS.');
            } finally {
                btn.textContent = 'Entrar no WebPlayer';
                btn.disabled = false;
            }
        }

        function logoutUser() {
            document.getElementById('view-player').classList.add('hidden');
            document.getElementById('view-login').classList.remove('hidden');
        }

        async function switchMediaTab(tab) {
            currentTab = tab;
            currentCategoryId = 'all';

            ['live', 'vod', 'series'].forEach(t => {
                const el = document.getElementById('tab-' + t);
                if (t === tab) {
                    el.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold bg-indigo-600 text-white cursor-pointer';
                } else {
                    el.className = 'px-3 py-1.5 rounded-lg text-xs font-semibold text-slate-400 hover:text-white cursor-pointer';
                }
            });

            const titles = { live: 'Canais Ao Vivo', vod: 'Filmes On Demand', series: 'Séries' };
            document.getElementById('section-title').textContent = titles[tab];

            const catAction = tab === 'live' ? 'get_live_categories' : (tab === 'vod' ? 'get_vod_categories' : 'get_series_categories');
            try {
                const res = await fetch(`/api/xtream?username=${encodeURIComponent(currentUser.username)}&password=${encodeURIComponent(currentUser.password)}&action=${catAction}`);
                const data = await res.json();
                categoriesList = Array.isArray(data) ? data : [];
                renderCategoryList();
                const firstCat = categoriesList.length > 0 ? categoriesList[0].category_id : 'all';
                await loadStreamsByCategory(firstCat, categoriesList.length > 0 ? categoriesList[0].category_name : titles[tab]);
            } catch (e) {
                toast('Erro ao carregar categorias.');
            }
        }

        function renderCategoryList() {
            const filter = (document.getElementById('cat-filter').value || '').toLowerCase();
            const container = document.getElementById('category-container');
            container.innerHTML = '';

            categoriesList
                .filter(c => (c.category_name || '').toLowerCase().includes(filter))
                .forEach(c => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    const active = String(currentCategoryId) === String(c.category_id);
                    btn.className = 'w-full text-left px-3 py-2.5 rounded-xl text-xs font-medium transition flex items-center justify-between cursor-pointer ' + (active ? 'bg-indigo-600 text-white' : 'text-slate-300 hover:bg-white/5');
                    btn.textContent = c.category_name;
                    btn.addEventListener('click', () => {
                        toggleDrawer();
                        loadStreamsByCategory(c.category_id, c.category_name);
                    });
                    container.appendChild(btn);
                });
        }

        async function loadStreamsByCategory(catId, catName) {
            currentCategoryId = catId;
            renderCategoryList();
            document.getElementById('section-title').textContent = catName;
            const grid = document.getElementById('items-grid');
            grid.innerHTML = '<div class="col-span-full py-12 text-center text-xs text-slate-400">Carregando conteúdos da categoria...</div>';

            const streamAction = currentTab === 'live' ? 'get_live_streams' : (currentTab === 'vod' ? 'get_vod_streams' : 'get_series');
            try {
                const res = await fetch(`/api/xtream?username=${encodeURIComponent(currentUser.username)}&password=${encodeURIComponent(currentUser.password)}&action=${streamAction}&category_id=${encodeURIComponent(catId)}`);
                const data = await res.json();
                streamsList = Array.isArray(data) ? data : [];
                renderItems();
            } catch (e) {
                grid.innerHTML = '<div class="col-span-full py-12 text-center text-xs text-rose-400">Erro ao carregar lista.</div>';
            }
        }

        function renderItems() {
            const q = (document.getElementById('search-box').value || '').toLowerCase();
            const filtered = streamsList.filter(s => (s.name || '').toLowerCase().includes(q));
            document.getElementById('items-count').textContent = filtered.length + ' itens';

            const grid = document.getElementById('items-grid');
            grid.innerHTML = '';

            filtered.slice(0, 120).forEach(item => {
                const card = document.createElement('div');
                card.className = 'poster-card cursor-pointer rounded-2xl overflow-hidden bg-slate-900 border border-white/5 flex flex-col';
                const imgUrl = item.stream_icon || item.cover || 'https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?auto=format&fit=crop&w=400&q=80';

                card.innerHTML = `
                    <div class="relative aspect-[2/3] w-full bg-black overflow-hidden">
                        <img src="${imgUrl}" loading="lazy" onerror="this.src='https://images.unsplash.com/photo-1522869635100-9f4c5e86aa37?auto=format&fit=crop&w=400&q=80'" class="w-full h-full object-cover">
                    </div>
                    <div class="p-3">
                        <h3 class="item-name text-xs font-bold text-slate-100 truncate"></h3>
                    </div>
                `;
                card.querySelector('.item-name').textContent = item.name || 'Sem título';
                card.addEventListener('click', () => onItemClick(item));
                grid.appendChild(card);
            });
        }

        function onItemClick(item) {
            const u = encodeURIComponent(currentUser.username);
            const p = encodeURIComponent(currentUser.password);
            const dns = currentUser.dns;

            if (currentTab === 'live') {
                const urlM3u8 = `${dns}/live/${u}/${p}/${item.stream_id}.m3u8`;
                const urlTs = `${dns}/live/${u}/${p}/${item.stream_id}.ts`;
                openPlayer(item.name, urlM3u8, 'live', urlTs);
            } else if (currentTab === 'vod') {
                const ext = item.container_extension || 'mp4';
                const urlVod = `${dns}/movie/${u}/${p}/${item.stream_id}.${ext}`;
                openPlayer(item.name, urlVod, 'vod', null);
            } else if (currentTab === 'series') {
                openSeriesEpisodes(item.series_id, item.name);
            }
        }

        async function openSeriesEpisodes(seriesId, title) {
            const modal = document.getElementById('series-modal');
            const container = document.getElementById('series-episodes');
            document.getElementById('series-title').textContent = title;
            container.innerHTML = '<p class="text-xs text-slate-400 text-center py-8">Carregando episódios...</p>';
            modal.classList.remove('hidden');
            modal.classList.add('flex');

            try {
                const res = await fetch(`/api/xtream?username=${encodeURIComponent(currentUser.username)}&password=${encodeURIComponent(currentUser.password)}&action=get_series_info&series_id=${seriesId}`);
                const data = await res.json();
                const episodes = data.episodes || {};
                container.innerHTML = '';

                Object.keys(episodes).forEach(season => {
                    episodes[season].forEach(ep => {
                        const ext = ep.container_extension || 'mp4';
                        const epUrl = `${currentUser.dns}/series/${encodeURIComponent(currentUser.username)}/${encodeURIComponent(currentUser.password)}/${ep.id}.${ext}`;
                        const epName = `T${season}:E${ep.episode_num} - ${ep.title || 'Episódio'}`;

                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'w-full text-left p-3 rounded-xl bg-slate-900 hover:bg-indigo-600/20 border border-white/5 flex justify-between items-center text-xs text-white cursor-pointer';
                        btn.textContent = epName;
                        btn.addEventListener('click', () => {
                            closeSeriesModal();
                            openPlayer(epName, epUrl, 'vod', null);
                        });
                        container.appendChild(btn);
                    });
                });
            } catch (e) {
                container.innerHTML = '<p class="text-xs text-rose-400 text-center py-8">Erro ao carregar episódios.</p>';
            }
        }

        function closeSeriesModal() {
            const modal = document.getElementById('series-modal');
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }

        function destroyPlayers() {
            if (hls) { hls.destroy(); hls = null; }
            if (mpegtsPlayer) { mpegtsPlayer.destroy(); mpegtsPlayer = null; }
        }

        function openPlayer(title, url, type, altTsUrl) {
            activeStreamObj = { title, url, type, altTsUrl, usingTs: false };
            document.getElementById('player-title').textContent = title;
            const modal = document.getElementById('video-modal');
            const speeds = document.getElementById('vod-speeds');
            const btnAlt = document.getElementById('btn-alt-ts');

            if (type === 'live') {
                speeds.classList.add('hidden');
                btnAlt.classList.remove('hidden');
            } else {
                speeds.classList.remove('hidden');
                btnAlt.classList.add('hidden');
            }

            modal.classList.remove('hidden');
            modal.classList.add('flex');
            startVideoEngine(url);
            setSpeed(1.0);
        }

        function startVideoEngine(url) {
            destroyPlayers();
            const video = document.getElementById('video-el');
            const lower = url.toLowerCase();

            if (lower.endsWith('.m3u8') && window.Hls && Hls.isSupported()) {
                hls = new Hls({ enableWorker: true, lowLatencyMode: true });
                hls.loadSource(url);
                hls.attachMedia(video);
                hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
                hls.on(Hls.Events.ERROR, (_, data) => {
                    if (data.fatal && activeStreamObj && activeStreamObj.altTsUrl && !activeStreamObj.usingTs) {
                        activeStreamObj.usingTs = true;
                        startVideoEngine(activeStreamObj.altTsUrl);
                    }
                });
            } else if (lower.endsWith('.ts') && window.mpegts && mpegts.getFeatureList().mseLivePlayback) {
                mpegtsPlayer = mpegts.createPlayer({ type: 'mpegts', isLive: true, url: url });
                mpegtsPlayer.attachMediaElement(video);
                mpegtsPlayer.load();
                mpegtsPlayer.play().catch(() => {});
            } else {
                video.src = url;
                video.play().catch(() => {});
            }
        }

        function switchLiveFormat() {
            if (!activeStreamObj || !activeStreamObj.altTsUrl) return;
            activeStreamObj.usingTs = !activeStreamObj.usingTs;
            const nextUrl = activeStreamObj.usingTs ? activeStreamObj.altTsUrl : activeStreamObj.url;
            toast(activeStreamObj.usingTs ? 'Mudando para fluxo MPEG-TS (.ts)' : 'Mudando para fluxo HLS (.m3u8)');
            startVideoEngine(nextUrl);
        }

        function setSpeed(rate) {
            const video = document.getElementById('video-el');
            video.playbackRate = rate;
            video.preservesPitch = true;
            document.querySelectorAll('.spd-btn').forEach(b => {
                if (parseFloat(b.getAttribute('data-rate')) === rate) {
                    b.className = 'spd-btn px-2 py-1 rounded text-xs font-bold bg-indigo-600 text-white cursor-pointer';
                } else {
                    b.className = 'spd-btn px-2 py-1 rounded text-xs font-bold bg-white/5 text-slate-300 cursor-pointer';
                }
            });
        }

        function closePlayer() {
            const modal = document.getElementById('video-modal');
            const video = document.getElementById('video-el');
            video.pause();
            video.removeAttribute('src');
            destroyPlayers();
            modal.classList.add('hidden');
            modal.classList.remove('flex');
        }
    </script>
</body>
</html>"""

ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Painel Admin - StreamFlix VPS</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { background-color: #07070a; color: #f8fafc; font-family: 'Inter', sans-serif; }
        .glass { background: rgba(15, 16, 22, 0.88); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
    </style>
</head>
<body class="min-h-screen p-6 lg:p-10">

    {% if not logged %}
    <div class="max-w-sm mx-auto mt-20 glass rounded-3xl p-8">
        <h1 class="text-xl font-bold text-white mb-2">Login Administrativo</h1>
        <p class="text-xs text-slate-400 mb-6">Senha padrão inicial: <code class="text-indigo-400">admin123</code></p>
        {% if error %}
        <div class="mb-4 p-3 rounded-xl bg-rose-500/20 border border-rose-500/40 text-rose-300 text-xs">{{ error }}</div>
        {% endif %}
        <form method="POST" class="space-y-4">
            <input type="hidden" name="action" value="login">
            <input type="password" name="admin_pass" required placeholder="Senha do Admin"
                class="w-full bg-slate-900 border border-white/10 rounded-xl px-4 py-3 text-sm text-white outline-none">
            <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-3 rounded-xl">Entrar no Painel</button>
        </form>
        <div class="mt-4 text-center">
            <a href="/" class="text-xs text-slate-400 hover:text-white underline">Voltar para o WebPlayer</a>
        </div>
    </div>
    {% else %}
    <div class="max-w-5xl mx-auto space-y-6">
        <div class="flex flex-wrap items-center justify-between gap-4 pb-5 border-b border-white/10">
            <div>
                <span class="text-xs font-bold uppercase text-indigo-400">Página Admin Independente</span>
                <h1 class="text-2xl font-bold text-white">Configurações StreamFlix VPS</h1>
            </div>
            <div class="flex items-center gap-3">
                <a href="/" class="px-4 py-2 rounded-xl bg-white/5 hover:bg-white/10 text-xs font-semibold text-white border border-white/10">Abrir WebPlayer</a>
                <form method="POST" class="inline">
                    <input type="hidden" name="action" value="logout">
                    <button type="submit" class="px-4 py-2 rounded-xl bg-rose-500/20 text-rose-300 text-xs font-semibold">Sair</button>
                </form>
            </div>
        </div>

        {% if saved %}
        <div class="p-4 rounded-xl bg-emerald-500/20 border border-emerald-500/40 text-emerald-300 text-xs font-semibold">
            Configurações salvas com sucesso no banco SQLite da VPS!
        </div>
        {% endif %}

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <form method="POST" class="glass rounded-2xl p-6 space-y-4">
                <input type="hidden" name="action" value="save">
                <h2 class="font-bold text-base text-white border-b border-white/5 pb-2">DNS Xtream & APIs</h2>

                <div>
                    <label class="block text-xs text-slate-300 mb-1">DNS do Servidor IPTV (Ex: http://trevcdn.online)</label>
                    <input type="text" name="dns" value="{{ cfg.dns }}" required
                        class="w-full bg-slate-900 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white font-mono">
                </div>

                <div>
                    <label class="block text-xs text-slate-300 mb-1">URL da API de Teste Automático</label>
                    <input type="text" name="trial_api" value="{{ cfg.trial_api }}"
                        class="w-full bg-slate-900 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white font-mono">
                </div>

                <div>
                    <label class="block text-xs text-slate-300 mb-1">Link de Pagamento Padrão (Fallback)</label>
                    <input type="text" name="default_pay_url" value="{{ cfg.default_pay_url }}"
                        class="w-full bg-slate-900 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white font-mono">
                </div>

                <div>
                    <label class="block text-xs text-slate-300 mb-1">TMDB API Key</label>
                    <input type="text" name="tmdb_key" value="{{ cfg.tmdb_key }}"
                        class="w-full bg-slate-900 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white font-mono">
                </div>

                <div>
                    <label class="block text-xs text-slate-300 mb-1">Alterar Senha Admin (Deixe em branco para manter)</label>
                    <input type="password" name="new_admin_pass" placeholder="Nova senha..."
                        class="w-full bg-slate-900 border border-white/10 rounded-xl px-3.5 py-2.5 text-sm text-white">
                </div>

                <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-semibold py-3 rounded-xl text-xs">
                    Salvar Configurações na VPS
                </button>
            </form>

            <div class="glass rounded-2xl p-6 space-y-4">
                <h2 class="font-bold text-base text-white border-b border-white/5 pb-2">Últimos Testes & Logins Salvos</h2>
                <div class="space-y-2 max-h-96 overflow-y-auto pr-1">
                    {% for u in recent_users %}
                    <div class="p-3 rounded-xl bg-slate-900 border border-white/5 text-xs">
                        <div class="flex justify-between font-mono font-bold text-white">
                            <span>{{ u.username }} / {{ u.password }}</span>
                            <span class="text-slate-500 font-sans">{{ u.created_at }}</span>
                        </div>
                        <p class="text-[11px] text-emerald-400 truncate mt-1">PayURL: {{ u.pay_url }}</p>
                    </div>
                    {% else %}
                    <p class="text-xs text-slate-500 py-8 text-center">Nenhum acesso registrado ainda.</p>
                    {% endfor %}
                </div>
            </div>
        </div>
    </div>
    {% endif %}
</body>
</html>"""

# ==============================================================================
# ROTAS FLASK (USANDO render_template_string PARA ARQUIVO ÚNICO)
# ==============================================================================
@app.route("/")
def index():
    cfg = get_settings()
    return render_template_string(
        PLAYER_HTML,
        default_pay_url=cfg.get("default_pay_url", ""),
        tmdb_key=cfg.get("tmdb_key", "")
    )

@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    cfg = get_settings()
    error = None

    if request.method == "POST":
        action = request.form.get("action")
        if action == "login":
            pwd = request.form.get("admin_pass", "")
            if pwd == cfg.get("admin_pass", "admin123"):
                session["admin_logged"] = True
                return redirect(url_for("admin_page"))
            else:
                error = "Senha administrativa incorreta."
        elif action == "save" and session.get("admin_logged"):
            new_dns = clean_dns(request.form.get("dns", ""))
            new_trial = request.form.get("trial_api", "").strip()
            new_pay = request.form.get("default_pay_url", "").strip()
            new_tmdb = request.form.get("tmdb_key", "").strip()
            new_pass = request.form.get("new_admin_pass", "").strip()

            conn = get_db()
            conn.execute("REPLACE INTO settings (key, value) VALUES ('dns', ?)", (new_dns,))
            conn.execute("REPLACE INTO settings (key, value) VALUES ('trial_api', ?)", (new_trial,))
            conn.execute("REPLACE INTO settings (key, value) VALUES ('default_pay_url', ?)", (new_pay,))
            conn.execute("REPLACE INTO settings (key, value) VALUES ('tmdb_key', ?)", (new_tmdb,))
            if new_pass:
                conn.execute("REPLACE INTO settings (key, value) VALUES ('admin_pass', ?)", (new_pass,))
            conn.commit()
            conn.close()
            return redirect(url_for("admin_page", saved="1"))
        elif action == "logout":
            session.pop("admin_logged", None)
            return redirect(url_for("admin_page"))

    conn = get_db()
    recent_users = conn.execute("SELECT username, password, pay_url, created_at FROM users_pay ORDER BY created_at DESC LIMIT 30").fetchall()
    conn.close()

    return render_template_string(
        ADMIN_HTML,
        logged=session.get("admin_logged", False),
        cfg=get_settings(),
        recent_users=recent_users,
        error=error,
        saved=request.args.get("saved")
    )

@app.route("/api/trial", methods=["POST"])
def api_generate_trial():
    cfg = get_settings()
    trial_url = cfg.get("trial_api", "").strip()
    if not trial_url:
        return jsonify({"ok": False, "error": "URL da API de Teste não configurada no Admin."}), 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*"
    }
    try:
        resp = requests.post(trial_url, json={}, headers=headers, timeout=15)
        if resp.status_code >= 400:
            resp = requests.get(trial_url, headers=headers, timeout=15)
        raw_text = resp.text
    except Exception as e:
        return jsonify({"ok": False, "error": f"Falha ao contactar API de Teste: {str(e)}"}), 502

    parsed = parse_trial_response(raw_text)
    if not parsed["username"] or not parsed["password"]:
        return jsonify({
            "ok": False,
            "error": "A API respondeu, mas não foi possível extrair usuário/senha.",
            "raw": raw_text[:500]
        }), 422

    pay_url = parsed["pay_url"] or cfg.get("default_pay_url", "")
    conn = get_db()
    conn.execute(
        "REPLACE INTO users_pay (username, password, pay_url) VALUES (?, ?, ?)",
        (parsed["username"], parsed["password"], pay_url)
    )
    if parsed["dns"] and not cfg.get("dns"):
        conn.execute("REPLACE INTO settings (key, value) VALUES ('dns', ?)", (parsed["dns"],))
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "username": parsed["username"],
        "password": parsed["password"],
        "pay_url": pay_url
    })

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "error": "Informe usuário e senha."}), 400

    cfg = get_settings()
    dns = clean_dns(cfg.get("dns", ""))
    if not dns:
        return jsonify({"ok": False, "error": "DNS não configurado no Painel Admin."}), 400

    url = f"{dns}/player_api.php?username={username}&password={password}"
    headers = {"User-Agent": "IPTVSmartersPlayer"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        info = r.json()
        user_info = info.get("user_info", {})
        if str(user_info.get("auth", 0)) == "0":
            return jsonify({"ok": False, "error": "Usuário ou senha inválidos no servidor IPTV."}), 401
    except Exception as e:
        return jsonify({"ok": False, "error": f"Erro ao autenticar no DNS ({dns}): {str(e)}"}), 502

    conn = get_db()
    row = conn.execute("SELECT pay_url FROM users_pay WHERE username = ?", (username,)).fetchone()
    pay_url = row["pay_url"] if row and row["pay_url"] else cfg.get("default_pay_url", "")
    conn.execute(
        "REPLACE INTO users_pay (username, password, pay_url) VALUES (?, ?, ?)",
        (username, password, pay_url)
    )
    conn.commit()
    conn.close()

    return jsonify({
        "ok": True,
        "username": username,
        "password": password,
        "dns": dns,
        "pay_url": pay_url
    })

@app.route("/api/xtream")
def api_xtream():
    username = request.args.get("username", "").strip()
    password = request.args.get("password", "").strip()
    action = request.args.get("action", "").strip()
    category_id = request.args.get("category_id", "").strip()
    series_id = request.args.get("series_id", "").strip()

    cfg = get_settings()
    dns = clean_dns(cfg.get("dns", ""))
    if not dns or not username or not password:
        return jsonify({"error": "Credenciais ou DNS ausentes"}), 400

    params = {
        "username": username,
        "password": password,
        "action": action
    }
    if category_id and category_id != "all":
        params["category_id"] = category_id
    if series_id:
        params["series_id"] = series_id

    headers = {"User-Agent": "IPTVSmartersPlayer"}
    try:
        r = requests.get(f"{dns}/player_api.php", params=params, headers=headers, timeout=25)
        return Response(r.content, status=r.status_code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8095, debug=False)
