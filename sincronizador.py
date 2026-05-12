#!/usr/bin/env python3
"""
Endure.LabbS Sync Engine v3.1
Sincronização: TrainingPeaks -> Intervals.icu

Lógica:
  1. Cookie persistente por atleta (armazenado em cookies.json)
  2. Tudo via requests (leve, rápido, sem browser)
  3. Browser (Playwright+Chrome) só para renovar cookie quando expira
  4. API do Intervals.icu usa Basic auth com "API_KEY:<key>" em base64
  5. Sincronização TP -> Intervals com deduplicação
"""

import json
import base64
import requests
from datetime import date, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright

# ============================================================
# CONFIGS
# ============================================================

CHROME_PATH = "/usr/bin/google-chrome-stable"
TP_API_URL = "https://tpapi.trainingpeaks.com"
INTERVALS_API_URL = "https://intervals.icu/api/v1"
COOKIE_STORE = Path(__file__).parent / "cookies.json"

# Coach API key (endure-labbs) — acesso a todos os atletas
COACH_API_KEY = "3o68ipgae5ndvi5u445tvqd90"

# Mapeamento workoutTypeValueId do TP -> tipo do Intervals.icu
# Valores confirmados via API: 1=Swim, 2=Ride, 3=Run
TP_TYPE_MAP = {
    1: ("Swim", "Swimming"),
    2: ("Ride", "Cycling"),
    3: ("Run", "Running"),
    4: ("Walk", "Walk/Hike"),
    5: ("Workout", "Cross-Training"),
    6: ("Strength", "Strength"),
    12: ("Ride", "Mountain Bike"),
    13: ("Run", "Treadmill Run"),
    14: ("Ride", "Trainer Ride"),
}

ATHLETES = {
    "bruno-abud": {
        "tp_username": "brunoabudd",
        "tp_password": "Bruno@1985",
        "intervals_athlete_id": "i463516",
    },
    "gabriel-sousa": {
        "tp_username": "GASousa",
        "tp_password": "Sous@245",
        "intervals_athlete_id": "i543031",
    },
    "rafael": {
        "tp_username": "Coachenjoytri",
        "tp_password": "261207",
        "intervals_athlete_id": "i560211",
    },
}

# ============================================================
# COOKIE STORE (persistência local)
# ============================================================

def _load_cookies() -> dict:
    if COOKIE_STORE.exists():
        return json.loads(COOKIE_STORE.read_text())
    return {}

def _save_cookies(data: dict):
    COOKIE_STORE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def get_stored_cookie(athlete_key: str) -> str | None:
    store = _load_cookies()
    entry = store.get(athlete_key, {})
    return entry.get("cookie")

def store_cookie(athlete_key: str, cookie: str, user_id: str = None):
    store = _load_cookies()
    store[athlete_key] = {
        "cookie": cookie,
        "user_id": user_id,
        "captured_at": date.today().isoformat(),
    }
    _save_cookies(store)

# ============================================================
# TRAINING PEAKS - LOGIN VIA BROWSER (só para renovar cookie)
# ============================================================

def tp_login_browser(username: str, password: str) -> dict:
    """
    Login no TrainingPeaks via browser headless.
    Retorna {"success": True, "cookie": "...", "user_id": "..."} ou {"success": False, "error": "..."}
    """
    print(f"  [BROWSER] Iniciando login para {username}...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            executable_path=CHROME_PATH,
            args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-software-rasterizer", "--no-first-run",
                "--disable-background-networking", "--disable-sync", "--mute-audio",
            ],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        page = context.new_page()

        try:
            # Ir direto para o fluxo OAuth
            page.goto("https://app.trainingpeaks.com/", wait_until="networkidle", timeout=30000)

            # Se não redirecionou para login, forçar logout+relogin
            if "oauth" not in page.url:
                page.goto("https://oauth.trainingpeaks.com/Account/LogOff", wait_until="networkidle", timeout=15000)
                page.goto("https://app.trainingpeaks.com/", wait_until="networkidle", timeout=30000)

            # Se estamos no app (logado), capturar cookie direto
            if "oauth" not in page.url and "login" not in page.url.lower():
                cookies = context.cookies()
                tp_auth = next((c for c in cookies if c["name"] == "Production_tpAuth"), None)
                if tp_auth:
                    cookie_str = f"Production_tpAuth={tp_auth['value']}"
                    access_token = _tp_cookie_to_token(cookie_str)
                    user_id = _tp_get_user_id(access_token)
                    browser.close()
                    return {"success": True, "cookie": cookie_str, "user_id": user_id}

            # Estamos na página de login
            page.wait_for_selector('input[name="Username"], input[type="email"]', timeout=15000)

            username_sel = 'input[name="Username"]' if page.query_selector('input[name="Username"]') else 'input[type="email"]'
            password_sel = 'input[name="Password"]' if page.query_selector('input[name="Password"]') else 'input[type="password"]'

            page.fill(username_sel, username)
            page.fill(password_sel, password)

            btn = page.query_selector('button[type="submit"]') or page.query_selector('input[type="submit"]')
            if btn:
                btn.click()

            try:
                page.wait_for_url("**/app.trainingpeaks.com/**", timeout=30000)
            except:
                pass

            import time
            time.sleep(2)

            cookies = context.cookies()
            tp_auth = next((c for c in cookies if c["name"] == "Production_tpAuth"), None)

            if not tp_auth:
                cookie_names = [c["name"] for c in cookies]
                browser.close()
                return {"success": False, "error": f"Cookie nao encontrado. Cookies: {cookie_names}"}

            cookie_str = f"Production_tpAuth={tp_auth['value']}"
            access_token = _tp_cookie_to_token(cookie_str)
            user_id = _tp_get_user_id(access_token)

            browser.close()
            return {"success": True, "cookie": cookie_str, "user_id": user_id}

        except Exception as e:
            browser.close()
            return {"success": False, "error": str(e)}


# ============================================================
# TRAINING PEAKS - API via requests (rápido, sem browser)
# ============================================================

def _tp_cookie_to_token(cookie: str) -> str | None:
    """Troca cookie Production_tpAuth por Bearer access_token."""
    try:
        resp = requests.get(
            f"{TP_API_URL}/users/v3/token",
            headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("access_token") or data.get("token", {}).get("access_token")
        return None
    except:
        return None


def _tp_get_user_id(access_token: str) -> str | None:
    try:
        resp = requests.get(
            f"{TP_API_URL}/users/v3/user",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return str(data.get("user", {}).get("userId", ""))
        return None
    except:
        return None


def tp_get_workouts(access_token: str, user_id: str, start_date: str, end_date: str) -> list:
    try:
        resp = requests.get(
            f"{TP_API_URL}/fitness/v6/athletes/{user_id}/workouts/{start_date}/{end_date}",
            headers={"Authorization": f"Bearer {access_token}", "User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        print(f"  [TP] get_workouts {resp.status_code}: {resp.text[:150]}")
        return []
    except Exception as e:
        print(f"  [TP] get_workouts erro: {e}")
        return []


# ============================================================
# INTERVALS.ICU - API via requests (auth: Basic API_KEY:<key>)
# ============================================================

def _intervals_auth() -> str:
    """Gera header Authorization correto para Intervals.icu."""
    cred = base64.b64encode(f"API_KEY:{COACH_API_KEY}".encode()).decode()
    return f"Basic {cred}"


def intervals_list_events(athlete_id: str, from_date: str, to_date: str) -> list:
    try:
        resp = requests.get(
            f"{INTERVALS_API_URL}/athlete/{athlete_id}/events",
            headers={"Authorization": _intervals_auth()},
            params={"from": from_date, "to": to_date},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, list) else []
        return []
    except:
        return []


def intervals_create_event(athlete_id: str, event: dict) -> dict | None:
    try:
        resp = requests.post(
            f"{INTERVALS_API_URL}/athlete/{athlete_id}/events",
            headers={"Authorization": _intervals_auth(), "Content-Type": "application/json"},
            json=event,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return resp.json()
        print(f"  [Intervals] create {resp.status_code}: {resp.text[:150]}")
        return None
    except Exception as e:
        print(f"  [Intervals] erro: {e}")
        return None


def intervals_delete_event(athlete_id: str, event_id: str) -> bool:
    try:
        resp = requests.delete(
            f"{INTERVALS_API_URL}/athlete/{athlete_id}/events/{event_id}",
            headers={"Authorization": _intervals_auth()},
            timeout=15,
        )
        return resp.status_code in (200, 204)
    except:
        return False


# ============================================================
# CONVERSÃO TP WORKOUT -> INTERVALS EVENT
# ============================================================

# Mapeamento intensityClass do TP -> seção do Intervals Workout Builder
INTENSITY_CLASS_MAP = {
    "warmUp": "Warmup",
    "active": None,       # depende do contexto (serie principal)
    "rest": None,         # vira descanso dentro de repeat
    "coolDown": "Cooldown",
    "recovery": None,     # descanso entre intervalos
}

# Mapeamento primaryIntensityMetric -> target format do Intervals
TP_METRIC_MAP = {
    "percentOfFtp": "power",       # ciclismo -> Z1, Z2, % FTP
    "percentOfThresholdHr": "hr",  # qualquer -> Z1 HR, Z2 HR
    "percentOfThresholdPace": "pace",  # corrida/natação -> Z1 Pace, Z2 Pace
}

# Cache de zonas por atleta (evita fetch repetido)
_athlete_settings_cache: dict = {}


def _fetch_athlete_zones(athlete_id: str) -> dict:
    """
    Busca zonas do atleta no Intervals.icu via API do athlete.
    Os pace_zones/(hr_zones/power_zones) já são percentages do threshold,
    que é exatamente o que o TrainingPeaks também envia como percentOfThreshold*.
    
    Retorna dict: {"pace": [77.6, 87.7, 94.5, 100.0, 103.4, 111.6, 134.4], "power": [...], "hr": [...]}
    """
    if athlete_id in _athlete_settings_cache:
        return _athlete_settings_cache[athlete_id]

    url = f"{INTERVALS_API_URL}/athlete/{athlete_id}"
    try:
        resp = requests.get(url, headers={"Authorization": _intervals_auth()}, timeout=10)
        if resp.status_code != 200:
            return {}
        data = resp.json()
    except Exception:
        return {}

    sport_settings = data.get("sportSettings", [])
    result = {"pace": None, "power": None, "hr": None, "pace_threshold": None, "hr_threshold": None, "ftp": None}

    for ss in sport_settings:
        types = ss.get("types", [])

        # Power zones (for cycling)
        if any(t in types for t in ("Ride", "VirtualRide", "Cyclocross")):
            result["power"] = ss.get("power_zones")
            result["ftp"] = ss.get("ftp")
            result["hr_threshold"] = ss.get("lthr")
            result["hr"] = ss.get("hr_zones")

        # Pace zones (for run and swim)
        if any(t in types for t in ("Run", "VirtualRun", "TrailRun")):
            result["pace"] = ss.get("pace_zones")
            result["pace_threshold"] = ss.get("threshold_pace")

        if any(t in types for t in ("Swim", "OpenWaterSwim")):
            if result["pace"] is None:
                result["pace"] = ss.get("pace_zones")
                result["pace_threshold"] = ss.get("threshold_pace")

    _athlete_settings_cache[athlete_id] = result
    return result


TP_RUN_ZONE_BOUNDARIES = [
    (77, "Z1"),   # Z1: 0-77% threshold pace
    (87, "Z2"),   # Z2: 77-87%
    (94, "Z3"),   # Z3: 87-94%
    (100, "Z4"),  # Z4: 94-100%
    (105, "Z5A"), # Z5A: 100-105%
    (120, "Z5B"), # Z5B: 105-120%
    (999, "Z5C"), # Z5C: 120%+
]

TP_TO_INTERVALS_MAP = {
    "Z1": "Z1", "Z2": "Z2", "Z3": "Z3", "Z4": "Z4",
    "Z5A": "Z5", "Z5B": "Z6", "Z5C": "Z7",
}


def _pct_to_zone_dynamic(pct: float, metric: str, athlete_zones: dict) -> str:
    """
    Converte % do threshold (TP) em zona do Intervals.icu usando mapping direto.

    O TrainingPeaks mostra zonas Z1-Z7 para todos os esportes.
    O mapping para o Intervals.icu é direto:
      Z1→Z1, Z2→Z2, Z3→Z3, Z4→Z4, Z5A→Z5, Z5B→Z6, Z5C→Z7

    Para determinar a zona, usamos os boundaries do TP para cada modalidade:
      - Pace (corrida): Z1=0-77%, Z2=77-87%, Z3=87-94%, Z4=94-100%, Z5A=100-105%, Z5B=105-120%, Z5C=120%+
      - HR: boundaries específicos do atleta (convertemos % → BPM via LTHR)
      - Power: boundaries específicos do atleta (% direto do FTP)
    """
    if metric == "pace":
        # Usar boundaries fixos do TP para corrida
        boundaries = [
            (77, "Z1"), (87, "Z2"), (94, "Z3"), (100, "Z4"),
            (105, "Z5A"), (120, "Z5B"), (999, "Z5C"),
        ]
        for boundary, tp_zone in boundaries:
            if pct <= boundary:
                return TP_TO_INTERVALS_MAP.get(tp_zone, tp_zone)
        return "Z7"

    elif metric == "hr":
        # HR: converter % para BPM via LTHR do atleta, depois usar zonas de HR do Intervals
        hr_zones = athlete_zones.get("hr", [])
        threshold_hr = athlete_zones.get("hr_threshold", 0)
        if not threshold_hr or not hr_zones:
            return "HR?"
        target_hr = int(pct * threshold_hr / 100)
        for i, zone_max in enumerate(hr_zones):
            if target_hr <= zone_max:
                return f"Z{i+1}"
        return f"Z{len(hr_zones)}"

    else:
        # Power: boundaries do atleta (% FTP)
        power_zones = athlete_zones.get("power", [])
        if not power_zones:
            return "Z?"
        for i, boundary in enumerate(power_zones):
            if pct <= boundary:
                return f"Z{i+1}"
        return f"Z{len(power_zones)}"


def _format_duration(length: dict, sport: str) -> str:
    """Converte o campo length do TP em formato do Intervals.icu."""
    value = length.get("value", 0)
    unit = length.get("unit", "")

    if unit == "second":
        if value >= 3600:
            h = value // 3600
            m = (value % 3600) // 60
            return f"{h}h{m}m" if m else f"{h}h"
        if value >= 60:
            m = value // 60
            s = value % 60
            return f"{m}m{s}s" if s else f"{m}m"
        return f"{value}s"

    if unit == "minute":
        return f"{int(value)}m"

    if unit == "hour":
        total_min = int(value * 60)
        h = total_min // 60
        m = total_min % 60
        return f"{h}h{m}m" if m else f"{h}h"

    if unit == "meter":
        # NUNCA usar "m" sozinho — o Workout Builder do Intervals.icu interpreta "m" como minutos!
        # Para natação: "meters" explicitamente.
        # Para corrida: "meters" para distancias < 1000, "km" para >= 1000 (parser reconhece "km" como quilometros).
        if value >= 1000:
            return f"{value/1000:.1f}km".replace(".0km", "km")
        return f"{int(value)}meters"

    if unit == "kilometer":
        return f"{value:.1f}km".replace(".0km", "km")

    if unit == "repetition":
        return None  # repetitions são tratadas no nível do bloco

    return f"{int(value)}m"  # fallback


def _format_target(targets: list, metric: str, sport: str, athlete_zones: dict = None) -> str:
    """
    Converte targets do TP em zona/target do Intervals.icu.
    Para ranges de zona (ex: Z2-Z3), mostra a faixa em vez de uma zona única.
    """
    if not targets:
        return ""

    min_val = targets[0].get("minValue", 0)
    max_val = targets[0].get("maxValue", 0)

    zone_type = TP_METRIC_MAP.get(metric, "power")

    zone_min = _pct_to_zone_dynamic(min_val, zone_type, athlete_zones or {})
    zone_max = _pct_to_zone_dynamic(max_val, zone_type, athlete_zones or {})

    # Se min != max, mostrar range de zonas (Z2-Z3)
    if zone_min != zone_max:
        zone = f"{zone_min}-{zone_max}"
    else:
        zone = zone_min

    # Sufixo por esporte/métrica
    if sport == "Swim":
        return f"{zone} Pace"
    elif sport == "Run" or zone_type == "pace":
        return f"{zone} Pace"
    elif zone_type == "hr":
        return f"{zone} HR"
    else:  # Ride / power
        # Sweet spot: 88-94% FTP
        if min_val >= 88 and max_val <= 94:
            return "88-94% FTP"
        # Power zones (ex: Z2, Z3)
        return f"{zone}"


def _swim_structure_to_description(structure: dict) -> str:
    """
    Converte a structure de natação do TP em description RAW para o Intervals.icu.
    Não converte zonas - usa os labels brutos do TP (A0, A1, AN1, SOLTO, etc.)
    que o Intervals.icu interpreta via DE:PARA configurado pelo usuário.
    """
    steps_list = structure.get("structure", [])
    if not steps_list:
        return ""

    lines = []
    current_section = None

    def emit_section(name):
        nonlocal current_section
        if name and name != current_section:
            if lines:
                lines.append("")
            lines.append(name)
            current_section = name

    def emit_step(block):
        sub_steps = block.get("steps", [])
        for s in sub_steps:
            length = s.get("length", {})
            value = length.get("value", 0)
            unit = length.get("unit", "")
            name = s.get("name", "").strip()

            if not value:
                continue

            # Formatar distância/duração (SEMPRE usar "meters" — "m" = minutos no parser do Intervals!)
            if unit == "meter":
                dist_str = f"{int(value)}meters"
            elif unit == "second":
                dist_str = f"{int(value)}s"
            elif unit == "minute":
                dist_str = f"{int(value)}min"
            else:
                dist_str = f"{int(value)}{unit}"

            # Usar o nome bruto do TP (A1, SOLTO, etc.)
            if name:
                lines.append(f"- {dist_str} {name}")
            else:
                lines.append(f"- {dist_str}")

    for block in steps_list:
        block_type = block.get("type", "step")
        reps = block.get("length", {}).get("value", 1) if block.get("length", {}).get("unit") == "repetition" else 1
        sub_steps = block.get("steps", [])
        if not sub_steps:
            continue

        first = sub_steps[0]
        name = first.get("name", "").strip()
        iclass = first.get("intensityClass", "")

        # Classificar seção
        if iclass == "warmUp" or any(kw in name.lower() for kw in ["warm", "aquec", "solto"]):
            section = "Aquecimento"
        elif iclass == "coolDown" or any(kw in name.lower() for kw in ["cool", "desaq"]):
            section = "Desaquecimento"
        elif name.lower() in ("a0", "a1", "a2", "a3", "an1", "an2", "an3"):
            section = name  # Seção nomeada pela zona (A1, AN1, etc.)
        else:
            section = "Serie Principal"

        if reps > 1:
            emit_section(f"{section} x{reps}")
            # Para reptições, mostrar apenas o primeiro passo (o bloco se repete)
            emit_step(block)
        else:
            if section != current_section:
                emit_section(section)
            emit_step(block)

    return "\n".join(lines)


def _structure_to_description(structure: dict, sport: str, athlete_zones: dict = None) -> str:
    """
    Converte a structure do TP em description formatada para o Workout Builder do Intervals.icu.
    O Intervals.icu parseia a description automaticamente para criar o workout estruturado.
    
    Estratégia: agrupar blocos em seções lógicas (Warmup, Main Set, Cooldown).
    Blocos de repetição consecutivos (ex: tiros com distâncias decrescentes) são
    consolidados em uma única seção "Tiros" com todos os intervalos listados.
    """
    steps_list = structure.get("structure", [])
    if not steps_list:
        return ""

    metric = structure.get("primaryIntensityMetric", "")
    lines = []
    block_count = len(steps_list)
    
    # Fase 1: classificar cada bloco em warmup / main / cooldown / rest
    classified = []
    for i, block in enumerate(steps_list):
        sub_steps = block.get("steps", [])
        if not sub_steps:
            classified.append({"phase": "skip", "block": block})
            continue
        
        first = sub_steps[0]
        iclass = first.get("intensityClass", "active")
        name = first.get("name", "").strip().lower()
        
        if iclass == "warmUp" or any(kw in name for kw in ["warm", "aquec", "solto"]):
            phase = "warmup"
        elif iclass == "coolDown" or any(kw in name for kw in ["cool", "desaq", "volta a calma"]):
            phase = "cooldown"
        elif iclass in ("rest", "recovery") and len(sub_steps) == 1:
            phase = "rest_standalone"
        elif block_count <= 2 and iclass == "active":
            # Treino simples (1-2 blocos ativos) - sem divisão
            phase = "main_simple"
        elif i == 0 and iclass not in ("coolDown",):
            phase = "warmup"
        elif i == block_count - 1 and iclass not in ("warmUp",):
            phase = "cooldown"
        else:
            phase = "main"
        
        classified.append({"phase": phase, "block": block, "iclass": iclass, "name": first.get("name", "").strip()})
    
    # Fase 2: gerar linhas por seção
    current_section = None
    
    def emit_section(name):
        nonlocal current_section
        if name and name != current_section:
            if lines:
                lines.append("")
            lines.append(name)
            current_section = name
    
    def emit_step(block, sport, metric):
        block_type = block.get("type", "step")
        reps = block.get("length", {}).get("value", 1) if block.get("length", {}).get("unit") == "repetition" else 1
        sub_steps = block.get("steps", [])
        
        if block_type == "repetition" and len(sub_steps) > 1:
            for s in sub_steps:
                dur = _format_duration(s.get("length", {}), sport)
                if dur is None:
                    continue
                target = _format_target(s.get("targets", []), metric, sport, athlete_zones)
                s_iclass = s.get("intensityClass", "")

                if s_iclass in ("rest", "recovery"):
                    s_unit = s.get("length", {}).get("unit", "")
                    if s_unit in ("second", "minute"):
                        rest_dur = _format_duration(s.get("length", {}), sport)
                        lines.append(f"- Rest {rest_dur}")
                    else:
                        if target:
                            lines.append(f"- {dur} {target}")
                        else:
                            lines.append(f"- {dur} Z1 Pace" if sport in ("Swim", "Run") else f"- {dur} Z1")
                else:
                    if target:
                        lines.append(f"- {dur} {target}")
                    else:
                        lines.append(f"- {dur}")
        else:
            for s in sub_steps:
                dur = _format_duration(s.get("length", {}), sport)
                if dur is None:
                    continue
                target = _format_target(s.get("targets", []), metric, sport, athlete_zones)
                s_iclass = s.get("intensityClass", "")
                
                if s_iclass in ("rest", "recovery"):
                    s_unit = s.get("length", {}).get("unit", "")
                    if s_unit in ("second", "minute"):
                        rest_dur = _format_duration(s.get("length", {}), sport)
                        lines.append(f"- Rest {rest_dur}")
                    else:
                        if target:
                            lines.append(f"- {dur} {target}")
                        else:
                            lines.append(f"- {dur} Z1 Pace" if sport in ("Swim", "Run") else f"- {dur} Z1")
                else:
                    if target:
                        lines.append(f"- {dur} {target}")
                    else:
                        lines.append(f"- {dur}")
    
    # Detectar blocos main consecutivos para agrupar
    in_main = False
    main_count = 0
    
    for i, item in enumerate(classified):
        phase = item["phase"]
        block = item["block"]
        block_type = block.get("type", "step")
        reps = block.get("length", {}).get("value", 1) if block.get("length", {}).get("unit") == "repetition" else 1
        sub_steps = block.get("steps", [])
        
        if phase == "skip":
            continue
        
        elif phase == "warmup":
            emit_section("Warmup")
            emit_step(block, sport, metric)
            in_main = False
        
        elif phase == "cooldown":
            emit_section("Cooldown")
            emit_step(block, sport, metric)
            in_main = False
        
        elif phase == "main_simple":
            # Treino simples sem divisão em seções
            emit_step(block, sport, metric)
            in_main = False
        
        elif phase == "rest_standalone":
            # Descanso isolado entre blocos - vira "Rest Xm"
            for s in sub_steps:
                dur = _format_duration(s.get("length", {}), sport)
                if dur:
                    lines.append(f"- Rest {dur}")
            in_main = False
        
        elif phase == "main":
            if not in_main:
                # Verificar se há múltiplos blocos main para agrupar em "Tiros"
                future_mains = sum(1 for x in classified[i:] if x["phase"] == "main")
                if future_mains > 1 and sport == "Run":
                    emit_section("Tiros")
                elif reps > 1 and len(sub_steps) > 1:
                    step_name = item.get("name", "")
                    if step_name and step_name.upper() not in ("ACTIVE", "HARD", "EASY"):
                        emit_section(f"{step_name} Repeat {reps}x")
                    else:
                        emit_section("Tiros" if sport == "Run" else "Serie Principal")
                else:
                    emit_section("Tiros" if sport == "Run" else "Serie Principal")
                in_main = True
                main_count = 0
            
            main_count += 1
            emit_step(block, sport, metric)
    
    return "\n".join(lines)


def tp_to_intervals(tp_w: dict, athlete_id: str = None) -> dict:
    """Converte workout do TP em evento do Intervals.icu com description estruturada para o Workout Builder."""
    wtype = tp_w.get("workoutTypeValueId", 0)
    intervals_type, _ = TP_TYPE_MAP.get(wtype, ("Workout", "Workout"))
    wday = tp_w.get("workoutDay", "")[:10] or str(date.today())
    title = tp_w.get("title", "") or TP_TYPE_MAP.get(wtype, ("", "Workout"))[1]
    coach_desc = tp_w.get("description", "")
    duration = tp_w.get("totalTimePlanned", 0) or 0  # horas decimais
    distance = tp_w.get("distancePlanned", 0) or 0  # metros
    structure = tp_w.get("structure", {})

    # Buscar zonas do atleta para mapeamento preciso de % -> zona
    athlete_zones = _fetch_athlete_zones(athlete_id) if athlete_id else {}

    # Montar description com blocos estruturados para o Workout Builder
    desc_parts = []

    # 1. Blocos estruturados
    if structure and structure.get("structure"):
        if intervals_type == "Swim":
            # Natação: usar labels brutos do TP (A0/A1/AN1) que o Intervals interpreta via DE:PARA
            workout_text = _swim_structure_to_description(structure)
        else:
            # Corrida/Bike: usar zonas Intervals (Z1-Z7)
            workout_text = _structure_to_description(structure, intervals_type, athlete_zones)
        if workout_text:
            desc_parts.append(workout_text)

    # 2. Notas do coach (após linha em branco, conforme regra do parser)
    if coach_desc:
        coach_clean = coach_desc.strip()
        desc_parts.append("")
        desc_parts.append(coach_clean)

    # 3. Rodapé
    desc_parts.append("")
    desc_parts.append("[Synced from TrainingPeaks via Endure.LabbS]")

    description = "\n".join(desc_parts)

    # Campos obrigatórios do Intervals.icu
    event = {
        "start_date_local": f"{wday}T06:00:00",
        "type": intervals_type,
        "category": "WORKOUT",
        "name": title,
        "description": description,
    }

    if duration > 0:
        event["moving_time"] = int(duration * 3600)  # duration em horas -> segundos

    # TSS estimado: TSS = (IF^2 * duration_hours) * 100
    # duration vem em horas decimais do TP (totalTimePlanned)
    if_planned = tp_w.get("ifPlanned", 0) or 0
    tss_planned = tp_w.get("tssPlanned", 0) or 0

    if tss_planned > 0:
        # Usar tssPlanned do TP diretamente (mais preciso)
        event["load_target"] = round(tss_planned)
    elif if_planned and duration > 0:
        # Fallback: calcular manualmente
        # TSS = IF^2 * duration_hours * 100
        tss = (if_planned ** 2) * duration * 100
        event["load_target"] = round(tss)

    # external_id para deduplicação
    tp_id = tp_w.get("workoutId", "")
    if tp_id:
        event["external_id"] = f"tp-{tp_id}"

    return event


# ============================================================
# ORQUESTRADOR: obter access_token (cookie persistente + fallback browser)
# ============================================================

def get_tp_access(athlete_key: str) -> dict:
    """
    Retorna {"access_token": "...", "user_id": "...", "cookie_refreshed": bool}
    Lógica:
      1. Tenta cookie armazenado -> troca por access_token
      2. Se falhar, renova via browser
    """
    refreshed = False

    # 1. Tentar cookie armazenado
    stored_cookie = get_stored_cookie(athlete_key)
    if stored_cookie:
        print(f"  [COOKIE] Cookie armazenado encontrado, testando...")
        access_token = _tp_cookie_to_token(stored_cookie)
        if access_token:
            user_id = _tp_get_user_id(access_token)
            if user_id:
                print(f"  [COOKIE] Valido! user_id={user_id}")
                return {"access_token": access_token, "user_id": user_id, "cookie_refreshed": False}
        print(f"  [COOKIE] Expirado ou invalido.")

    # 2. Login via browser
    a = ATHLETES[athlete_key]
    print(f"  [BROWSER] Renovando cookie via login...")
    login = tp_login_browser(a["tp_username"], a["tp_password"])
    if not login.get("success"):
        return {"error": f"Falha no login: {login.get('error')}"}

    store_cookie(athlete_key, login["cookie"], login.get("user_id"))
    print(f"  [COOKIE] Novo cookie salvo! user_id={login.get('user_id')}")

    return {
        "access_token": _tp_cookie_to_token(login["cookie"]),
        "user_id": login.get("user_id"),
        "cookie_refreshed": True,
    }


# ============================================================
# SINCRONIZAÇÃO PRINCIPAL
# ============================================================

def sync_athlete(athlete_key: str, days_ahead: int = 7, days_back: int = 0) -> dict:
    """Sincroniza treinos de um atleta: TP -> Intervals.icu"""
    a = ATHLETES.get(athlete_key)
    if not a:
        return {"success": False, "error": f"Atleta nao encontrado: {athlete_key}"}

    print(f"\n{'='*60}")
    print(f"SYNC: {athlete_key}")
    print(f"{'='*60}")

    # 1. Obter acesso ao TP (cookie persistente ou browser)
    tp_access = get_tp_access(athlete_key)
    if "error" in tp_access:
        return {"success": False, "error": tp_access["error"]}

    access_token = tp_access["access_token"]
    user_id = tp_access["user_id"]

    if not access_token or not user_id:
        return {"success": False, "error": "access_token ou user_id vazios"}

    # 2. Buscar treinos do TP
    start_date = (date.today() - timedelta(days=days_back)).isoformat()
    end_date = (date.today() + timedelta(days=days_ahead)).isoformat()
    print(f"\n  Buscando treinos TP: {start_date} -> {end_date}")

    tp_workouts = tp_get_workouts(access_token, user_id, start_date, end_date)
    print(f"  Encontrados: {len(tp_workouts)} treinos no TP")

    if not tp_workouts:
        return {"success": True, "synced": 0, "skipped": 0, "errors": 0, "total_tp": 0}

    # 3. Buscar eventos existentes no Intervals (deduplicação)
    i_athlete_id = a["intervals_athlete_id"]
    existing = intervals_list_events(i_athlete_id, start_date, end_date)
    existing_keys = set()
    for ev in existing:
        ekey = f"{ev.get('start_date_local','')[:10]}|{ev.get('name','')}"
        existing_keys.add(ekey)
    print(f"  Eventos existentes no Intervals: {len(existing)}")

    # 4. Sincronizar
    synced = 0
    skipped = 0
    errors = 0

    for tp_w in tp_workouts:
        event = tp_to_intervals(tp_w, athlete_id=i_athlete_id)
        ekey = f"{event['start_date_local'][:10]}|{event['name']}"

        if ekey in existing_keys:
            print(f"  SKIP: {event['start_date_local'][:10]} - {event['name']}")
            skipped += 1
            continue

        print(f"  SYNC: {event['start_date_local'][:10]} - {event['name']} ({event['type']})")
        result = intervals_create_event(i_athlete_id, event)
        if result:
            synced += 1
        else:
            errors += 1

    print(f"\n  RESULTADO: {synced} sincronizados, {skipped} pulados, {errors} erros")
    return {
        "success": True,
        "synced": synced,
        "skipped": skipped,
        "errors": errors,
        "total_tp": len(tp_workouts),
        "cookie_refreshed": tp_access.get("cookie_refreshed", False),
    }


# ============================================================
# COMANDO: capturar cookie (sem sincronizar)
# ============================================================

def capture_cookie(athlete_key: str) -> dict:
    """Faz login no TP e salva o cookie. Nao sincroniza nada."""
    a = ATHLETES.get(athlete_key)
    if not a:
        return {"success": False, "error": f"Atleta nao encontrado: {athlete_key}"}

    print(f"CAPTURE COOKIE: {athlete_key}")
    login = tp_login_browser(a["tp_username"], a["tp_password"])
    if not login.get("success"):
        return login

    store_cookie(athlete_key, login["cookie"], login.get("user_id"))
    access_token = _tp_cookie_to_token(login["cookie"])
    user_id = _tp_get_user_id(access_token)

    print(f"  Cookie salvo!")
    print(f"  user_id: {user_id}")
    print(f"  access_token: {'OK' if access_token else 'FALHOU'}")

    return {
        "success": True,
        "user_id": user_id,
        "access_token_valid": access_token is not None,
    }


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Endure.LabbS Sync Engine v3.1")
        print("")
        print("Uso:")
        print("  python sincronizador.py capture <athlete_key>   # Captura cookie via browser")
        print("  python sincronizador.py sync <athlete_key>      # Sincroniza TP->Intervals")
        print("  python sincronizador.py sync-all               # Sincroniza todos os atletas")
        print(f"\nAtletas: {list(ATHLETES.keys())}")
        sys.exit(0)

    cmd = sys.argv[1]

    if cmd == "capture":
        key = sys.argv[2] if len(sys.argv) > 2 else list(ATHLETES.keys())[0]
        result = capture_cookie(key)
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")

    elif cmd == "sync":
        key = sys.argv[2] if len(sys.argv) > 2 else list(ATHLETES.keys())[0]
        result = sync_athlete(key)
        print(f"\n{json.dumps(result, indent=2, ensure_ascii=False)}")

    elif cmd == "sync-all":
        results = {}
        for key in ATHLETES:
            results[key] = sync_athlete(key)
        print(f"\n{'='*60}")
        print(f"RESULTADO FINAL")
        print(f"{'='*60}")
        print(json.dumps(results, indent=2, ensure_ascii=False))

    else:
        print(f"Comando desconhecido: {cmd}")
        print("Use: capture, sync, sync-all")
