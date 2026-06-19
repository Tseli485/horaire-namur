# -*- coding: utf-8 -*-
"""
HoraireManager - Prison de Namur / SPF Justice
Application web de gestion des horaires rotatifs
Lancement: python app_horaire.py  -> http://localhost:5050
"""
import sys, os, secrets
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

import json, uuid as _uuid
from datetime import date, timedelta
from calendar import monthrange, isleap
from pathlib import Path

from flask import Flask, jsonify, request, Response, session
from werkzeug.security import generate_password_hash, check_password_hash

from horaire_agent import get_shift, MONTH_NAMES_FR, DAY_NAMES_FR, CYCLE_LEN, ANCHOR
from conges_bosa import LEAVE_CATALOG, get_public_holidays, get_vac_entitlement, get_sick_capital

app = Flask(__name__)
_data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DATA_FILE  = _data_dir / "agenda_data.json"

def _persistent_secret_key():
    """Clé de session stable entre redémarrages (sinon tout le monde est
    déconnecté à chaque reload). Priorité à la variable d'env SECRET_KEY,
    sinon clé générée une fois et conservée dans DATA_DIR/.secret_key."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    kp = _data_dir / ".secret_key"
    try:
        if kp.exists():
            return kp.read_text(encoding="utf-8").strip()
        k = os.urandom(32).hex()
        _data_dir.mkdir(parents=True, exist_ok=True)
        kp.write_text(k, encoding="utf-8")
        return k
    except Exception:
        return os.urandom(32)

app.secret_key = _persistent_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)  # 30 jours

# ── Auth PAR AGENT (chacun son PIN, accès à son seul horaire) ──────────────
# Chaque agent possède un pin_hash. La connexion ouvre une session liée à son
# identifiant ; toute route comportant <aid> est verrouillée sur l'agent connecté.
_PUBLIC_PATHS = {"/", "/manifest.json", "/icon.svg", "/dev-version",
                 "/api/auth/login", "/api/auth/register", "/api/auth/me", "/api/auth/logout"}

def current_aid():
    return session.get("agent_id")

@app.before_request
def check_auth():
    p = request.path
    if p in _PUBLIC_PATHS or p.startswith("/static"):
        return
    # iCal : accès sans session via jeton secret propre à l'agent
    # (permet la synchro Google Agenda tout en restant privé : le lien d'un
    #  agent est indevinable et ne donne accès qu'à SON horaire)
    if p.startswith("/ical/"):
        ag_id = (request.view_args or {}).get("aid")
        tok   = request.args.get("token", "")
        ag    = load()["agents"].get(ag_id) if ag_id else None
        if ag and tok and secrets.compare_digest(tok, ag.get("ical_token") or ""):
            return
        return jsonify({"error": "Lien iCal invalide ou expiré"}), 403
    aid = current_aid()
    if not aid:
        return jsonify({"error": "Non autorisé", "login_required": True}), 401
    # Propriété : une route avec <aid> ne peut viser QUE l'agent connecté
    route_aid = (request.view_args or {}).get("aid")
    if route_aid is not None and route_aid != aid:
        return jsonify({"error": "Accès interdit à l'horaire d'un autre agent"}), 403

@app.route("/api/auth/register", methods=["POST"])
def api_register():
    """Crée un compte agent avec son PIN et ouvre la session."""
    data = load()
    body = request.json or {}
    aid  = (body.get("id") or "").strip().upper()
    name = (body.get("name") or "").strip()
    pin  = str(body.get("pin") or "")
    if not _AID_RE.match(aid):
        return jsonify({"error": "Identifiant invalide (lettres/chiffres, 1-64)"}), 400
    if aid in data["agents"]:
        return jsonify({"error": "Cet identifiant existe déjà — choisissez-en un autre"}), 409
    if not name:
        return jsonify({"error": "Nom obligatoire"}), 400
    if len(pin) < 4:
        return jsonify({"error": "PIN trop court (4 chiffres minimum)"}), 400
    r45 = body.get("regime_4_5")
    data["agents"][aid] = {
        "name":         name,
        "birth_date":   body.get("birth_date") or None,
        "career_start": body.get("career_start") or None,
        "team_offset":  int(body.get("offset", 0)),
        "regime_4_5":   int(r45) if r45 not in (None, "") else None,
        "pin_hash":     generate_password_hash(pin),
        "ical_token":   secrets.token_urlsafe(16),
    }
    save(data)
    session.permanent = True
    session["agent_id"] = aid
    return jsonify({"ok": True, "id": aid})

@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """Connexion : identifiant + PIN."""
    data = load()
    body = request.json or {}
    aid  = (body.get("id") or "").strip().upper()
    pin  = str(body.get("pin") or "")
    ag = data["agents"].get(aid)
    if not ag:
        return jsonify({"error": "Identifiant ou PIN incorrect"}), 403
    ph = ag.get("pin_hash")
    if not ph:
        # Agent existant sans PIN (ancienne donnée) : le 1er PIN saisi le définit
        ag["pin_hash"] = generate_password_hash(pin)
        save(data)
    elif not check_password_hash(ph, pin):
        return jsonify({"error": "Identifiant ou PIN incorrect"}), 403
    session.permanent = True
    session["agent_id"] = aid
    return jsonify({"ok": True, "id": aid})

@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def api_auth_me():
    """Renvoie l'agent connecté (ou logged_in=False)."""
    aid = current_aid()
    if not aid:
        return jsonify({"logged_in": False})
    ag = load()["agents"].get(aid)
    if not ag:
        session.clear()
        return jsonify({"logged_in": False})
    return jsonify({"logged_in": True, "id": aid, "name": ag["name"],
                    "team_offset": ag.get("team_offset", 0),
                    "regime_4_5": ag.get("regime_4_5"),
                    "birth_date": ag.get("birth_date"),
                    "career_start": ag.get("career_start")})

# ─────────────────────── ROOT + PWA ──────────────────────────
@app.route("/")
def index():
    return Response(HTML, mimetype='text/html; charset=utf-8')

@app.route("/dev-version")
def dev_version():
    mtime = int(Path(__file__).stat().st_mtime)
    return jsonify({"v": mtime})

@app.route("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "HoraireManager — Prison de Namur",
        "short_name": "Horaire",
        "description": "Gestion des horaires rotatifs SPF Justice",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#1e293b",
        "orientation": "any",
        "icons": [{"src": "/icon.svg", "type": "image/svg+xml", "sizes": "any"}]
    })

@app.route("/icon.svg")
def pwa_icon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" rx="20" fill="#0f172a"/>'
        '<rect x="14" y="28" width="72" height="58" rx="9" fill="none" stroke="#3b82f6" stroke-width="5"/>'
        '<line x1="14" y1="46" x2="86" y2="46" stroke="#3b82f6" stroke-width="5"/>'
        '<line x1="34" y1="28" x2="34" y2="18" stroke="#94a3b8" stroke-width="5" stroke-linecap="round"/>'
        '<line x1="66" y1="28" x2="66" y2="18" stroke="#94a3b8" stroke-width="5" stroke-linecap="round"/>'
        '<rect x="24" y="54" width="13" height="11" rx="3" fill="#ef4444"/>'
        '<rect x="44" y="54" width="13" height="11" rx="3" fill="#f97316"/>'
        '<rect x="64" y="54" width="13" height="11" rx="3" fill="#22c55e"/>'
        '<rect x="24" y="70" width="13" height="11" rx="3" fill="#22c55e"/>'
        '<rect x="44" y="70" width="13" height="11" rx="3" fill="#ef4444"/>'
        '<rect x="64" y="70" width="13" height="11" rx="3" fill="#f97316"/>'
        '</svg>'
    )
    return svg, 200, {"Content-Type": "image/svg+xml"}

# ─────────────────────── DATA HELPERS ────────────────────────
def load():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8-sig"))
    return {"agents": {}, "events": [], "reliquats": {}, "capitals": {}, "exchanges": [], "remarks": {}, "shift_overrides": {}}

def save(data):
    _data_dir.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

def _get_week_shift(d: date, offset: int) -> str:
    """Poste dominant (M ou S) de la semaine contenant d.
    Parcourt lun-ven, ignore R/36/38, retourne M si égalité."""
    monday = d - timedelta(days=d.weekday())
    counts = {'M': 0, 'S': 0}
    for i in range(5):
        sh = get_shift(monday + timedelta(i), offset)
        if sh in ('M', 'S'):
            counts[sh] += 1
    return 'S' if counts['S'] > counts['M'] else 'M'

def _get_displaced_target(d_orig: date, offset: int) -> "date | None":
    """Où atterrit un R ou 38h déplacé par le jour 4/5.
    Avance depuis d_orig+1 en sautant R et 38h (qui restent en place).
    S'arrête au premier M, S ou 36h (la pose qui sera remplacée)."""
    cand = d_orig + timedelta(1)
    for _ in range(7):
        sh = get_shift(cand, offset)
        if sh not in ('R', '38'):
            return cand   # M, S ou 36h → destination du déplacé
        cand += timedelta(1)
    return None

def get_day_info(d: date, agent_id: str, data: dict) -> dict:
    agent   = data["agents"][agent_id]
    offset  = agent["team_offset"]
    regime  = agent.get("regime_4_5")
    raw_base = get_shift(d, offset)
    base     = raw_base

    # ── RÉGIME 4/5 ────────────────────────────────────────────────────────────
    # Règles :
    #  1. Le jour désigné (lun-ven) est TOUJOURS vert "4/5".
    #  2. Si ce jour avait R ou 38h dans le cycle → glisse à droite (en sautant
    #     R et 38h) jusqu'au 1er M/S/36h qui reçoit le repos déplacé.
    #  3. Si ce jour avait 36h → 4/5 prend ce jour, pas de déplacement
    #     (36h hors 4/5 est converti en pose de la semaine).
    #  4. Tous les autres 36h de la semaine → convertis en M ou S.
    # ──────────────────────────────────────────────────────────────────────────
    decale_38 = False     # badge "38h → ici"
    decale_r  = False     # label "Repos décalé"

    if regime is not None:
        # Jour de repos 4/5 de CETTE semaine
        monday   = d - timedelta(days=d.weekday())
        rest_day = monday + timedelta(regime)

        if d == rest_day:
            # Ce jour EST le 4/5 — base R (repos), code géré plus bas
            base = 'R'
        else:
            # Vérifier si un déplacement (R ou 38h) arrive sur ce jour
            # depuis la semaine courante OU la semaine précédente
            # (ex: 38h vendredi → déplacé au lundi suivant)
            displaced = False
            for wdelta in (0, -7):
                ref      = d + timedelta(wdelta)
                mon_ref  = ref - timedelta(days=ref.weekday())
                rest_ref = mon_ref + timedelta(regime)
                raw_rest = get_shift(rest_ref, offset)
                if raw_rest in ('R', '38'):
                    disp = _get_displaced_target(rest_ref, offset)
                    if disp == d:
                        base = raw_rest   # R ou 38h s'installe ici
                        if raw_rest == '38':
                            decale_38 = True
                        else:
                            decale_r = True
                        displaced = True
                        break
            # Si pas de déplacement : convertir 36h en pose de la semaine
            if not displaced and base == '36':
                base = _get_week_shift(d, offset)

    # ── OVERRIDE MANUEL ───────────────────────────────────────────────────────
    shift_ov = data.get("shift_overrides", {}).get(agent_id, {}).get(d.isoformat())
    if shift_ov in ("M", "S", "R", "12H", "08H"):
        base = shift_ov
        decale_38 = False
        decale_r  = False

    # ── FÉRIÉS, CONGÉS ────────────────────────────────────────────────────────
    hols   = {h[0]: (h[1], h[2]) for h in get_public_holidays(d.year)}
    events = [e for e in data["events"]
               if e["agent_id"] == agent_id
               and date.fromisoformat(e["date_start"]) <= d
               <= date.fromisoformat(e["date_end"])]
    eff, code, label = base, None, None
    if d in hols:
        eff, code, label = hols[d][0], hols[d][0], hols[d][1]
    if events:
        ev = events[0]
        eff, code, label = ev["code"], ev["code"], ev["label"]

    # ── JOUR 4/5 (priorité SUR fériés/ponts, sous congés explicites/overrides) ──
    # Un jour 4/5 reste affiché 4/5 même si c'est un jour férié.
    # Seuls les congés encodés manuellement (events) gardent la priorité.
    if regime is not None and shift_ov is None and not events:
        monday   = d - timedelta(days=d.weekday())
        rest_day = monday + timedelta(regime)
        if d == rest_day and d.weekday() < 5:
            eff, code, label = 'R', '4/5', 'Régime 4/5'

    # ── REPOS DÉPLACÉ ─────────────────────────────────────────────────────────
    if code is None and shift_ov is None and (decale_38 or decale_r):
        if decale_38:
            eff, code, label = 'R', 'REPOS-38', 'Repos 38h (décalé ↓)'
        else:
            eff, code, label = 'R', 'REPOS-R',  'Repos (décalé ↓)'

    # ── COULEUR ───────────────────────────────────────────────────────────────
    _BASE_COLOR = {"M": "red", "S": "orange", "12H": "purple", "08H": "teal"}
    if code is None:
        color = _BASE_COLOR.get(base, "green")
    elif code in ("FERIE", "PONT"):
        color = "blue"
    else:
        color = "green"

    return {"date": d.isoformat(), "day_num": d.day, "day_name": DAY_NAMES_FR[d.weekday()],
            "weekday": d.weekday(), "base": base, "effective": eff,
            "code": code, "label": label, "color": color,
            "is_today": d == date.today(), "events": events, "remark": "",
            "decale_38": decale_38, "decale_r": decale_r}

# ─────────────────────── API ROUTES ──────────────────────────
@app.route("/api/agents", methods=["GET"])
def api_agents():
    """Ne renvoie QUE l'agent connecté (isolation des données), sans le pin_hash."""
    aid = current_aid()
    ag  = load()["agents"].get(aid)
    if not ag:
        return jsonify({})
    return jsonify({aid: {k: v for k, v in ag.items() if k != "pin_hash"}})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Désactivé en mode multi-agents (protège les données des autres)."""
    return jsonify({"error": "Réinitialisation désactivée en mode multi-agents"}), 403

import re as _re
_AID_RE = _re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')

@app.route("/api/agents", methods=["POST"])
def api_add_agent():
    """Création de compte : passer par /api/auth/register (avec PIN)."""
    return jsonify({"error": "Utilisez la création de compte (avec PIN)"}), 403

@app.route("/api/agents/<aid>", methods=["PATCH"])
def api_patch_agent(aid):
    # Propriété déjà vérifiée par check_auth (aid == agent connecté)
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    body = request.json
    if "regime_4_5" in body:
        v = body["regime_4_5"]
        data["agents"][aid]["regime_4_5"] = int(v) if v is not None else None
    if "birth_date" in body:
        data["agents"][aid]["birth_date"] = body["birth_date"] or None
    if "career_start" in body:
        data["agents"][aid]["career_start"] = body["career_start"] or None
    if "team_offset" in body:
        data["agents"][aid]["team_offset"] = int(body["team_offset"])
    if body.get("pin"):                       # changement de PIN
        if len(str(body["pin"])) < 4:
            return jsonify({"error": "PIN trop court (4 chiffres minimum)"}), 400
        data["agents"][aid]["pin_hash"] = generate_password_hash(str(body["pin"]))
    save(data)
    ag = {k: v for k, v in data["agents"][aid].items() if k != "pin_hash"}
    return jsonify({"ok": True, "agent": ag})

@app.route("/api/agents/<aid>", methods=["DELETE"])
def api_del_agent(aid):
    data = load()
    data["agents"].pop(aid, None)
    data["events"] = [e for e in data["events"] if e["agent_id"] != aid]
    save(data)
    return jsonify({"ok": True})

VALID_OFFSETS = {0, 7, 14, 21, 28, 35, 42, 49}
SHIFT_LABELS = {'M':'Matin','S':'Soir','R':'Repos','36':'Repos 36h','38':'Repos 38h'}
SHIFT_HOURS  = {'M':'06:00 – 14:00', 'S':'14:00 – 22:00'}

def _hols_dict(year):
    """Construit {date: nom} depuis get_public_holidays (liste de tuples)."""
    return {d: name for d, _, name in get_public_holidays(year)}

_DAY_NAMES = {0:'Lun',1:'Mar',2:'Mer',3:'Jeu',4:'Ven',5:'Sam',6:'Dim'}

def _team_day(dt, offset, hols):
    """Construit le dict jour pour la route team, identique au format get_day_info."""
    sh  = get_shift(dt, offset)
    hol = hols.get(dt)
    # code vide pour les jours normaux — sinon renderGrid affiche le badge CONGÉ
    code  = 'FERIE' if hol else ''
    label = hol if hol else SHIFT_LABELS.get(sh, sh)
    return {
        "date":         dt.isoformat(),
        "weekday":      dt.weekday(),
        "day_name":     _DAY_NAMES[dt.weekday()],
        "day_num":      dt.day,
        "base":         sh,
        "code":         code,
        "shift":        sh,
        "label":        label,
        "is_holiday":   bool(hol),
        "holiday_name": hol or "",
        "shift_hours":  SHIFT_HOURS.get(sh, ""),
        "is_today":     dt == date.today(),
        "remark":       "",
        "events":       [],
        "decale_38":    False,
        "decale_r":     False,
    }

@app.route("/api/calendar/team/<int:offset>/<int:year>/<int:month>")
def api_calendar_team(offset, year, month):
    """Calendrier d'une equipe (pas d'agent requis)."""
    if offset not in VALID_OFFSETS:
        return jsonify({"error": "Offset invalide"}), 400
    days_in_month = monthrange(year, month)[1]
    first_day_wd  = date(year, month, 1).weekday()
    hols = _hols_dict(year)
    days = [_team_day(date(year, month, d), offset, hols) for d in range(1, days_in_month + 1)]
    return jsonify({"year": year, "month": month,
                    "month_name": MONTH_NAMES_FR[month-1],
                    "first_weekday": first_day_wd, "days": days})

@app.route("/api/day/team/<int:offset>/<date_str>")
def api_day_team(offset, date_str):
    """Info jour pour une equipe (pas d'agent requis)."""
    if offset not in VALID_OFFSETS:
        return jsonify({"error": "Offset invalide"}), 400
    try:
        dt = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Date invalide"}), 400
    hols = _hols_dict(dt.year)
    return jsonify(_team_day(dt, offset, hols))

@app.route("/api/calendar/<aid>/<int:year>/<int:month>")
def api_calendar(aid, year, month):
    data  = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    days_in_month = monthrange(year, month)[1]
    first_day_wd  = date(year, month, 1).weekday()   # 0=Lun
    remarks = data.get("remarks", {}).get(aid, {})
    days = []
    for d in range(1, days_in_month + 1):
        day_info = get_day_info(date(year, month, d), aid, data)
        day_info["remark"] = remarks.get(day_info["date"], "")
        days.append(day_info)
    return jsonify({"year": year, "month": month,
                    "month_name": MONTH_NAMES_FR[month-1],
                    "first_weekday": first_day_wd,
                    "days": days})

@app.route("/api/remarks/<aid>/<date_str>", methods=["GET", "PUT"])
def api_remark(aid, date_str):
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    if "remarks" not in data:
        data["remarks"] = {}
    if request.method == "GET":
        txt = data["remarks"].get(aid, {}).get(date_str, "")
        return jsonify({"remark": txt})
    txt = (request.json or {}).get("remark", "").strip()
    if aid not in data["remarks"]:
        data["remarks"][aid] = {}
    if txt:
        data["remarks"][aid][date_str] = txt
    else:
        data["remarks"][aid].pop(date_str, None)
    save(data)
    return jsonify({"ok": True, "remark": txt})

@app.route("/api/shift_override/<aid>/<date_str>", methods=["PUT", "DELETE"])
def api_shift_override(aid, date_str):
    """Remplace ou supprime le poste manuellement pour un jour donné."""
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    data.setdefault("shift_overrides", {})
    if request.method == "DELETE":
        data["shift_overrides"].get(aid, {}).pop(date_str, None)
        save(data)
        return jsonify({"ok": True})
    shift = (request.json or {}).get("shift", "").upper()
    if shift not in ("M", "S", "R", "12H", "08H"):
        return jsonify({"error": "Shift invalide (M/S/R/12H/08H)"}), 400
    data["shift_overrides"].setdefault(aid, {})[date_str] = shift
    save(data)
    return jsonify({"ok": True, "shift": shift})

@app.route("/print/<aid>/<int:year>/<int:month>")
def print_month(aid, year, month):
    """Page A4 imprimable : jours prestés (MATIN/SOIR) du mois."""
    data = load()
    if aid not in data["agents"]:
        return "Agent inconnu", 404
    agent = data["agents"][aid]
    days_in_month = monthrange(year, month)[1]
    remarks = data.get("remarks", {}).get(aid, {})
    worked_days = []
    for d in range(1, days_in_month + 1):
        day_info = get_day_info(date(year, month, d), aid, data)
        day_info["remark"] = remarks.get(day_info["date"], "")
        # Jour presté = poste travaillé (M/S/12H/08H), sans congé/événement posé
        if day_info["base"] in ("M", "S", "12H", "08H") and day_info["code"] is None:
            worked_days.append(day_info)
    month_name = MONTH_NAMES_FR[month - 1]
    matin_count = sum(1 for d in worked_days if d["base"] == "M")
    soir_count  = sum(1 for d in worked_days if d["base"] == "S")
    day_names_full = {"Lun": "Lundi", "Mar": "Mardi", "Mer": "Mercredi",
                      "Jeu": "Jeudi", "Ven": "Vendredi", "Sam": "Samedi", "Dim": "Dimanche"}
    rows_html = ""
    for i, day in enumerate(worked_days, 1):
        _pill = {"M": ("MATIN", "#dbeafe", "#1e40af"),
                 "S": ("SOIR",  "#ffedd5", "#9a3412"),
                 "12H": ("12H", "#ede9fe", "#6d28d9"),
                 "08H": ("08H", "#ccfbf1", "#0f766e")}
        pill_label, pill_bg, pill_color = _pill.get(day["base"], ("SOIR", "#ffedd5", "#9a3412"))
        remark_txt = day.get("remark", "")
        full_name  = day_names_full.get(day["day_name"], day["day_name"])
        rows_html += (
            '<tr>'
            '<td class="num">' + str(i) + '</td>'
            '<td class="date-cell">' + full_name + ' <strong>' + str(day["day_num"]).zfill(2) + '</strong></td>'
            '<td><span class="pill" style="background:' + pill_bg + ';color:' + pill_color + '">' + pill_label + '</span></td>'
            '<td class="remark-cell">' + (remark_txt or '') + '</td>'
            '</tr>'
        )
    if not rows_html:
        rows_html = '<tr><td colspan="4" style="text-align:center;color:#94a3b8;padding:20px">Aucun jour presté ce mois</td></tr>'
    today_str = date.today().strftime("%d/%m/%Y")
    page_html = (
        '<!DOCTYPE html>'
        '<html lang="fr"><head>'
        '<meta charset="utf-8">'
        '<title>Prestations ' + month_name + ' ' + str(year) + ' — ' + agent["name"] + '</title>'
        '<style>'
        '@page{size:A4 portrait;margin:15mm 15mm 20mm 15mm}'
        '*{box-sizing:border-box;margin:0;padding:0}'
        'body{font-family:"Segoe UI",Arial,sans-serif;font-size:11pt;color:#1e293b;background:#fff}'
        '.header{border-bottom:3px solid #1e40af;padding-bottom:10px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:flex-end}'
        '.header h1{font-size:19pt;color:#1e40af;font-weight:700}'
        '.header .sub{font-size:10.5pt;color:#64748b;margin-top:3px}'
        '.header-right{text-align:right;font-size:8.5pt;color:#94a3b8}'
        '.summary{display:flex;gap:14px;margin-bottom:16px}'
        '.sbox{flex:1;border:1.5px solid #e2e8f0;border-radius:8px;padding:9px 12px;text-align:center}'
        '.sbox .val{font-size:24pt;font-weight:800;color:#1e40af}'
        '.sbox .lbl{font-size:8.5pt;color:#64748b;margin-top:2px;text-transform:uppercase;letter-spacing:.5px}'
        '.sbox.soir .val{color:#9a3412}'
        'table{width:100%;border-collapse:collapse;margin-bottom:14px}'
        'th{background:#1e40af;color:#fff;padding:8px 10px;text-align:left;font-size:9.5pt;font-weight:600;text-transform:uppercase;letter-spacing:.4px}'
        'td{padding:7px 10px;border-bottom:1px solid #e2e8f0;vertical-align:middle;font-size:10.5pt}'
        'tr:nth-child(even) td{background:#f8fafc}'
        'tr:last-child td{border-bottom:2px solid #1e40af}'
        '.num{width:32px;text-align:center;color:#94a3b8;font-size:9pt}'
        '.date-cell{width:160px}'
        '.pill{display:inline-block;padding:3px 12px;border-radius:20px;font-weight:700;font-size:9.5pt;letter-spacing:.6px}'
        '.remark-cell{color:#64748b;font-size:9pt;font-style:italic}'
        '.footer{position:fixed;bottom:8mm;left:15mm;right:15mm;font-size:7.5pt;color:#94a3b8;border-top:1px solid #e2e8f0;padding-top:4px;display:flex;justify-content:space-between}'
        '@media print{body{-webkit-print-color-adjust:exact;print-color-adjust:exact}}'
        '</style></head><body>'
        '<div class="header">'
        '  <div>'
        '    <h1>Prestations — ' + month_name + ' ' + str(year) + '</h1>'
        '    <div class="sub">Agent&nbsp;: <strong>' + agent["name"] + '</strong>&nbsp;&nbsp;|&nbsp;&nbsp;Prison de Namur / SPF Justice</div>'
        '  </div>'
        '  <div class="header-right">Généré le ' + today_str + '</div>'
        '</div>'
        '<div class="summary">'
        '  <div class="sbox"><div class="val">' + str(len(worked_days)) + '</div><div class="lbl">Jours prestés</div></div>'
        '  <div class="sbox"><div class="val">' + str(matin_count) + '</div><div class="lbl">Postes Matin</div></div>'
        '  <div class="sbox soir"><div class="val">' + str(soir_count) + '</div><div class="lbl">Postes Soir</div></div>'
        '</div>'
        '<table>'
        '  <thead><tr><th>#</th><th>Date</th><th>Poste</th><th>Remarque</th></tr></thead>'
        '  <tbody>' + rows_html + '</tbody>'
        '</table>'
        '<div class="footer">'
        '  <span>HoraireManager — Prison de Namur / SPF Justice</span>'
        '  <span>' + agent["name"] + '&nbsp;|&nbsp;' + month_name + ' ' + str(year) + '</span>'
        '</div>'
        '<script>window.print();</script>'
        '</body></html>'
    )
    return Response(page_html, mimetype='text/html; charset=utf-8')

# ── FICHE ANNUELLE INTERACTIVE (6 mois en haut + 6 en bas, A4 portrait) ─────
_FICHE_TEAM = {0: 4, 7: 8, 14: 1, 21: 5, 28: 2, 35: 6, 42: 3, 49: 7}

# Postes TRAVAILLÉS (colorés) ; tout le reste (R/36/38/congé) = grisé
def _poste_kind(p):
    """Classe CSS du poste : m/s/w12/w08 = travaillé (coloré), off = grisé."""
    if p == 'M':   return 'm'
    if p == 'S':   return 's'
    if p == '12H': return 'w12'
    if p == '08H': return 'w08'
    return 'off'   # R, 36, 38, congés, 4/5… -> grisé

_FICHE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',Arial,sans-serif;background:#475569;color:#0f172a;padding:14px}
.sheet{background:#fff;width:210mm;min-height:297mm;margin:0 auto;padding:8mm 7mm;
  box-shadow:0 4px 24px rgba(0,0,0,.35);border-radius:4px}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;
  border-bottom:3px solid #1e40af;padding-bottom:6px;margin-bottom:7px}
.hdr h1{font-size:15pt;color:#1e40af;font-weight:800;letter-spacing:.3px}
.hdr .sub{font-size:8pt;color:#64748b;margin-top:2px}
.legend{display:flex;gap:9px;align-items:center;font-size:7.5pt;color:#475569;flex-wrap:wrap;justify-content:flex-end}
.legend .b{display:inline-flex;align-items:center;gap:3px}
.legend .sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.sw.m{background:#fecaca}.sw.s{background:#fed7aa}
.sw.w12{background:#ddd6fe}.sw.w08{background:#99f6e4}.sw.off{background:#cbd5e1}
table{width:100%;border-collapse:collapse;table-layout:fixed;margin-bottom:5mm}
table.b2{margin-bottom:0}
th{background:#1e40af;color:#fff;font-size:8pt;font-weight:700;padding:3px 2px;
  text-transform:uppercase;letter-spacing:.2px;border:1px solid #1e3a8a}
td{border:1px solid #e2e8f0;height:3.7mm;padding:0 3px;vertical-align:middle;
  white-space:nowrap;overflow:hidden}
td.empty{background:#f8fafc}
td.d{font-size:7pt;line-height:1}
/* Largeurs FIXES -> toutes les colonnes alignées (M, S, R, 4/5, 12H, VAC… même gabarit) */
td.d .dn{display:inline-block;width:22px;color:#94a3b8;font-size:6.5pt}
td.d .num{display:inline-block;width:15px;text-align:right;font-weight:700;font-size:7pt}
td.d .pose{display:inline-block;width:24px;text-align:center;font-weight:800;
  border-radius:3px;padding:1px 0;margin:0 3px;font-size:7pt;box-sizing:border-box}
/* POSTES TRAVAILLÉS = colorés */
td.m .pose{background:#fee2e2;color:#b91c1c}
td.s .pose{background:#ffedd5;color:#c2410c}
td.w12 .pose{background:#ede9fe;color:#6d28d9}
td.w08 .pose{background:#ccfbf1;color:#0f766e}
/* TOUT LE RESTE (R/36/38/congé/4-5) = case grisée */
td.off{background:#cbd5e1}
td.off .pose{background:transparent;color:#334155}
td.off .dn,td.off .num{color:#64748b}
td.d .c{vertical-align:middle;cursor:pointer;width:11px;height:11px;accent-color:#1e40af;margin-left:2px}
/* CASE C MANUELLE COCHÉE -> grise cette case */
td.d.on{background:#cbd5e1!important}
td.d.on .pose{background:transparent!important;color:#334155!important}
td.d.on .dn,td.d.on .num{color:#64748b}
.bar{max-width:210mm;margin:0 auto 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar button{background:#1e40af;color:#fff;border:none;border-radius:7px;
  padding:8px 16px;font-size:13px;font-weight:600;cursor:pointer}
.bar button.ghost{background:#fff;color:#1e40af;border:1px solid #1e40af}
.bar .yr{margin-left:auto;font-size:13px;color:#e2e8f0}
@page{size:A4 portrait;margin:8mm}
@media print{
  body{background:#fff;padding:0}
  .bar{display:none}
  .sheet{box-shadow:none;width:auto;min-height:auto;padding:0;border-radius:0}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
"""

_FICHE_JS = """
const KEY='STORAGEKEY';
function tog(cb){cb.closest('td').classList.toggle('on', cb.checked);save();}
function save(){
  const on=[...document.querySelectorAll('input.c:checked')].map(c=>c.dataset.k);
  localStorage.setItem(KEY, JSON.stringify(on));
}
function load(){
  let on=[]; try{on=JSON.parse(localStorage.getItem(KEY)||'[]')}catch(e){}
  const set=new Set(on);
  document.querySelectorAll('input.c').forEach(cb=>{
    if(set.has(cb.dataset.k)){cb.checked=true;cb.closest('td').classList.add('on');}
  });
}
function clearAll(){
  if(!confirm('Décocher toutes les cases C manuelles ?'))return;
  document.querySelectorAll('input.c').forEach(cb=>{cb.checked=false;cb.closest('td').classList.remove('on');});
  save();
}
document.addEventListener('DOMContentLoaded', load);
"""

def _fiche_block(year, months, day_fn, css_cls=''):
    """Un bloc de 6 mois (6 colonnes x 31 lignes). day_fn(d)->(badge,kind,title)."""
    head = "".join(f'<th>{MONTH_NAMES_FR[m-1].capitalize()}</th>' for m in months)
    rows = []
    for day in range(1, 32):
        tds = []
        for m in months:
            try:
                d = date(year, m, day)
            except ValueError:
                tds.append('<td class="empty"></td>')
                continue
            badge, kind, title = day_fn(d)
            dn  = DAY_NAMES_FR[d.weekday()]
            key = f'{m}-{day}'
            t   = f' title="{title}"' if title else ''
            tds.append(
                f'<td class="d {kind}" data-k="{key}"{t}>'
                f'<span class="dn">{dn}</span>'
                f'<span class="num">{day}</span>'
                f'<span class="pose">{badge}</span>'
                f'<input type="checkbox" class="c" data-k="{key}" '
                f'onchange="tog(this)" title="Congé (marquage manuel)">'
                f'</td>'
            )
        rows.append(f'<tr>{"".join(tds)}</tr>')
    return (f'<table class="{css_cls}"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')

def _fiche_page(year, title_html, subtitle, day_fn, storage_key):
    """Page complète : 6 premiers mois en haut, 6 derniers en bas (A4 portrait)."""
    block_top = _fiche_block(year, [1, 2, 3, 4, 5, 6],  day_fn, 'b1')
    block_bot = _fiche_block(year, [7, 8, 9, 10, 11, 12], day_fn, 'b2')
    js = _FICHE_JS.replace("STORAGEKEY", storage_key)
    html = (
        '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title_html} — {year}</title>'
        f'<style>{_FICHE_CSS}</style></head><body>'
        '<div class="bar">'
        '<button onclick="window.print()">🖨️ Imprimer / PDF</button>'
        '<button class="ghost" onclick="clearAll()">Tout décocher</button>'
        f'<span class="yr">{title_html} · {year}</span>'
        '</div>'
        '<div class="sheet">'
        '<div class="hdr">'
        f'<div><h1>Prison de Namur — {year}</h1>'
        f'<div class="sub">{subtitle}</div></div>'
        '<div class="legend">'
        '<span class="b"><span class="sw m"></span>Matin</span>'
        '<span class="b"><span class="sw s"></span>Soir</span>'
        '<span class="b"><span class="sw w12"></span>12H</span>'
        '<span class="b"><span class="sw w08"></span>08H</span>'
        '<span class="b"><span class="sw off"></span>Repos / Congé</span>'
        '</div></div>'
        + block_top + block_bot +
        '</div>'
        f'<script>{js}</script>'
        '</body></html>'
    )
    return Response(html, mimetype='text/html; charset=utf-8')

@app.route("/fiche/<int:offset>/<int:year>")
def fiche_blank(offset, year):
    """Fiche annuelle VIERGE : cycle brut de l'équipe (sans 4/5 ni congés)."""
    if offset not in VALID_OFFSETS:
        return "Offset invalide", 400
    team = _FICHE_TEAM.get(offset, "?")

    def day_fn(d):
        sh = get_shift(d, offset)
        return (sh, _poste_kind(sh), '')

    return _fiche_page(year, f"Équipe {team}",
                       f"Horaire annuel vierge · Équipe {team} · SPF Justice",
                       day_fn, f"fiche_{offset}_{year}")

@app.route("/fiche/agent/<aid>/<int:year>")
def fiche_agent(aid, year):
    """Fiche annuelle PERSONNELLE : horaire réel de l'agent avec ses
    modifications (régime 4/5, congés, surcharges de poste, fériés).
    Tout ce qui n'est pas un poste travaillé (M/S/12H/08H) est grisé."""
    data = load()
    if aid not in data["agents"]:
        return "Agent inconnu", 404
    agent = data["agents"][aid]
    team  = _FICHE_TEAM.get(agent.get("team_offset", 0), "?")

    def day_fn(d):
        info = get_day_info(d, aid, data)
        base = info["base"]
        code = info["code"]
        # Férié/pont : afficher le POSTE RÉEL (travaillé->coloré, repos->grisé).
        # (FERIE/PONT sont aussi des clés du catalogue -> traités AVANT la branche congé)
        if code in ("FERIE", "PONT"):
            return (base, _poste_kind(base), info.get("label") or "Férié")
        if code in LEAVE_CATALOG:                 # congé/absence -> grisé + code (3 car. max, aligné)
            return (code[:3], 'off', info.get("label") or code)
        if code == '4/5':                         # jour 4/5 -> grisé, marqué 4/5
            return ('4/5', 'off', 'Régime 4/5')
        # Postes (M/S/12H/08H travaillés -> colorés ; R/36/38 -> grisés)
        title = info.get("label") or ''
        return (base, _poste_kind(base), title)

    return _fiche_page(year, agent["name"],
                       f"Horaire personnel · {agent['name']} · Équipe {team} · SPF Justice",
                       day_fn, f"fiche_agent_{aid}_{year}")

@app.route("/api/events", methods=["POST"])
def api_add_event():
    data = load()
    body = request.json
    aid  = body["agent_id"]
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 400
    if body["code"] not in LEAVE_CATALOG:
        return jsonify({"error": f"Code inconnu: {body['code']}"}), 400
    ev = {"agent_id": aid, "code": body["code"],
          "label": LEAVE_CATALOG[body["code"]]["label"],
          "category": LEAVE_CATALOG[body["code"]]["category"],
          "date_start": body["date_start"], "date_end": body["date_end"],
          "note": body.get("note", ""), "created": date.today().isoformat()}
    data["events"].append(ev)
    save(data)
    return jsonify({"ok": True})

@app.route("/api/events", methods=["DELETE"])
def api_del_event():
    data = load()
    body = request.json
    before = len(data["events"])
    data["events"] = [e for e in data["events"]
                      if not (e["agent_id"] == body["agent_id"]
                              and e["date_start"] == body["date_start"]
                              and e["code"] == body["code"])]
    save(data)
    return jsonify({"removed": before - len(data["events"])})

@app.route("/api/balance/<aid>/<int:year>")
def api_balance(aid, year):
    data  = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    agent = data["agents"][aid]
    # Age exact au 01/01 de l'annee (sans arrondi — pas d'anticipation d'anniversaire)
    bd_str = agent.get("birth_date")
    if bd_str:
        bd  = date.fromisoformat(bd_str)
        age = year - bd.year - (1 if (1, 1) < (bd.month, bd.day) else 0)
    else:
        age = agent.get("age", 40)
    vac_info  = get_vac_entitlement(age)
    vac_droit = vac_info["days"]
    sick_droit = 21   # allocation annuelle indicative affichage solde carte
    counters   = {}
    hols       = {h[0] for h in get_public_holidays(year)}
    offset     = agent["team_offset"]
    for e in data["events"]:
        if e["agent_id"] != aid:
            continue
        es, ee = date.fromisoformat(e["date_start"]), date.fromisoformat(e["date_end"])
        code   = e["code"]
        days   = 0
        d = es
        while d <= ee:
            if d.year == year:
                base = get_shift(d, offset)
                if base in ("M","S") and d not in hols:
                    days += 1
            d += timedelta(1)
        counters[code] = counters.get(code, 0) + days
    vac_used  = counters.get("VAC",  0)
    sick_used = counters.get("MAL",  0) + counters.get("MAL_LONG", 0)
    return jsonify({
        "agent": agent["name"], "year": year, "age": age,
        "vacances": {"droit": vac_droit,  "utilise": vac_used,  "solde": vac_droit - vac_used},
        "maladie":  {"droit": sick_droit, "utilise": sick_used, "solde": sick_droit - sick_used},
        "detail": counters,
    })

@app.route("/api/entitlements/<aid>/<int:year>")
def api_entitlements(aid, year):
    """Tableau de bord complet des droits legaux BOSA pour l'annee donnee."""
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    agent  = data["agents"][aid]
    offset = agent["team_offset"]

    # Age EXACT au 01/01 de l'annee de reference (sans arrondi — sans anticiper l'anniversaire)
    # L'agent voit l'age qu'il a au debut de l'annee; si son anniversaire change la tranche
    # en cours d'annee, on affiche la date de changement dans le frontend.
    bd_str = agent.get("birth_date")
    if bd_str:
        bd     = date.fromisoformat(bd_str)
        # Age au 1er janvier de l'annee cible (exact, pas d'arrondi)
        age_yr = year - bd.year - (1 if (1, 1) < (bd.month, bd.day) else 0)
        # Birthday this year (info supplementaire pour le frontend)
        try:
            bday_this_year = date(year, bd.month, bd.day).isoformat()
        except ValueError:
            bday_this_year = None  # 29 fevrier annee non-bissextile
    else:
        age_yr = agent.get("age", 40)
        bd     = None
        bday_this_year = None

    # Droits vacances annuels + reliquat
    vac_info = get_vac_entitlement(age_yr)
    reliquat = data.get("reliquats", {}).get(aid, {}).get(str(year), 0)
    # Capitals manuels saisis par l'utilisateur (overrides calcul auto)
    manual_caps = data.get("capitals", {}).get(aid, {}).get(str(year), {})
    vac_droit_manual = manual_caps.get("vacances")   # None = non saisi = 0
    sick_cap_manual  = manual_caps.get("maladie")    # None = non saisi = 0
    # Droit vacances effectif : 0 par défaut, l'utilisateur saisit son quota réel
    vac_droit_eff = vac_droit_manual if vac_droit_manual is not None else 0
    # Detecter si l'anniversaire de cette annee change la tranche vacances
    next_bracket_info = None
    if bd_str and bday_this_year:
        age_after_bday = age_yr + 1
        vac_after = get_vac_entitlement(age_after_bday)
        if vac_after["days"] != vac_info["days"]:
            next_bracket_info = {
                "date":   bday_this_year,
                "age":    age_after_bday,
                "days":   vac_after["days"],
                "delta":  vac_after["days"] - vac_info["days"],
            }

    # Capital maladie (calcule a partir d'aujourd'hui, annees COMPLETES sans arrondi)
    cs_str    = agent.get("career_start")
    sick_info = None
    if cs_str:
        cs = date.fromisoformat(cs_str)
        today = date.today()
        svc_months = (today.year - cs.year) * 12 + (today.month - cs.month)
        sick_info = get_sick_capital(svc_months)

    # Cache jours feries par annee
    _hc: dict = {}
    def hols(yr):
        if yr not in _hc:
            _hc[yr] = {h[0] for h in get_public_holidays(yr)}
        return _hc[yr]

    # Comptage de tous les conges : cette annee + total toutes annees pour MAL
    counters_year  = {}   # code -> jours ouvres pris cette annee
    sick_all_years = 0    # total historique MAL/MAL_LONG pour le capital

    for e in data["events"]:
        if e["agent_id"] != aid:
            continue
        es   = date.fromisoformat(e["date_start"])
        ee   = date.fromisoformat(e["date_end"])
        code = e["code"]
        d = es
        while d <= ee:
            base = get_shift(d, offset)
            if base in ("M", "S") and d not in hols(d.year):
                if d.year == year:
                    counters_year[code] = counters_year.get(code, 0) + 1
                if code in ("MAL", "MAL_LONG"):
                    sick_all_years += 1
            d += timedelta(1)

    vac_used = counters_year.get("VAC", 0)

    # Construire le detail de chaque type de conge
    conges_detail = {}
    for code, info in LEAVE_CATALOG.items():
        used_yr = counters_year.get(code, 0)
        quota   = info.get("days")  # None si variable
        # Inclure si utilise OU si quota fixe (circonstances, specials)
        if used_yr > 0 or (quota is not None):
            conges_detail[code] = {
                "label":    info["label"],
                "category": info["category"],
                "used":     used_yr,
                "quota":    quota,
            }

    result = {
        "agent":              agent["name"],
        "year":               year,
        "age_this_year":      age_yr,
        "birthday_this_year": bday_this_year,
        "next_bracket":       next_bracket_info,
        "birth_date":         bd_str,
        "career_start":       cs_str,
        "vacances": {
            "droit":         vac_droit_eff,
            "droit_auto":    vac_info["days"],    # toujours disponible pour info
            "reliquat":      reliquat,
            "total":         vac_droit_eff + reliquat,
            "utilise":       vac_used,
            "solde":         vac_droit_eff + reliquat - vac_used,
            "epargnable_an": vac_info["annual_save"],
            "manual":        vac_droit_manual is not None,
        },
        "conges_detail": conges_detail,
    }
    # Capital maladie : toujours affiché, 0 par défaut jusqu'à saisie manuelle
    effective_capital = sick_cap_manual if sick_cap_manual is not None else 0
    result["maladie"] = {
        "capital":        effective_capital,
        "utilise":        sick_all_years,
        "solde":          effective_capital - sick_all_years,
        "is_advance":     False,
        "service_months": sick_info["service_months"] if sick_info else None,
        "manual":         sick_cap_manual is not None,
    }
    return jsonify(result)


@app.route("/api/reliquat/<aid>/<int:year>", methods=["GET", "PUT"])
def api_reliquat(aid, year):
    """Reliquat de conges annuels reporte de l'annee precedente."""
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    if request.method == "GET":
        val = data.get("reliquats", {}).get(aid, {}).get(str(year), 0)
        return jsonify({"reliquat": val})
    # PUT
    val = max(0, int(request.json.get("reliquat", 0)))
    if "reliquats" not in data:
        data["reliquats"] = {}
    if aid not in data["reliquats"]:
        data["reliquats"][aid] = {}
    data["reliquats"][aid][str(year)] = val
    save(data)
    return jsonify({"ok": True, "reliquat": val})


@app.route("/api/capitals/<aid>/<int:year>", methods=["GET", "PUT"])
def api_capitals(aid, year):
    """Capitals manuels (vacances droit + maladie) saisies par l'utilisateur."""
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    if request.method == "GET":
        caps = data.get("capitals", {}).get(aid, {}).get(str(year), {})
        return jsonify(caps)
    # PUT — chaque cle est optionnelle; None/vide = supprimer l'override
    body = request.json or {}
    if "capitals" not in data:
        data["capitals"] = {}
    if aid not in data["capitals"]:
        data["capitals"][aid] = {}
    if str(year) not in data["capitals"][aid]:
        data["capitals"][aid][str(year)] = {}
    caps = data["capitals"][aid][str(year)]
    for key in ("maladie", "vacances"):
        if key in body:
            v = body[key]
            if v is None or v == "":
                caps.pop(key, None)
            else:
                caps[key] = max(0, int(v))
    data["capitals"][aid][str(year)] = caps
    save(data)
    return jsonify({"ok": True, "capitals": caps})


# ─────────────────────── ÉCHANGES DE SERVICE ─────────────────
@app.route("/api/exchanges/<aid>")
def api_exchanges_list(aid):
    data = load()
    exs  = sorted(
        [e for e in data.get("exchanges", []) if e["agent_id"] == aid],
        key=lambda x: x.get("date_service",""), reverse=True
    )
    return jsonify(exs)

@app.route("/api/exchanges/<aid>/balance")
def api_exchanges_balance(aid):
    data = load()
    exs  = [e for e in data.get("exchanges", []) if e["agent_id"] == aid and e["status"] == "pending"]
    hier_pending = sum(1 for e in exs if e["type"] == "hierarchie")
    cols: dict = {}
    for e in exs:
        if e["type"] != "collegue":
            continue
        c = (e.get("colleague") or "?").strip() or "?"
        if c not in cols:
            cols[c] = {"donne": 0, "recu": 0}
        if e.get("direction") == "donne":
            cols[c]["donne"] += 1
        else:
            cols[c]["recu"] += 1
    return jsonify({
        "hierarchie_pending": hier_pending,
        "collegues": {
            c: {"donne": v["donne"], "recu": v["recu"], "balance": v["donne"] - v["recu"]}
            for c, v in cols.items()
        },
    })

@app.route("/api/exchanges", methods=["POST"])
def api_exchanges_create():
    data = load()
    body = request.json or {}
    aid  = body.get("agent_id")
    if not aid or aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 400
    dr   = body.get("date_remboursement") or None
    ex   = {
        "id":                  str(_uuid.uuid4())[:8],
        "agent_id":            aid,
        "type":                body.get("type", "collegue"),
        "direction":           body.get("direction", "donne"),
        "date_service":        body.get("date_service", ""),
        "poste_service":       body.get("poste_service", ""),
        "colleague":           body.get("colleague", ""),
        "date_remboursement":  dr,
        "poste_remboursement": body.get("poste_remboursement") or None,
        "note":                body.get("note", ""),
        "status":              "done" if dr else "pending",
        "created":             date.today().isoformat(),
    }
    data.setdefault("exchanges", []).append(ex)
    save(data)
    return jsonify({"ok": True, "id": ex["id"]})

@app.route("/api/exchanges/<eid>", methods=["PATCH"])
def api_exchanges_update(eid):
    data = load()
    for ex in data.get("exchanges", []):
        if ex["id"] == eid:
            body = request.json or {}
            for f in ("date_service", "poste_service", "colleague", "note", "direction"):
                if f in body:
                    ex[f] = body[f]
            for f in ("date_remboursement", "poste_remboursement"):
                if f in body:
                    ex[f] = body[f] or None
            ex["status"] = "done" if ex.get("date_remboursement") else "pending"
            save(data)
            return jsonify({"ok": True, "exchange": ex})
    return jsonify({"error": "Exchange not found"}), 404

@app.route("/api/exchanges/<eid>", methods=["DELETE"])
def api_exchanges_delete(eid):
    data   = load()
    before = len(data.get("exchanges", []))
    data["exchanges"] = [e for e in data.get("exchanges", []) if e["id"] != eid]
    save(data)
    return jsonify({"removed": before - len(data["exchanges"])})

@app.route("/api/ical_url")
def api_ical_url():
    """Renvoie le lien iCal privé (avec jeton) de l'agent connecté."""
    aid  = current_aid()
    data = load()
    ag   = data["agents"].get(aid)
    if not ag:
        return jsonify({"error": "Non connecté"}), 401
    if not ag.get("ical_token"):
        ag["ical_token"] = secrets.token_urlsafe(16)
        save(data)
    url = request.host_url.rstrip("/") + f"/ical/{aid}.ics?token={ag['ical_token']}"
    return jsonify({"url": url})

@app.route("/ical/<aid>.ics")
def export_ical(aid):
    """Flux iCal abonnable par Google Agenda / Apple Calendrier."""
    data = load()
    if aid not in data["agents"]:
        return "Agent inconnu", 404
    agent = data["agents"][aid]
    name  = agent.get("name", aid)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//HoraireManager Prison de Namur//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Horaire {name} – SPF Justice",
        "X-WR-TIMEZONE:Europe/Brussels",
        "X-WR-CALDESC:Horaire rotatif Prison de Namur",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]

    SHIFT_LABELS = {
        "M":  ("🌅 MATIN",  "06:00", "14:00", "OPAQUE"),
        "S":  ("🌆 SOIR",   "14:00", "22:00", "OPAQUE"),
        "R":  ("🛌 REPOS",  None,    None,    "TRANSPARENT"),
        "38": ("📋 38h",    None,    None,    "TRANSPARENT"),
        "36": ("📋 36h",    None,    None,    "TRANSPARENT"),
    }

    # Générer 18 mois : 3 mois passés + 15 mois futurs
    today = date.today()
    start = date(today.year, today.month, 1) - timedelta(days=92)
    end   = date(today.year + 1, today.month, 1) + timedelta(days=31*3)
    cur   = start

    import hashlib
    while cur < end:
        info = get_day_info(cur, aid, data)
        eff  = info["effective"]
        code = info["code"]
        label = info["label"] or ""

        # Titre de l'événement
        if code and code not in ("REPOS-38", "REPOS-R"):
            title = f"🗓 {label}"
            transp = "TRANSPARENT"
            color  = "7"   # cyan = congé/férié
        elif eff in SHIFT_LABELS:
            s = SHIFT_LABELS[eff]
            title = s[0]
            transp = s[3]
            color  = "11" if eff == "M" else ("6" if eff == "S" else "8")
        else:
            cur += timedelta(1)
            continue

        uid = hashlib.md5(f"{aid}-{cur.isoformat()}".encode()).hexdigest()
        dt  = cur.strftime("%Y%m%d")
        now = date.today().strftime("%Y%m%dT%H%M%SZ")

        if eff in ("M", "S") and not code:
            s = SHIFT_LABELS[eff]
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}@horaire-namur",
                f"DTSTAMP:{now}",
                f"DTSTART;TZID=Europe/Brussels:{dt}T{s[1].replace(':','')}00",
                f"DTEND;TZID=Europe/Brussels:{dt}T{s[2].replace(':','')}00",
                f"SUMMARY:{title}",
                f"COLOR:{color}",
                f"TRANSP:{transp}",
                "END:VEVENT",
            ]
        else:
            # Journée entière
            next_dt = (cur + timedelta(1)).strftime("%Y%m%d")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid}@horaire-namur",
                f"DTSTAMP:{now}",
                f"DTSTART;VALUE=DATE:{dt}",
                f"DTEND;VALUE=DATE:{next_dt}",
                f"SUMMARY:{title}",
                f"TRANSP:{transp}",
                "END:VEVENT",
            ]
        cur += timedelta(1)

    lines.append("END:VCALENDAR")
    ical_text = "\r\n".join(lines) + "\r\n"
    return Response(ical_text, mimetype="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{aid}_horaire.ics"'})


@app.route("/api/leaves_catalog")
def api_catalog():
    return jsonify({k: {"label": v["label"], "category": v["category"],
                        "days": v.get("days"), "note": v.get("note","")}
                    for k,v in LEAVE_CATALOG.items()})

@app.route("/api/day/<aid>/<date_str>")
def api_day(aid, date_str):
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    d = date.fromisoformat(date_str)
    info = get_day_info(d, aid, data)
    agent = data["agents"][aid]
    offset = agent["team_offset"]
    delta = (d - ANCHOR).days
    cycle_pos = (delta - offset) % CYCLE_LEN
    mon = d - timedelta(days=d.weekday())
    week = [get_day_info(mon + timedelta(i), aid, data) for i in range(7)]
    shift_hours = {"M": "06:00 – 14:00", "S": "14:00 – 22:00",
                   "12H": "07:00 – 19:00", "08H": "08:00 – 16:00"}.get(info["base"], "Hors service")
    shift_overridden = date_str in data.get("shift_overrides", {}).get(aid, {})
    return jsonify({**info,
        "week": week,
        "cycle_pos": cycle_pos + 1,
        "cycle_len": CYCLE_LEN,
        "shift_hours": shift_hours,
        "shift_overridden": shift_overridden,
        "prev_date": (d - timedelta(1)).isoformat(),
        "next_date": (d + timedelta(1)).isoformat(),
    })

@app.route("/api/stats/<aid>/<int:year>")
def api_stats(aid, year):
    data = load()
    if aid not in data["agents"]:
        return jsonify({"error": "Agent inconnu"}), 404
    agent  = data["agents"][aid]
    offset = agent["team_offset"]
    hols   = {h[0]: h[2] for h in get_public_holidays(year)}
    months_data = []
    for m in range(1, 13):
        cm = cm_s = cr = cf = cl = 0
        for d in range(1, monthrange(year, m)[1]+1):
            info = get_day_info(date(year, m, d), aid, data)
            b = info["base"]
            if info["code"] in ("FERIE","PONT"): cf += 1
            elif info["code"]:                   cl += 1
            elif b == "M":                        cm += 1
            elif b == "S":                        cm_s += 1
            else:                                 cr += 1
        months_data.append({"month": MONTH_NAMES_FR[m-1][:3],
                             "M": cm, "S": cm_s, "R": cr, "F": cf, "C": cl})
    return jsonify(months_data)

# ─────────────────────── HTML TEMPLATE ───────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>HoraireManager — Prison de Namur</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#1e293b">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Horaire">
<link rel="apple-touch-icon" href="/icon.svg">
<style>
:root{
  --red:#ef4444; --orange:#f97316; --green:#22c55e;
  --green-dark:#16a34a; --red-dark:#dc2626; --orange-dark:#ea580c;
  --bg:#0f172a; --sidebar:#1e293b; --card:#1e293b; --card2:#263348;
  --border:#334155; --text:#f1f5f9; --muted:#94a3b8; --accent:#3b82f6;
}
*{box-sizing:border-box;margin:0;padding:0}
html{overflow-x:hidden;max-width:100vw}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;overflow-x:hidden;max-width:100vw}

/* ── SIDEBAR ── */
#sidebar{width:260px;background:var(--sidebar);display:flex;flex-direction:column;border-right:1px solid var(--border);flex-shrink:0}
#sidebar .logo{padding:24px 20px;border-bottom:1px solid var(--border)}
#sidebar .logo h1{font-size:16px;font-weight:700;color:var(--text);letter-spacing:.5px}
#sidebar .logo p{font-size:11px;color:var(--muted);margin-top:3px}
.nav-section{padding:16px 12px 8px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.nav-btn{display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--muted);transition:.15s;border:none;background:none;width:100%;text-align:left}
.nav-btn:hover,.nav-btn.active{background:var(--card2);color:var(--text)}
.nav-btn svg{width:16px;height:16px;opacity:.7}

/* team buttons */
.team-btn{flex:0 0 calc(25% - 5px);padding:7px 4px;border-radius:8px;border:1px solid var(--border);background:var(--card2);color:var(--muted);font-size:12px;font-weight:700;cursor:pointer;text-align:center;transition:.15s}
.team-btn:hover{border-color:var(--accent);color:var(--text)}
.team-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}

/* agent select */
.agent-section{padding:16px;border-top:1px solid var(--border);margin-top:auto}
.agent-section label{font-size:11px;color:var(--muted);display:block;margin-bottom:6px}
select,input{width:100%;padding:8px 10px;background:var(--card2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px;outline:none}
select:focus,input:focus{border-color:var(--accent)}
.btn{padding:8px 16px;border-radius:6px;border:none;font-size:13px;font-weight:600;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:#2563eb}
.btn-danger{background:#7f1d1d;color:#fca5a5}
.btn-danger:hover{background:#991b1b}
.btn-green{background:#14532d;color:#86efac}
.btn-green:hover{background:#166534}
.btn-sm{padding:5px 10px;font-size:11px}

/* ── MAIN ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#topbar{padding:16px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--sidebar)}
#topbar h2{font-size:18px;font-weight:700}
.month-nav{display:flex;align-items:center;gap:12px}
.month-nav button{background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
.month-nav button:hover{background:var(--border)}
.month-nav span{font-size:15px;font-weight:600;min-width:160px;text-align:center}

#content{flex:1;overflow-y:auto;padding:24px;display:grid;grid-template-columns:1fr 280px;gap:20px}

/* ── CALENDAR ── */
#calendar-wrap{display:flex;flex-direction:column;gap:14px}
.legend{display:flex;gap:20px;flex-wrap:wrap;padding:10px 14px;background:var(--card);border-radius:10px;border:1px solid var(--border)}
.legend-item{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--text)}
.legend-bar{width:4px;height:20px;border-radius:2px}
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.cal-header{text-align:center;font-size:11px;font-weight:700;color:var(--muted);padding:8px 0;letter-spacing:.5px;text-transform:uppercase}

/* cellule jour */
.cal-day{
  min-height:82px;background:var(--card);border-radius:8px;
  padding:8px 8px 8px 13px;cursor:pointer;position:relative;
  border:1px solid var(--border);border-left-width:4px;
  display:flex;flex-direction:column;gap:2px;
  transition:background .12s, box-shadow .12s;
}
.cal-day:hover{background:var(--card2);box-shadow:0 2px 12px rgba(0,0,0,.3)}
.cal-day.empty{background:transparent;border:none;cursor:default}
.cal-day.today{box-shadow:0 0 0 2px #f8fafc!important}
.cal-day.weekend{opacity:.85}

/* contenu cellule */
.day-top{display:flex;justify-content:space-between;align-items:center}
.day-abbr{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.shift-pill{font-size:10px;font-weight:800;padding:2px 8px;border-radius:20px;letter-spacing:.3px;white-space:nowrap}
.day-num{font-size:30px;font-weight:900;line-height:1;color:var(--text);margin-top:2px}
.cal-day.today .day-num{
  display:inline-flex;align-items:center;justify-content:center;
  width:38px;height:38px;border-radius:50%;
  background:rgba(248,250,252,.18);color:#fff;
}
.day-reason{font-size:11px;font-weight:600;margin-top:auto;line-height:1.3;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;opacity:.85;}
.day-remark{font-size:9px;color:var(--accent);opacity:.9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%;margin-top:1px}
.badge-decale{display:inline-block;font-size:8px;font-weight:800;background:#374151;color:#fbbf24;border-radius:4px;padding:1px 4px;margin-top:2px;letter-spacing:.3px}
.remark-dot{display:inline-block;width:5px;height:5px;border-radius:50%;background:var(--accent);margin-right:2px;vertical-align:middle;flex-shrink:0}
.n-red    .day-reason{color:#f87171}
.n-orange .day-reason{color:#fb923c}
.n-green  .day-reason{color:#4ade80}
.n-blue   .day-reason{color:#93c5fd}
.n-purple .day-reason{color:#c084fc}

/* ── COULEURS PAR TYPE ── */
/* Bordures gauches */
.b-red   {border-left-color:#f87171}
.b-orange{border-left-color:#fb923c}
.b-green {border-left-color:#4ade80}
.b-blue  {border-left-color:#60a5fa}
.b-purple{border-left-color:#c084fc}
.b-violet{border-left-color:#a78bfa}
.b-teal  {border-left-color:#2dd4bf}

/* Fonds tintés des cellules */
.c-red   {background:rgba(239,68,68,.14)!important}
.c-orange{background:rgba(249,115,22,.13)!important}
.c-green {background:rgba(34,197,94,.10)!important}
.c-blue  {background:rgba(59,130,246,.14)!important}
.c-purple{background:rgba(168,85,247,.12)!important}
.c-violet{background:rgba(139,92,246,.16)!important}
.c-teal  {background:rgba(20,184,166,.16)!important}

/* Pills plus opaques */
.p-red   {background:rgba(239,68,68,.35);color:#fca5a5;font-weight:900}
.p-orange{background:rgba(249,115,22,.35);color:#fdba74;font-weight:900}
.p-green {background:rgba(34,197,94,.30);color:#86efac;font-weight:900}
.p-blue  {background:rgba(59,130,246,.35);color:#93c5fd;font-weight:900}
.p-purple{background:rgba(168,85,247,.30);color:#d8b4fe;font-weight:900}
.p-violet{background:rgba(139,92,246,.35);color:#c4b5fd;font-weight:900}
.p-teal  {background:rgba(20,184,166,.35);color:#5eead4;font-weight:900}

/* Numéros colorés par type */
.n-red   .day-num{color:#f87171}
.n-orange .day-num{color:#fb923c}
.n-green  .day-num{color:#4ade80}
.n-blue   .day-num{color:#93c5fd}
.n-purple .day-num{color:#c084fc}
.n-violet .day-num{color:#a78bfa}
.n-teal   .day-num{color:#2dd4bf}
/* today override : cercle blanc brillant */
.cal-day.today .day-num{color:#fff!important}

/* ── RIGHT PANEL ── */
#right-panel{display:flex;flex-direction:column;gap:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px}
.card h3{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.balance-row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)}
.balance-row:last-child{border:none}
.balance-label{font-size:12px;color:var(--muted)}
.balance-value{font-size:14px;font-weight:700}
.balance-bar{height:4px;border-radius:2px;background:var(--border);margin-top:4px;overflow:hidden}
.balance-fill{height:100%;border-radius:2px;background:var(--green)}
.balance-fill.warn{background:var(--orange)}
.balance-fill.danger{background:var(--red)}

/* ── MODAL ── */
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;display:none;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal{background:var(--sidebar);border:1px solid var(--border);border-radius:16px;padding:24px;width:420px;max-width:95vw;box-shadow:0 25px 60px rgba(0,0,0,.5)}
.modal h2{font-size:16px;margin-bottom:20px;display:flex;align-items:center;justify-content:space-between}
.modal h2 .close{cursor:pointer;opacity:.5;font-size:20px;line-height:1}
.modal h2 .close:hover{opacity:1}
.form-group{margin-bottom:14px}
.form-group label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-note{font-size:11px;color:var(--muted);margin-top:4px;padding:6px 10px;background:var(--bg);border-radius:4px}
.modal-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:20px}

/* ── AGENT MODAL ── */
#agent-modal .modal{width:360px}

/* ── STATS ── */
.stat-bar{display:flex;height:20px;border-radius:6px;overflow:hidden;gap:1px;margin-top:8px}
.stat-bar div{transition:.3s}

/* ── TOAST ── */
#toast{position:fixed;bottom:24px;right:24px;background:var(--green-dark);color:#fff;padding:12px 20px;border-radius:10px;font-size:13px;z-index:999;transform:translateY(80px);opacity:0;transition:.3s}
#toast.show{transform:translateY(0);opacity:1}
#toast.error{background:#7f1d1d}

/* ── 4/5 BUTTONS ── */
.btn-45{
  padding:8px 4px;border-radius:8px;border:2px solid var(--border);
  background:var(--card2);color:var(--muted);font-size:12px;font-weight:700;
  cursor:pointer;transition:.15s;text-align:center;
}
.btn-45:hover{border-color:var(--green);color:var(--green)}
.btn-45.active{
  background:rgba(34,197,94,.20);border-color:#4ade80;
  color:#4ade80;box-shadow:0 0 0 1px #4ade80;
}

/* ── DAY MODAL WIDE ── */
#day-modal .modal{width:680px;max-width:96vw;padding:0;overflow:hidden;border-radius:16px}
.dm-nav{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;border-bottom:1px solid var(--border)}
.dm-nav-arrow{background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 14px;cursor:pointer;font-size:15px;line-height:1;transition:.12s}
.dm-nav-arrow:hover{background:var(--border)}
.dm-date-title{text-align:center}
.dm-date-title .dm-weekday{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.dm-date-title .dm-main-date{font-size:22px;font-weight:800;line-height:1.1}
.dm-body{padding:20px}
.dm-top{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}
.dm-shift-block{border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:6px}
.dm-shift-code{font-size:36px;font-weight:900;letter-spacing:-1px;line-height:1}
.dm-shift-label{font-size:13px;font-weight:600}
.dm-shift-hours{font-size:12px;opacity:.75;font-family:monospace;margin-top:2px}
.dm-shift-cycle{font-size:11px;opacity:.6;margin-top:4px}
.dm-week-strip{border:1px solid var(--border);border-radius:12px;padding:12px 14px}
.dm-week-strip .dm-ws-title{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px;font-weight:700}
.dm-week-cells{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.dm-wc{border-radius:6px;padding:5px 2px;text-align:center;cursor:pointer;transition:.12s;border:1px solid transparent}
.dm-wc:hover{border-color:var(--muted)}
.dm-wc.dm-wc-active{border-color:#f8fafc!important;box-shadow:0 0 0 1px #f8fafc}
.dm-wc .dm-wc-d{font-size:9px;color:var(--muted);font-weight:700;text-transform:uppercase}
.dm-wc .dm-wc-n{font-size:15px;font-weight:800;line-height:1.2}
.dm-wc .dm-wc-s{font-size:8px;font-weight:700;margin-top:2px;opacity:.8}
.dm-events-section{margin-top:4px}
.dm-events-section .dm-sec-title{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;font-weight:700;margin-bottom:8px}
.dm-event-row{display:flex;justify-content:space-between;align-items:center;padding:10px 12px;background:var(--bg);border-radius:8px;margin-bottom:6px;border:1px solid var(--border)}
.dm-event-row .dm-ev-label{font-size:13px;font-weight:600}
.dm-event-row .dm-ev-meta{font-size:11px;color:var(--muted);margin-top:2px}
.dm-footer{padding:16px 20px;border-top:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}

/* scrollbar */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* ══════════════════════════════════════════
   RESPONSIVE — TABLETTE PAYSAGE  (≤ 1024px)
══════════════════════════════════════════ */
@media(max-width:1024px){
  #sidebar{width:72px}
  #sidebar .logo p,.nav-section,.agent-section{display:none}
  #sidebar .logo{padding:16px;text-align:center}
  #sidebar .logo h1{font-size:22px}
  .nav-btn{padding:12px 0;justify-content:center;font-size:0;gap:0}
  .nav-btn svg{width:22px;height:22px;opacity:1}
  #mobile-agent-bar{display:flex!important}
  #mobile-team-bar{display:flex!important}
  #team-btns{display:none!important}
  #content{padding:14px;gap:12px}
}

/* ══════════════════════════════════════════
   RESPONSIVE — TABLETTE PORTRAIT  (≤ 768px)
══════════════════════════════════════════ */
@media(max-width:768px){
  /* Cellules légèrement plus compactes */
  .cal-day{min-height:72px;padding:6px 6px 6px 10px}
  .day-num{font-size:24px}
  .shift-pill{font-size:9px;padding:2px 6px}
  .day-abbr{font-size:9px}
  .day-reason{font-size:10px}
  /* Topbar */
  #topbar h2{font-size:15px}
  .month-nav span{font-size:14px}
  /* Légende : 2 colonnes sur tablette portrait */
  .legend{display:grid;grid-template-columns:1fr 1fr;gap:6px}
  /* Échanges : 2 colonnes */
  .exch-balance-grid{grid-template-columns:repeat(2,1fr)}
  .exr-note{display:none}
  /* Formulaires modals → 1 colonne */
  .form-row{grid-template-columns:1fr!important}
  /* Annual : 1 colonne */
  #annual-content{padding:14px}
  #annual-grid{grid-template-columns:1fr!important}
}

/* ══════════════════════════════════════════
   RESPONSIVE — SMARTPHONE  (≤ 640px)
══════════════════════════════════════════ */
@media(max-width:640px){
  body{flex-direction:column}

  /* Sidebar → barre de navigation en bas */
  #sidebar{
    width:100%;height:60px;flex-direction:row;
    position:fixed;bottom:0;left:0;right:0;z-index:60;
    border-right:none;border-top:1px solid var(--border);
    padding-bottom:env(safe-area-inset-bottom,0);
  }
  #sidebar .logo,#sidebar .agent-section,.nav-section{display:none}
  .nav-btn{flex:1;flex-direction:column;justify-content:center;align-items:center;
    padding:6px 0;font-size:9px;font-weight:700;gap:3px;border-radius:0;color:var(--muted)}
  .nav-btn svg{width:20px;height:20px;opacity:1}
  .nav-btn.active{color:var(--text);background:rgba(255,255,255,.05)}
  #btn-panel-mobile{display:flex!important}

  /* Zone principale */
  #main{padding-bottom:calc(60px + env(safe-area-inset-bottom,0));overflow:hidden}

  /* Topbar compact — 1 seule ligne */
  #topbar{padding:4px 8px;gap:4px;flex-direction:row;align-items:center;flex-wrap:wrap}
  #topbar h2{font-size:12px;font-weight:800;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .month-nav{justify-content:flex-end;gap:3px;flex:2}
  .month-nav button{padding:4px 8px;font-size:12px;min-width:30px;border-radius:7px}
  .month-nav span{font-size:12px;min-width:80px;text-align:center}
  #btn-week-toggle{padding:4px 8px;font-size:11px}

  /* Barre agent + équipe (mobile) */
  #mobile-agent-bar{display:flex!important}
  #mobile-team-bar{display:flex!important}
  #team-btns{display:none!important}

  /* Grille calendrier — forcée dans la largeur du GSM */
  #content{grid-template-columns:1fr;padding:2px;gap:4px;width:100%;overflow:hidden}
  #right-panel{display:none}
  #right-panel.panel-open{display:flex!important;flex-direction:column}
  #calendar-wrap{width:100%;overflow:hidden}
  .cal-grid{gap:1px;width:100%}
  .cal-header{font-size:8px;padding:4px 0;font-weight:800;letter-spacing:0;overflow:hidden}
  .cal-day{min-height:58px;padding:3px 2px 3px 5px;gap:1px;overflow:hidden;min-width:0}
  .cal-day.today .day-num{width:24px;height:24px;font-size:13px}
  .day-num{font-size:15px;font-weight:900}
  .day-abbr{display:none}
  .day-top{justify-content:flex-end;overflow:hidden}
  .shift-pill{font-size:7px;padding:1px 3px;letter-spacing:0;white-space:nowrap;max-width:100%;overflow:hidden}
  .day-reason{font-size:7px;-webkit-line-clamp:1;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .day-remark{display:none}
  .badge-decale{font-size:6px;padding:1px 2px}

  /* Entitlements scrollable */
  #entitlements-bar{padding:6px 8px}
  #btn-panel-mobile{font-size:10px;padding:6px 8px}

  /* Pill du jour → toujours en haut à droite (abbr masqué) */
  .day-top{justify-content:flex-end}

  /* Légende → scroll horizontal compact */
  .legend{overflow-x:auto;flex-wrap:nowrap;gap:8px;padding:5px 8px;
    -webkit-overflow-scrolling:touch;scrollbar-width:none}
  .legend::-webkit-scrollbar{display:none}
  .legend-item{flex-shrink:0;font-size:10px;gap:4px}

  /* Bouton Imprimer inutile sur mobile */
  #btn-print{display:none!important}

  /* Toast — au-dessus de la bottom nav */
  #toast{bottom:72px;right:12px;left:12px;text-align:center}

  /* Vue annuelle — padding réduit */
  #annual-content{padding:10px 8px}
  #annual-grid{grid-template-columns:1fr!important}

  /* Bannière base-planning → empilée */
  #base-banner{flex-direction:column;gap:6px;text-align:center;font-size:12px}

  /* Formulaires dans modals → 1 colonne */
  .form-row{grid-template-columns:1fr!important}

  /* Modals → tiroir du bas */
  .modal-overlay{align-items:flex-end;padding:0}
  .modal{
    border-radius:20px 20px 0 0!important;
    width:100%!important;max-width:100%!important;
    max-height:90dvh;overflow-y:auto;
    padding-bottom:env(safe-area-inset-bottom,12px);
  }
  #day-modal .modal{border-radius:20px 20px 0 0!important}
  .dm-top{grid-template-columns:1fr}
  .dm-nav{padding:12px 16px}
  .dm-main-date{font-size:18px}
  .dm-body{padding:14px}
}

/* entitlements bar — grille 2 colonnes sur mobile */
@media(max-width:640px){
  #entitlements-content{grid-template-columns:1fr 1fr!important}
  #ent-details .ent-grid{grid-template-columns:1fr!important}
}

/* ── EXCHANGES VIEW ── */
#exchanges-content{flex:1;padding:24px;overflow-y:auto;display:none}
.exch-balance-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-bottom:20px}
.exch-balance-card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.exch-balance-card .ebc-title{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin-bottom:6px}
.exch-balance-card .ebc-num{font-size:30px;font-weight:900;line-height:1}
.exch-balance-card .ebc-sub{font-size:11px;color:var(--muted);margin-top:4px}
.exch-filter{display:flex;gap:6px;margin-bottom:14px}
.exch-filter button{padding:5px 14px;border-radius:20px;border:1px solid var(--border);background:var(--card2);color:var(--muted);font-size:12px;cursor:pointer;font-weight:600}
.exch-filter button.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.exch-list{display:flex;flex-direction:column;gap:8px}
.exch-row{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.exch-row:hover{background:var(--card2)}
.exr-date{font-size:12px;font-weight:700;min-width:90px;color:var(--muted)}
.exr-type{font-size:10px;padding:3px 8px;border-radius:20px;font-weight:700;white-space:nowrap}
.exr-type.hier{background:rgba(168,85,247,.2);color:#d8b4fe}
.exr-type.col-donne{background:rgba(34,197,94,.15);color:#86efac}
.exr-type.col-recu{background:rgba(249,115,22,.15);color:#fdba74}
.exr-who{flex:1;font-size:13px;font-weight:600;min-width:120px}
.exr-poste{font-size:11px;color:var(--muted);white-space:nowrap}
.exr-note{font-size:11px;color:var(--muted);font-style:italic;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.exr-status{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;white-space:nowrap}
.exr-status.pending{background:rgba(249,115,22,.2);color:#fdba74}
.exr-status.done{background:rgba(34,197,94,.15);color:#86efac}
.exr-actions{display:flex;gap:6px;margin-left:auto}
@media(max-width:640px){
  #exchanges-content{padding:12px}
  .exch-row{gap:8px}
  .exr-note{display:none}
}

/* ══════ Très petits écrans (≤ 380px) ══════ */
@media(max-width:380px){
  .cal-day{min-height:50px;padding:3px 2px 3px 5px}
  .day-num{font-size:15px}
  .shift-pill{font-size:7px;padding:1px 3px}
  .cal-header{font-size:8px}
  /* Sur très petit écran, masquer le texte raison — couleur + pill suffisent */
  .day-reason{display:none}
  .day-remark{display:none}
}
/* Textarea remarque dans modal jour */
#dm-remark-area{width:100%;padding:10px;background:var(--card2);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;resize:none;min-height:70px;font-family:inherit;outline:none;margin-top:6px}
#dm-remark-area:focus{border-color:var(--accent)}
.dm-shift-override{padding:12px 16px;border-top:1px solid var(--border)}
.dm-shift-override .dm-sec-label{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px}
.shift-btn-group{display:flex;gap:8px;flex-wrap:wrap}
.shift-btn{flex:1;min-width:80px;padding:8px 4px;border:2px solid var(--border);border-radius:8px;background:var(--card2);color:var(--text);cursor:pointer;font-weight:700;font-size:13px;transition:.15s;text-align:center}
.shift-btn:hover{border-color:var(--accent)}
.shift-btn.sb-matin{border-color:#ef4444;color:#fca5a5}
.shift-btn.sb-soir{border-color:#f97316;color:#fdba74}
.shift-btn.sb-h12{border-color:#8b5cf6;color:#c4b5fd}
.shift-btn.sb-h08{border-color:#14b8a6;color:#5eead4}
.shift-btn.sb-repos{border-color:#22c55e;color:#86efac}
.shift-btn.sb-reset{border-color:var(--muted);color:var(--muted);font-size:11px}
.shift-btn.sb-active{opacity:1;font-size:14px}
.shift-btn:not(.sb-active){opacity:.55}
.dm-remark-section{padding:12px 16px;border-top:1px solid var(--border)}
.dm-remark-label{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between}
.dm-remark-save{font-size:11px;padding:4px 12px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:600}
.dm-remark-save:hover{background:#2563eb}

/* ══════════════════════════════════════════
   VUE SEMAINE MOBILE
══════════════════════════════════════════ */
#week-view{display:none;flex-direction:column;gap:8px;padding:10px 10px 84px;overflow-y:auto;flex:1}
#week-view.wv-active{display:flex}

/* Barre soldes en haut de la vue semaine */
#wv-balance{display:flex;gap:0;background:var(--card);border-radius:12px;border:1px solid var(--border);overflow:hidden;flex-shrink:0;margin-bottom:2px}
.wvb-item{display:flex;flex-direction:column;align-items:center;justify-content:center;flex:1;padding:10px 6px;gap:1px;border-right:1px solid var(--border)}
.wvb-item:last-child{border-right:none}
.wvb-lbl{font-size:9px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.wvb-val{font-size:22px;font-weight:900;line-height:1.1}
.wvb-sub{font-size:9px;color:var(--muted)}
.wvb-action{background:none;border:none;font-size:9px;color:var(--accent);cursor:pointer;font-weight:700;margin-top:2px;text-decoration:underline}

/* Carte de jour */
.week-card{
  display:flex;align-items:stretch;border-radius:14px;
  border:1px solid var(--border);border-left-width:5px;
  min-height:82px;cursor:pointer;overflow:hidden;
  transition:.12s;background:var(--card);
  -webkit-tap-highlight-color:transparent;user-select:none
}
.week-card:active{opacity:.78;transform:scale(.988)}
.wc-today{outline:2.5px solid var(--accent);outline-offset:0px}

/* Colonne date */
.wc-date{
  width:62px;min-width:62px;text-align:center;
  padding:10px 4px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:1px;
  border-right:1px solid rgba(255,255,255,.07)
}
.wc-dow{font-size:9px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:.6px}
.wc-num{font-size:28px;font-weight:900;line-height:1.1;color:var(--text)}
.wc-num.wc-num-today{
  background:var(--accent);color:#fff;
  width:36px;height:36px;border-radius:50%;
  display:inline-flex;align-items:center;justify-content:center;
  font-size:18px;line-height:1
}
.wc-month{font-size:9px;color:var(--muted);margin-top:1px}

/* Colonne poste */
.wc-shift-col{
  width:86px;min-width:86px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:3px;
  padding:8px 6px;border-right:1px solid rgba(255,255,255,.07)
}
.wc-shift{font-size:14px;font-weight:900;letter-spacing:.2px;text-align:center;padding:4px 8px;border-radius:8px}
.wc-hours{font-size:9px;color:var(--muted);font-weight:600;text-align:center;line-height:1.2}

/* Colonne info */
.wc-info{
  flex:1;padding:10px 8px 10px 10px;
  display:flex;flex-direction:column;justify-content:center;
  gap:4px;overflow:hidden;min-width:0
}
/* Reason : jusqu'à 2 lignes */
.wc-reason{
  font-size:12px;color:var(--text);font-weight:600;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden;white-space:normal;line-height:1.35
}
/* Remark visible sur 1 ligne */
.wc-remark{
  display:flex;align-items:flex-start;gap:5px;
  font-size:11px;color:var(--accent);font-style:italic;
  overflow:hidden;white-space:normal;
  display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical
}
.wc-chevron{font-size:18px;color:var(--muted);padding-right:8px;display:flex;align-items:center;opacity:.4;flex-shrink:0}

.wv-empty{text-align:center;color:var(--muted);padding:40px 20px;font-size:14px;line-height:1.5}

/* Toggle semaine/mois — visible seulement sur mobile */
#btn-week-toggle{
  display:none;padding:5px 10px;border-radius:8px;
  border:1px solid var(--border);background:var(--card2);
  color:var(--text);font-size:12px;font-weight:700;
  cursor:pointer;white-space:nowrap;align-items:center;gap:5px
}
@media(max-width:640px){
  #btn-week-toggle{display:inline-flex}
}
</style>
</head>
<body>

<!-- AUTH OVERLAY (connexion / création de compte par agent) -->
<div id="login-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:9999;align-items:center;justify-content:center;flex-direction:column">
  <div style="background:#1e293b;border:1px solid #334155;border-radius:16px;padding:28px 30px;max-width:380px;width:92%;text-align:center;box-sizing:border-box">
    <div style="font-size:2rem;margin-bottom:4px">🏛</div>
    <h2 style="color:#f1f5f9;margin:0 0 2px">HoraireManager</h2>
    <p style="color:#94a3b8;font-size:.78rem;margin:0 0 18px">Prison de Namur — SPF Justice</p>
    <div style="display:flex;gap:6px;margin-bottom:16px">
      <button id="tab-login" onclick="authMode('login')"
        style="flex:1;padding:9px;border-radius:8px;border:none;cursor:pointer;font-weight:700;background:#3b82f6;color:#fff">Connexion</button>
      <button id="tab-register" onclick="authMode('register')"
        style="flex:1;padding:9px;border-radius:8px;border:1px solid #475569;cursor:pointer;font-weight:700;background:transparent;color:#94a3b8">Créer un compte</button>
    </div>
    <input id="auth-id" placeholder="Identifiant (ex: TSE)" maxlength="64" autocomplete="off"
      style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#f1f5f9;font-size:1rem;box-sizing:border-box;margin-bottom:10px"
      onkeydown="if(event.key==='Enter')doAuth()">
    <input id="auth-pin" type="password" inputmode="numeric" placeholder="Code PIN" maxlength="20"
      style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#f1f5f9;font-size:1rem;text-align:center;box-sizing:border-box;margin-bottom:10px"
      onkeydown="if(event.key==='Enter')doAuth()">
    <div id="register-fields" style="display:none">
      <input id="auth-name" placeholder="Prénom NOM" maxlength="60"
        style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#f1f5f9;font-size:1rem;box-sizing:border-box;margin-bottom:10px">
      <select id="auth-offset"
        style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#f1f5f9;font-size:1rem;box-sizing:border-box;margin-bottom:10px">
        <option value="14">Équipe 1</option><option value="28">Équipe 2</option>
        <option value="42">Équipe 3</option><option value="0">Équipe 4</option>
        <option value="21">Équipe 5</option><option value="35">Équipe 6</option>
        <option value="49">Équipe 7</option><option value="7">Équipe 8</option>
      </select>
      <select id="auth-45"
        style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#f1f5f9;font-size:1rem;box-sizing:border-box;margin-bottom:10px">
        <option value="">— Pas de régime 4/5 —</option>
        <option value="0">4/5 : Lundi</option><option value="1">4/5 : Mardi</option>
        <option value="2">4/5 : Mercredi</option><option value="3">4/5 : Jeudi</option>
        <option value="4">4/5 : Vendredi</option>
      </select>
      <input id="auth-birth" type="date" max="2007-12-31"
        style="width:100%;padding:11px;border-radius:8px;border:1px solid #475569;background:#0f172a;color:#94a3b8;font-size:.9rem;box-sizing:border-box;margin-bottom:10px">
      <div style="color:#64748b;font-size:.72rem;text-align:left;margin:-4px 0 10px">Date de naissance (calcul des congés selon l'âge) — optionnel</div>
    </div>
    <button id="auth-submit" onclick="doAuth()"
      style="width:100%;padding:12px;border-radius:8px;background:#3b82f6;color:#fff;font-weight:700;font-size:1rem;border:none;cursor:pointer">
      Connexion
    </button>
    <p id="auth-error" style="color:#ef4444;font-size:.82rem;margin:10px 0 0;display:none"></p>
  </div>
</div>

<!-- SIDEBAR -->
<div id="sidebar">
  <div class="logo">
    <h1>🏛 HoraireManager</h1>
    <p>Prison de Namur — SPF Justice</p>
  </div>
  <div class="nav-section">Navigation</div>
  <button class="nav-btn active" onclick="showView('calendar')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
    Calendrier
  </button>
  <button class="nav-btn" onclick="showView('annuel')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>
    Annuel
  </button>
  <button class="nav-btn" onclick="showView('exchanges')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 16V4m0 0L3 8m4-4l4 4M17 8v12m0 0l4-4m-4 4l-4-4"/></svg>
    Échanges
  </button>
  <button class="nav-btn" onclick="openAgentModal()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
    Mon compte
  </button>
  <button class="nav-btn" id="btn-panel-mobile" style="display:none" onclick="toggleMobilePanel()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 010 14.14M4.93 19.07a10 10 0 010-14.14"/><path d="M15.54 8.46a5 5 0 010 7.07M8.46 15.54a5 5 0 010-7.07"/></svg>
    Réglages
  </button>

  <div class="nav-section" id="team-section-label">Équipe</div>
  <div id="team-btns" style="display:flex;flex-wrap:wrap;gap:6px;padding:0 12px 12px">
    <button class="team-btn" data-offset="14" onclick="selectTeam(14)">Éq 1</button>
    <button class="team-btn" data-offset="28" onclick="selectTeam(28)">Éq 2</button>
    <button class="team-btn" data-offset="42" onclick="selectTeam(42)">Éq 3</button>
    <button class="team-btn" data-offset="0"  onclick="selectTeam(0)">Éq 4</button>
    <button class="team-btn" data-offset="21" onclick="selectTeam(21)">Éq 5</button>
    <button class="team-btn" data-offset="35" onclick="selectTeam(35)">Éq 6</button>
    <button class="team-btn" data-offset="49" onclick="selectTeam(49)">Éq 7</button>
    <button class="team-btn" data-offset="7"  onclick="selectTeam(7)">Éq 8</button>
  </div>

  <div class="agent-section">
    <label>Agent actif</label>
    <select id="agent-select" onchange="onAgentChange()">
      <option value="">— Sélectionner —</option>
    </select>
    <div style="margin-top:8px;font-size:11px;color:var(--muted)" id="agent-info"></div>
  </div>
</div>

<!-- MAIN -->
<div id="main">
  <div id="topbar">
    <div style="display:flex;align-items:center;gap:16px">
      <h2 id="view-title">Calendrier mensuel</h2>
    </div>
    <div class="month-nav">
      <button onclick="prevPeriod()">◀</button>
      <span id="period-label"></span>
      <button onclick="nextPeriod()">▶</button>
      <button onclick="gotoToday()" style="margin-left:4px">Auj.</button>
      <button id="btn-week-toggle" onclick="toggleWeekMode()">📋 Semaine</button>
      <button id="btn-print" onclick="printMonth()" title="Imprimer les jours prestés (A4 PDF)" style="margin-left:8px;background:#1e40af;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">🖨️ Imprimer</button>
      <button id="btn-fiche-me" onclick="openFicheAgent()" title="Fiche annuelle de l'agent (avec ses congés, 4/5, modifications)" style="margin-left:4px;background:#0f766e;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">📄 Ma fiche</button>
      <button id="btn-fiche-blank" onclick="openFicheBlank()" title="Fiche annuelle vierge de l'équipe (cycle brut)" style="margin-left:4px;background:#64748b;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">📄 Vierge</button>
    </div>
  </div>
  <!-- Barre agent — visible seulement sur tablette/mobile -->
  <div id="mobile-agent-bar" style="display:none;padding:8px 14px;border-bottom:1px solid var(--border);background:var(--sidebar);gap:10px;align-items:center">
    <span style="font-size:11px;color:var(--muted);white-space:nowrap">Agent :</span>
    <select id="agent-select-mobile" onchange="onAgentChangeMobile()" style="flex:1;padding:6px 8px;background:var(--card2);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:13px">
      <option value="">— Sélectionner —</option>
    </select>
    <div style="font-size:11px;color:var(--muted)" id="agent-info-mobile"></div>
  </div>
  <!-- Barre équipe mobile — toujours visible sur mobile -->
  <div id="mobile-team-bar" style="display:none;padding:8px 10px;border-bottom:1px solid var(--border);background:var(--sidebar);gap:5px;flex-wrap:wrap">
    <button class="team-btn" data-offset="14" onclick="selectTeam(14)">Éq 1</button>
    <button class="team-btn" data-offset="28" onclick="selectTeam(28)">Éq 2</button>
    <button class="team-btn" data-offset="42" onclick="selectTeam(42)">Éq 3</button>
    <button class="team-btn" data-offset="0"  onclick="selectTeam(0)">Éq 4</button>
    <button class="team-btn" data-offset="21" onclick="selectTeam(21)">Éq 5</button>
    <button class="team-btn" data-offset="35" onclick="selectTeam(35)">Éq 6</button>
    <button class="team-btn" data-offset="49" onclick="selectTeam(49)">Éq 7</button>
    <button class="team-btn" data-offset="7"  onclick="selectTeam(7)">Éq 8</button>
  </div>

  <!-- CALENDAR VIEW -->
  <div id="content" style="display:grid">
    <div id="calendar-wrap">
      <div class="legend">
        <div class="legend-item"><div class="legend-bar" style="background:#ef4444"></div><span style="color:#fca5a5;font-weight:700">M</span> Matin</div>
        <div class="legend-item"><div class="legend-bar" style="background:#f97316"></div><span style="color:#fdba74;font-weight:700">S</span> Soir</div>
        <div class="legend-item"><div class="legend-bar" style="background:#22c55e"></div><span style="color:#86efac;font-weight:700">R</span> Repos / Congé</div>
        <div class="legend-item"><div class="legend-bar" style="background:#3b82f6"></div><span style="color:#93c5fd;font-weight:700">⭐</span> Jour férié</div>
        <div class="legend-item"><div style="width:18px;height:18px;border-radius:4px;border:2px solid #f8fafc"></div> Aujourd'hui</div>
      </div>
      <!-- TABLEAU DE BORD DROITS LEGAUX -->
      <div id="entitlements-bar" style="display:none;padding:14px 16px;background:var(--card);border-radius:10px;border:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700">Droits légaux — <span id="ent-year-label"></span></div>
          <button id="btn-ent-details" onclick="toggleEntDetails()" style="background:var(--card2);border:1px solid var(--border);color:var(--text);border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11px;font-weight:700">Réduire ▴</button>
        </div>
        <div id="entitlements-content" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:0;border-radius:8px;overflow:visible;border:1px solid var(--border)"></div>
        <div id="ent-details" style="margin-top:10px"></div>
      </div>
      <div id="base-banner" style="display:none;padding:10px 16px;background:rgba(249,115,22,.15);border:1px solid #fb923c;border-radius:10px;color:#fdba74;font-size:13px;font-weight:600;display:flex;align-items:center;justify-content:space-between">
        <span>⚠ Vue planning de base — le régime 4/5 est masqué</span>
        <button onclick="toggleBase()" style="background:none;border:1px solid #fb923c;color:#fdba74;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:12px;font-weight:700">Revenir au 4/5</button>
      </div>
      <div class="cal-grid" id="cal-grid"></div>
    </div>
    <div id="right-panel">
      <div class="card" id="balance-card">
        <h3>Soldes congés <span id="bal-year"></span></h3>
        <div id="balance-content"></div>
      </div>
      <div class="card">
        <h3>Activité du mois</h3>
        <div id="month-stats"></div>
      </div>
      <div class="card" id="card-45">
        <h3>Régime 4/5 — jour de repos</h3>
        <div style="font-size:11px;color:var(--muted);margin-bottom:10px">Cliquez pour choisir le jour fixe. Si ce jour est un 36/38/R, la logique de glissement s'applique automatiquement.</div>
        <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-bottom:10px" id="btn-45-row">
          <button class="btn-45" data-wd="0" onclick="set45(0)">Lun</button>
          <button class="btn-45" data-wd="1" onclick="set45(1)">Mar</button>
          <button class="btn-45" data-wd="2" onclick="set45(2)">Mer</button>
          <button class="btn-45" data-wd="3" onclick="set45(3)">Jeu</button>
          <button class="btn-45" data-wd="4" onclick="set45(4)">Ven</button>
        </div>
        <div style="display:flex;gap:6px;margin-top:0">
          <button class="btn btn-sm" style="background:var(--card2);flex:1;color:var(--muted)" onclick="set45(null)">✕ Désactiver</button>
          <button class="btn btn-sm" id="btn-toggle-base" style="flex:1" onclick="toggleBase()">Voir base</button>
        </div>
        <div style="margin-top:10px;font-size:11px;color:var(--muted)" id="label-45"></div>
      </div>
      <div class="card">
        <h3>Actions rapides</h3>
        <div style="display:flex;flex-direction:column;gap:8px">
          <button class="btn btn-primary" onclick="openLeaveModal()">+ Ajouter un congé</button>
          <button class="btn btn-green btn-sm" onclick="showView('annuel')">Vue annuelle →</button>
          <button class="btn btn-sm" id="btn-gcal" style="background:#4285f4;color:#fff;display:none" onclick="syncGoogleCal()">📅 Synchroniser Google Agenda</button>
        </div>
        <div id="gcal-info" style="display:none;margin-top:10px;font-size:11px;color:var(--muted);line-height:1.5"></div>
      </div>
    </div>
  </div>

  <!-- WEEK VIEW (mobile) -->
  <div id="week-view"></div>

  <!-- ANNUAL VIEW -->
  <div id="annual-content" style="display:none;padding:24px;overflow-y:auto">
    <div id="annual-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px"></div>
  </div>

  <!-- EXCHANGES VIEW -->
  <div id="exchanges-content">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:10px">
      <div>
        <div style="font-size:15px;font-weight:700;color:var(--text)">Échanges de service</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">Suivi des changements hiérarchie + échanges collègues</div>
      </div>
      <button class="btn btn-primary" onclick="openExchangeModal()">+ Nouvel échange</button>
    </div>
    <div id="exch-balance-area"></div>
    <div class="exch-filter" id="exch-filter">
      <button class="active" onclick="filterExchanges('all',this)">Tout</button>
      <button onclick="filterExchanges('pending',this)">⏳ En attente</button>
      <button onclick="filterExchanges('done',this)">✅ Soldés</button>
    </div>
    <div class="exch-list" id="exch-list"></div>
  </div>
</div>

<!-- MODAL CONGÉ -->
<div class="modal-overlay" id="leave-modal">
  <div class="modal">
    <h2>Ajouter un congé / absence <span class="close" onclick="closeModal('leave-modal')">✕</span></h2>
    <div class="form-group">
      <label>Type de congé</label>
      <select id="leave-code" onchange="onLeaveCodeChange()"></select>
      <div class="form-note" id="leave-note"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Date début</label><input type="date" id="leave-start"></div>
      <div class="form-group"><label>Date fin</label><input type="date" id="leave-end"></div>
    </div>
    <div class="form-group">
      <label>Note (optionnel)</label>
      <input type="text" id="leave-note-text" placeholder="Ex: grippe, congé été...">
    </div>
    <div class="modal-footer">
      <button class="btn" onclick="closeModal('leave-modal')" style="background:var(--card2)">Annuler</button>
      <button class="btn btn-primary" onclick="submitLeave()">Enregistrer</button>
    </div>
  </div>
</div>

<!-- MODAL AGENT -->
<div class="modal-overlay" id="agent-modal">
  <div class="modal">
    <h2>Mon compte <span class="close" onclick="closeModal('agent-modal')">✕</span></h2>
    <div id="agent-list" style="margin-bottom:16px"></div>
    <hr style="border-color:var(--border);margin:16px 0">
    <div style="font-size:13px;font-weight:600;margin-bottom:10px">📅 Date de début de carrière (SPF Justice)</div>
    <div class="form-row">
      <div class="form-group">
        <input type="date" id="acc-career" max="2025-12-31">
        <div class="form-note">Sert au calcul du capital maladie (21j × années de service)</div>
      </div>
      <div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" onclick="saveCareer()">Enregistrer</button></div>
    </div>
    <hr style="border-color:var(--border);margin:16px 0">
    <div style="font-size:13px;font-weight:600;margin-bottom:10px">🔒 Changer mon code PIN</div>
    <div class="form-row">
      <div class="form-group"><label>Nouveau PIN (4 chiffres min.)</label><input id="acc-pin" type="password" inputmode="numeric" maxlength="20" placeholder="••••"></div>
      <div class="form-group"><label>&nbsp;</label><button class="btn btn-primary" onclick="changePin()">Mettre à jour</button></div>
    </div>
    <div class="modal-footer" style="justify-content:space-between">
      <button class="btn btn-danger btn-sm" onclick="deleteMyAccount()" title="Supprime définitivement ton compte et toutes tes données">🗑 Supprimer mon compte</button>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeModal('agent-modal')" style="background:var(--card2)">Fermer</button>
        <button class="btn btn-primary" onclick="doLogout()" style="background:#dc2626">⎋ Déconnexion</button>
      </div>
    </div>
  </div>
</div>

<!-- MODAL JOUR (detail) -->
<div class="modal-overlay" id="day-modal">
  <div class="modal">
    <div class="dm-nav">
      <button class="dm-nav-arrow" id="dm-prev">&#8592;</button>
      <div class="dm-date-title">
        <div class="dm-weekday" id="dm-weekday"></div>
        <div class="dm-main-date" id="dm-maindate"></div>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <button class="dm-nav-arrow" id="dm-next">&#8594;</button>
        <span class="close" onclick="closeModal('day-modal')" style="margin-left:6px;opacity:.5;cursor:pointer;font-size:20px">✕</span>
      </div>
    </div>
    <div class="dm-body">
      <div class="dm-top">
        <div class="dm-shift-block" id="dm-shift-block"></div>
        <div class="dm-week-strip" id="dm-week-strip"></div>
      </div>
      <div class="dm-events-section" id="dm-events"></div>
    </div>
    <div class="dm-shift-override">
      <div class="dm-sec-label">🔄 Changer le poste</div>
      <div class="shift-btn-group" id="dm-shift-btns">
        <button class="shift-btn sb-matin" id="sb-M" onclick="setShiftOverride('M')">🌅 MATIN</button>
        <button class="shift-btn sb-soir" id="sb-S" onclick="setShiftOverride('S')">🌆 SOIR</button>
        <button class="shift-btn sb-h12" id="sb-12H" onclick="setShiftOverride('12H')">🕛 12H</button>
        <button class="shift-btn sb-h08" id="sb-08H" onclick="setShiftOverride('08H')">🕗 08H</button>
        <button class="shift-btn sb-repos" id="sb-R" onclick="setShiftOverride('R')">🛌 REPOS</button>
        <button class="shift-btn sb-reset" id="sb-reset" onclick="resetShiftOverride()" title="Restaurer le poste original du cycle">↩ Original</button>
      </div>
    </div>
    <div class="dm-remark-section">
      <div class="dm-remark-label">
        <span>📝 Remarque personnelle</span>
        <button class="dm-remark-save" onclick="saveDayRemark()">Enregistrer</button>
      </div>
      <textarea id="dm-remark-area" placeholder="Ajouter une note pour ce jour..."></textarea>
    </div>
    <div class="dm-footer">
      <button class="btn" onclick="closeModal('day-modal')" style="background:var(--card2)">Fermer</button>
      <button class="btn btn-primary" onclick="openLeaveFromDay()">+ Ajouter congé</button>
    </div>
  </div>
</div>

<!-- MODAL ÉCHANGE DE SERVICE -->
<div class="modal-overlay" id="exchange-modal">
  <div class="modal" style="width:480px">
    <h2>Échange de service <span class="close" onclick="closeModal('exchange-modal')">✕</span></h2>
    <div class="form-group">
      <label>Type d'échange</label>
      <select id="ex-type" onchange="onExTypeChange()">
        <option value="collegue">Échange avec un collègue</option>
        <option value="hierarchie">Changement imposé par la hiérarchie</option>
      </select>
    </div>
    <div id="ex-collegue-section">
      <div class="form-row">
        <div class="form-group">
          <label>Sens</label>
          <select id="ex-direction">
            <option value="donne">↗ J'ai travaillé POUR lui/elle</option>
            <option value="recu">↙ Il/elle a travaillé POUR moi</option>
          </select>
        </div>
        <div class="form-group">
          <label>Nom du collègue</label>
          <input type="text" id="ex-colleague" placeholder="Ex: Jean Dupont">
        </div>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Date du service échangé</label>
        <input type="date" id="ex-date-service">
      </div>
      <div class="form-group">
        <label>Poste ce jour-là</label>
        <select id="ex-poste-service">
          <option value="M">🔴 Matin (M)</option>
          <option value="S">🟠 Soir (S)</option>
          <option value="R">🟢 Repos (R)</option>
          <option value="">— Non précisé</option>
        </select>
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Date de remboursement <span style="color:var(--muted)">(si déjà connu)</span></label>
        <input type="date" id="ex-date-remb">
      </div>
      <div class="form-group">
        <label>Poste remboursement</label>
        <select id="ex-poste-remb">
          <option value="">— Non précisé</option>
          <option value="M">🔴 Matin (M)</option>
          <option value="S">🟠 Soir (S)</option>
          <option value="R">🟢 Repos (R)</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>Note / motif</label>
      <input type="text" id="ex-note" placeholder="Ex: urgence, accord verbal avec chef...">
    </div>
    <div class="modal-footer">
      <button class="btn" style="background:var(--card2);color:var(--muted)" onclick="closeModal('exchange-modal')">Annuler</button>
      <button class="btn btn-primary" onclick="saveExchange()">Enregistrer</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let curYear  = new Date().getFullYear();
let curMonth = new Date().getMonth() + 1;
let curAgent = '';
let curTeamOffset = null;  // offset actif (0-49) — null = aucune équipe sélectionnée
let _allAgents = {};  // cache de tous les agents (toutes équipes)
let curView  = 'calendar';
let catalog  = {};
let selDay   = null;
let viewBase = false;   // true = masque le 4/5, affiche le planning de base
let curWeekStart = null;   // Date (lundi) — semaine affichée en mode mobile
let mobileWeekMode = false; // true = vue semaine sur mobile
const MN = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre'];

// ── HELPERS SEMAINE ──
function isMobile() { return window.innerWidth <= 640; }
function getMondayOf(d) {
  const r = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const wd = r.getDay();
  r.setDate(r.getDate() - (wd === 0 ? 6 : wd - 1));
  return r;
}
function toDateStr(d) {
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
}

// ── INIT ──
async function init() {
  mobileWeekMode = isMobile();
  catalog = await fetch('/api/leaves_catalog').then(r=>r.json());
  populateCatalog();
  await loadAgents();   // ne renvoie QUE l'agent connecté -> curAgent = soi
  // Mode mono-agent : masquer la sélection d'équipe et d'agent (un agent ne voit que lui)
  ['team-btns','mobile-team-bar','mobile-agent-bar','team-section-label'].forEach(id=>{
    const e=document.getElementById(id); if(e) e.style.display='none';
  });
  const aSec=document.querySelector('.agent-section'); if(aSec) aSec.style.display='none';
  showView('calendar');
  gotoToday();
  initSwipe();
  window.addEventListener('resize', ()=>{
    if(!isMobile() && mobileWeekMode){ mobileWeekMode=false; showView('calendar'); }
    updateWeekToggleBtn();
  });
}

function populateCatalog() {
  const sel = document.getElementById('leave-code');
  const cats = {};
  Object.entries(catalog).forEach(([k,v])=>{
    if(!cats[v.category]) cats[v.category]=[];
    cats[v.category].push([k,v]);
  });
  Object.entries(cats).forEach(([cat,items])=>{
    const og = document.createElement('optgroup');
    og.label = cat;
    items.forEach(([k,v])=>{
      const o = document.createElement('option');
      o.value=k; o.textContent=`${v.label}${v.days?' ('+v.days+'j)':''}`;
      og.appendChild(o);
    });
    sel.appendChild(og);
  });
  onLeaveCodeChange();
}

function onLeaveCodeChange() {
  const code = document.getElementById('leave-code').value;
  const info = catalog[code];
  const el   = document.getElementById('leave-note');
  if(info && info.note) el.textContent = '📌 '+info.note;
  else el.textContent = '';
}

// ── AGENTS ──
function _applyTeamFilter(offset, autoSelect = true) {
  // Filtre le dropdown agent sur l'équipe (offset) ou tous si offset=null
  // autoSelect=true : sélectionne le 1er agent si aucun agent courant n'est dans l'équipe
  // autoSelect=false : vide la sélection (bouton équipe = vue planning brut)
  const sel  = document.getElementById('agent-select');
  const selM = document.getElementById('agent-select-mobile');
  const prev = curAgent || sel.value || '';
  const agents = offset !== null
    ? Object.fromEntries(Object.entries(_allAgents).filter(([,a]) => a.team_offset === offset))
    : _allAgents;
  [sel, selM].forEach(s => {
    s.innerHTML = '<option value="">— Sélectionner —</option>';
    Object.entries(agents).forEach(([id,a]) => {
      const o = document.createElement('option');
      o.value = id; o.textContent = a.name; s.appendChild(o);
    });
    if(prev && agents[prev]) s.value = autoSelect ? prev : '';
    else if(autoSelect && Object.keys(agents).length > 0) s.value = Object.keys(agents)[0];
    else s.value = '';
  });
  curAgent = sel.value;
}

async function loadAgents() {
  _allAgents = await fetch('/api/agents').then(r=>r.json());
  // Remplir le dropdown filtré par équipe si une équipe est déjà active
  _applyTeamFilter(curTeamOffset);
  curAgent = document.getElementById('agent-select').value;
  updateAgentInfo(_allAgents);
  renderAgentList(_allAgents);
  if(curAgent && _allAgents[curAgent]) {
    // Synchroniser le bouton équipe avec l'agent actif
    const agentOffset = _allAgents[curAgent].team_offset;
    curTeamOffset = agentOffset;
    document.querySelectorAll('.team-btn').forEach(b => {
      b.classList.toggle('active', parseInt(b.dataset.offset) === agentOffset);
    });
    const t = OFFSET_TO_TEAM[agentOffset];
    document.getElementById('view-title').textContent = t ? `Équipe ${t} — Calendrier` : 'Calendrier';
    refresh();
    const btnGcal = document.getElementById('btn-gcal');
    if(btnGcal) btnGcal.style.display = '';
  } else if(curTeamOffset !== null) {
    refresh();
  }
}

function onAgentChangeMobile() {
  const selM = document.getElementById('agent-select-mobile');
  document.getElementById('agent-select').value = selM.value;
  onAgentChange();
}

function toggleMobilePanel() {
  const rp = document.getElementById('right-panel');
  rp.classList.toggle('panel-open');
  document.getElementById('btn-panel-mobile').classList.toggle('active', rp.classList.contains('panel-open'));
}

async function syncGoogleCal() {
  if(!curAgent) return;
  const res = await fetch('/api/ical_url').then(r=>r.json()).catch(()=>null);
  if(!res || !res.url){ toast('Lien iCal indisponible','error'); return; }
  const icalUrl = res.url;
  const gcalUrl = 'https://calendar.google.com/calendar/r?cid=' + encodeURIComponent(icalUrl);
  const info = document.getElementById('gcal-info');
  info.style.display = 'block';
  info.innerHTML = '';
  const safeIcal = document.createTextNode(icalUrl);
  const aIcal = document.createElement('a');
  aIcal.href = icalUrl; aIcal.style.cssText = 'color:var(--accent);word-break:break-all';
  aIcal.appendChild(safeIcal);
  const aGcal = document.createElement('a');
  aGcal.href = gcalUrl; aGcal.target = '_blank';
  aGcal.style.cssText = 'color:#4285f4;font-weight:700';
  aGcal.textContent = '▶ Ouvrir dans Google Agenda';
  info.insertAdjacentHTML('beforeend', '<b>Lien iCal :</b><br>');
  info.appendChild(aIcal);
  info.insertAdjacentHTML('beforeend', '<br><br><b>Méthode 1 — Bouton automatique :</b><br>');
  info.appendChild(aGcal);
  info.insertAdjacentHTML('beforeend', '<br><br><b>Méthode 2 — Manuel :</b><br>Google Agenda → Autres agendas (+) → Via URL → coller le lien iCal');
  // Essayer d\'ouvrir directement
  window.open(gcalUrl, '_blank');
}

const WD_FR=['Lundi','Mardi','Mercredi','Jeudi','Vendredi'];
const WD_SHORT=['Lun','Mar','Mer','Jeu','Ven'];
const OFFSET_TO_TEAM={0:4,7:8,14:1,21:5,28:2,35:6,42:3,49:7};
function teamLabel(offset){const t=OFFSET_TO_TEAM[offset];return t?`Équipe ${t}`:`Offset ${offset}j`;}

function updateAgentInfo(agents) {
  const el = document.getElementById('agent-info');
  if(!curAgent || !agents[curAgent]) { el.textContent=''; return; }
  const a = agents[curAgent];
  let age_str = '';
  if(a.birth_date) {
    const age = new Date().getFullYear() - parseInt(a.birth_date.split('-')[0]);
    age_str = `${age} ans`;
  } else if(a.age) age_str = `${a.age} ans`;
  let info = age_str ? `${age_str} | ` : '';
  info += teamLabel(a.team_offset);
  if(a.regime_4_5 != null) info += ` | 4/5 ${WD_SHORT[a.regime_4_5]}`;
  el.textContent = info;
  refresh45UI(a.regime_4_5);
}

function refresh45UI(wd) {
  document.querySelectorAll('.btn-45').forEach(b=>{
    b.classList.toggle('active', wd != null && parseInt(b.dataset.wd)===wd);
  });
  const lbl = document.getElementById('label-45');
  if(lbl) lbl.textContent = wd != null
    ? `Jour actif : ${WD_FR[wd]} — le 36/38 de la semaine est absorbé automatiquement.`
    : 'Aucun régime 4/5 actif.';
}

async function set45(wd) {
  if(!curAgent){ toast('Sélectionnez un agent','error'); return; }
  const r = await fetch(`/api/agents/${curAgent}`, {
    method:'PATCH',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({regime_4_5: wd})
  });
  if(!r.ok){ toast('Erreur mise à jour 4/5','error'); return; }
  const data = await r.json();
  const newWd = data.agent.regime_4_5;
  // Quand on change le jour 4/5, on revient à la vue 4/5
  if(viewBase){ viewBase=false; _applyBaseToggleUI(); }
  refresh45UI(newWd);
  const agents = await fetch('/api/agents').then(r=>r.json());
  updateAgentInfo(agents);
  toast(wd != null ? `4/5 activé le ${WD_FR[wd]}` : '4/5 désactivé');
  renderCalendar();
}

function toggleBase() {
  viewBase = !viewBase;
  _applyBaseToggleUI();
  renderCalendar();
}

function _applyBaseToggleUI() {
  const btn  = document.getElementById('btn-toggle-base');
  const ban  = document.getElementById('base-banner');
  if(viewBase) {
    if(btn){ btn.textContent='Voir 4/5'; btn.style.background='rgba(249,115,22,.25)'; btn.style.color='#fdba74'; btn.style.border='1px solid #fb923c'; }
    if(ban){ ban.style.display='flex'; }
  } else {
    if(btn){ btn.textContent='Voir base'; btn.style.background=''; btn.style.color=''; btn.style.border=''; }
    if(ban){ ban.style.display='none'; }
  }
}

function onAgentChange() {
  curAgent = document.getElementById('agent-select').value;
  const selM = document.getElementById('agent-select-mobile');
  if(selM) selM.value = curAgent;
  viewBase = false;
  _applyBaseToggleUI();
  loadAgents();
}

function selectTeam(offset) {
  curTeamOffset = offset;
  viewBase = false;
  _applyBaseToggleUI();
  document.querySelectorAll('.team-btn').forEach(b => {
    b.classList.toggle('active', parseInt(b.dataset.offset) === offset);
  });
  const t = OFFSET_TO_TEAM[offset];
  document.getElementById('view-title').textContent = `Équipe ${t} — Calendrier`;
  // Filtrer le dropdown sur l'équipe SANS auto-sélectionner un agent
  // (le bouton équipe affiche le planning brut du PDF sans 4/5)
  _applyTeamFilter(offset, false);
  updateAgentInfo(_allAgents);
  curAgent = '';
  document.getElementById('agent-select').value = '';
  const selM = document.getElementById('agent-select-mobile');
  if(selM) selM.value = '';
  document.getElementById('agent-info').textContent = '';
  const aim = document.getElementById('agent-info-mobile');
  if(aim) aim.textContent = '';
  const ebar = document.getElementById('entitlements-bar');
  if(ebar) ebar.style.display = 'none';
  refresh();
}

const TEAM_OPTIONS = [{v:14,t:1},{v:28,t:2},{v:42,t:3},{v:0,t:4},{v:21,t:5},{v:35,t:6},{v:49,t:7},{v:7,t:8}];

function renderAgentList(agents) {
  const el = document.getElementById('agent-list');
  if(Object.keys(agents).length===0){el.innerHTML='<p style="color:var(--muted);font-size:12px">Aucun agent enregistré.</p>';return;}
  el.innerHTML = Object.entries(agents).map(([id,a])=>{
    let meta = id;
    if(a.birth_date) {
      const age = new Date().getFullYear() - parseInt(a.birth_date.split('-')[0]);
      meta += ` · ${age} ans (né ${a.birth_date})`;
    } else if(a.age) meta += ` · ${a.age} ans`;
    if(a.career_start) meta += ` · carrière depuis ${a.career_start}`;
    const warn = !a.birth_date ? ' <span style="color:#fb923c;font-size:10px">⚠ Ajouter date naissance</span>' : '';
    const opts = TEAM_OPTIONS.map(o=>`<option value="${o.v}"${o.v===a.team_offset?' selected':''}>Équipe ${o.t}</option>`).join('');
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:600">${a.name}${warn}</div>
        <div style="font-size:11px;color:var(--muted)">${meta}</div>
        <div style="margin-top:4px;display:flex;align-items:center;gap:6px">
          <select style="font-size:11px;padding:2px 4px;background:var(--card2);color:var(--fg);border:1px solid var(--border);border-radius:6px" onchange="changeTeam('${id}',this.value)">${opts}</select>
        </div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteAgent('${id}')" style="margin-left:8px">Suppr.</button>
    </div>`;
  }).join('');
}

async function changeTeam(aid, offset) {
  await fetch(`/api/agents/${aid}`,{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({team_offset:parseInt(offset)})});
  const agents = await fetch('/api/agents').then(r=>r.json());
  updateAgentInfo(agents);
  renderAgentList(agents);
  if(curAgent===aid){ toast(`Équipe mise à jour — ${teamLabel(parseInt(offset))}`); refresh(); }
}

async function addAgent() {
  const id    = document.getElementById('na-id').value.trim().toUpperCase();
  const name  = document.getElementById('na-name').value.trim();
  const birth = document.getElementById('na-birth').value;
  const career= document.getElementById('na-career').value;
  const offset= parseInt(document.getElementById('na-offset').value);
  const r45   = document.getElementById('na-45').value;
  if(!id || !name){ toast('ID et Nom obligatoires','error'); return; }
  if(!birth){ toast('Date de naissance obligatoire','error'); return; }
  await fetch('/api/agents',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({id, name, birth_date: birth, career_start: career||null,
                         offset, regime_4_5: r45===''?null:parseInt(r45)})});
  toast(`Agent ${name} ajouté`);
  await loadAgents();
  document.getElementById('na-id').value='';
  document.getElementById('na-name').value='';
  document.getElementById('na-birth').value='';
  document.getElementById('na-career').value='';
}

async function deleteAgent(id) {
  if(!confirm('Supprimer ce compte et toutes ses données ?')) return;
  await fetch(`/api/agents/${id}`,{method:'DELETE'});
  if(id===curAgent){ await fetch('/api/auth/logout',{method:'POST'}); location.reload(); return; }
  toast('Compte supprimé');
  await loadAgents();
}

// ── Mon compte ──
async function changePin(){
  const pin = document.getElementById('acc-pin').value.trim();
  if(pin.length < 4){ toast('PIN trop court (4 chiffres min.)','error'); return; }
  const r = await fetch(`/api/agents/${curAgent}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({pin})});
  if(r.ok){ toast('PIN mis à jour'); document.getElementById('acc-pin').value=''; }
  else toast('Erreur','error');
}
async function saveCareer(){
  const career = document.getElementById('acc-career').value || null;
  const r = await fetch(`/api/agents/${curAgent}`,{method:'PATCH',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({career_start:career})});
  if(r.ok){ toast('Date de carrière enregistrée'); loadAgents(); }
  else toast('Erreur','error');
}
async function deleteMyAccount(){
  if(!confirm('⚠️ Supprimer DÉFINITIVEMENT ton compte et toutes tes données ?')) return;
  if(!confirm('Dernière confirmation : cette action est irréversible.')) return;
  await fetch(`/api/agents/${curAgent}`,{method:'DELETE'});
  await fetch('/api/auth/logout',{method:'POST'});
  location.reload();
}

function printMonth() {
  if(!curAgent){ toast('Sélectionnez un agent avant d\'imprimer','error'); return; }
  window.open('/print/'+curAgent+'/'+curYear+'/'+curMonth, '_blank');
}

function openFicheAgent() {
  if(!curAgent){ toast('Sélectionnez un agent pour sa fiche personnelle','error'); return; }
  window.open('/fiche/agent/'+curAgent+'/'+curYear, '_blank');
}
function openFicheBlank() {
  const off = (curTeamOffset !== null) ? curTeamOffset : 0;
  window.open('/fiche/'+off+'/'+curYear, '_blank');
}

async function resetAllData() {
  if(!confirm('⚠️ ATTENTION : Cela supprime TOUTES les données (agents, congés, échanges, remarques).\n\nConfirmer pour remettre à zéro ?')) return;
  if(!confirm('Dernière confirmation : remettre vraiment tout à zéro ?')) return;
  const r = await fetch('/api/reset', {method:'POST'});
  if(r.ok){
    curAgent = '';
    toast('Toutes les données ont été supprimées', 'ok');
    closeModal('agent-modal');
    loadAgents();
    renderCalendar();
  } else toast('Erreur','error');
}

// ── NAVIGATION ──
function gotoToday() {
  const now = new Date();
  curYear=now.getFullYear(); curMonth=now.getMonth()+1;
  curWeekStart = getMondayOf(now);
  refresh();
}
function prevPeriod() {
  if(curView==='calendar' && isMobile() && mobileWeekMode){
    curWeekStart = new Date(curWeekStart); curWeekStart.setDate(curWeekStart.getDate()-7);
  } else if(curView==='calendar'){ if(--curMonth<1){curMonth=12;curYear--;} }
  else curYear--;
  refresh();
}
function nextPeriod() {
  if(curView==='calendar' && isMobile() && mobileWeekMode){
    curWeekStart = new Date(curWeekStart); curWeekStart.setDate(curWeekStart.getDate()+7);
  } else if(curView==='calendar'){ if(++curMonth>12){curMonth=1;curYear++;} }
  else curYear++;
  refresh();
}
function showView(v) {
  curView=v;
  const calMobile = v==='calendar' && isMobile() && mobileWeekMode;
  document.getElementById('content').style.display           = (v==='calendar' && !calMobile) ? 'grid' : 'none';
  document.getElementById('week-view').classList.toggle('wv-active', calMobile);
  document.getElementById('annual-content').style.display    = v==='annuel'    ? 'block' : 'none';
  document.getElementById('exchanges-content').style.display = v==='exchanges' ? 'block' : 'none';
  const titles={calendar:'Calendrier',annuel:'Vue annuelle',exchanges:'Échanges de service'};
  document.getElementById('view-title').textContent = titles[v]||v;
  document.querySelectorAll('.nav-btn').forEach((b,i)=>{
    b.classList.toggle('active',
      (i===0&&v==='calendar')||(i===1&&v==='annuel')||(i===2&&v==='exchanges'));
  });
  refresh();
}
function refresh() {
  if(curView==='calendar'){
    if(isMobile() && mobileWeekMode) renderWeekView();
    else renderCalendar();
  } else if(curView==='annuel') renderAnnual();
  else if(curView==='exchanges') renderExchanges();
  updateWeekToggleBtn();
}

// ── TOGGLE SEMAINE/MOIS ──
function toggleWeekMode() {
  mobileWeekMode = !mobileWeekMode;
  if(mobileWeekMode && !curWeekStart) curWeekStart = getMondayOf(new Date());
  showView('calendar');
}
function updateWeekToggleBtn() {
  const btn = document.getElementById('btn-week-toggle');
  if(!btn) return;
  btn.textContent = mobileWeekMode ? '📅 Mois' : '📋 Semaine';
}

// ── SWIPE SEMAINE ──
function initSwipe() {
  let sx=0, sy=0;
  const el = document.getElementById('week-view');
  el.addEventListener('touchstart', e=>{ sx=e.touches[0].clientX; sy=e.touches[0].clientY; },{passive:true});
  el.addEventListener('touchend', e=>{
    const dx = e.changedTouches[0].clientX - sx;
    const dy = e.changedTouches[0].clientY - sy;
    if(Math.abs(dx) > 55 && Math.abs(dx) > Math.abs(dy)*1.5){
      if(dx < 0) nextPeriod(); else prevPeriod();
    }
  },{passive:true});
}

// ── VUE SEMAINE ──
const WV_DOW  = ['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'];
const WV_MON  = ['jan','fév','mar','avr','mai','jun','jul','aoû','sep','oct','nov','déc'];
const WV_MONL = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre'];

async function renderWeekView() {
  if(!curWeekStart) curWeekStart = getMondayOf(new Date());
  const wv = document.getElementById('week-view');

  // Label période
  const endDate = new Date(curWeekStart); endDate.setDate(endDate.getDate()+6);
  const sL = `${curWeekStart.getDate()} ${WV_MON[curWeekStart.getMonth()]}`;
  const eL = `${endDate.getDate()} ${WV_MON[endDate.getMonth()]} ${endDate.getFullYear()}`;
  document.getElementById('period-label').textContent = `${sL} – ${eL}`;

  if(!curAgent && curTeamOffset === null){
    wv.innerHTML='<div class="wv-empty">Sélectionner une équipe pour voir la semaine</div>';
    return;
  }

  // 7 dates de la semaine
  const dates = [];
  for(let i=0;i<7;i++){
    const d = new Date(curWeekStart); d.setDate(d.getDate()+i);
    dates.push(toDateStr(d));
  }

  // Fetch jours + soldes en parallèle
  const yr = curWeekStart.getFullYear();
  const dayUrl = dt => curAgent
    ? `/api/day/${curAgent}/${dt}`
    : `/api/day/team/${curTeamOffset}/${dt}`;
  const [days, bal] = await Promise.all([
    Promise.all(dates.map(dt=>
      fetch(dayUrl(dt)).then(r=>r.json()).catch(()=>null)
    )),
    curAgent ? fetch(`/api/balance/${curAgent}/${yr}`).then(r=>r.json()).catch(()=>null) : Promise.resolve(null),
  ]);

  // ── Mini barre de soldes ──
  let balHtml = '';
  if(bal && !bal.error){
    const v=bal.vacances, m=bal.maladie;
    const vC=v.solde<=5?'var(--red)':v.solde<=8?'var(--orange)':'var(--green)';
    const mC=m.solde<21?'var(--red)':m.solde<63?'var(--orange)':'var(--green)';
    // Compter les postes de la semaine
    let nM=0,nS=0,nR=0;
    days.forEach(d=>{ if(!d)return; const b=(viewBase&&d.code==='4/5')?d.base:d.base; if(d.code&&d.code!=='4/5'&&d.code!=='REPOS-R'&&d.code!=='REPOS-38'){nR++;}else if(b==='M')nM++;else if(b==='S')nS++;else nR++;});
    balHtml = `<div id="wv-balance">
      <div class="wvb-item">
        <div class="wvb-lbl">🏖 Vacances</div>
        <div class="wvb-val" style="color:${vC}">${v.solde}</div>
        <div class="wvb-sub">j restants / ${v.droit}j</div>
      </div>
      <div class="wvb-item">
        <div class="wvb-lbl">🏥 Maladie</div>
        <div class="wvb-val" style="color:${mC}">${m.solde}</div>
        <div class="wvb-sub">j restants</div>
      </div>
      <div class="wvb-item">
        <div class="wvb-lbl">📅 Cette sem.</div>
        <div class="wvb-val" style="font-size:13px;margin-top:3px;color:var(--text)">
          ${nM?`<span style="color:#fca5a5">${nM}M</span> `:''}${nS?`<span style="color:#fdba74">${nS}S</span> `:''}${nR?`<span style="color:#86efac">${nR}R</span>`:''}
        </div>
        <button class="wvb-action" onclick="openLeaveModal()">+ Congé</button>
      </div>
    </div>`;
  }

  // ── Cartes jours ──
  const todayStr = toDateStr(new Date());
  let cardsHtml = '';

  days.forEach((day, i) => {
    if(!day || day.error) return;
    const dt = dates[i];
    const dn = parseInt(dt.slice(8,10),10);
    const mo = parseInt(dt.slice(5,7),10)-1;
    const isToday = dt===todayStr;

    const code = (viewBase && day.code==='4/5') ? null : day.code;
    const base = day.base;
    let bCls='b-green', cCls='c-green', pCls='p-green', pillTxt='Repos', reasonTxt='', hours='';

    if(code==='FERIE'||code==='PONT'){
      bCls='b-blue'; cCls='c-blue'; pCls='p-blue';
      const _sp={M:'Matin',S:'Soir',R:'Repos','36':'36h','38':'38h'};
      pillTxt=_sp[base]||'Repos';
      reasonTxt=(code==='PONT'?'Pont — ':'Jour férié — ')+(day.label||'');
      hours=day.shift_hours||'';
    } else if(code==='4/5'){
      bCls='b-green'; cCls='c-green'; pCls='p-green'; pillTxt='4/5'; reasonTxt='Régime 4/5';
    } else if(code==='REPOS-R'||code==='REPOS-38'){
      bCls='b-green'; cCls='c-green'; pCls='p-green'; pillTxt='Repos';
      reasonTxt=code==='REPOS-38'?'38h décalé':'Repos décalé';
    } else if(code){
      bCls='b-green'; cCls='c-green'; pCls='p-green'; pillTxt='Congé';
      reasonTxt=day.label||code;
    } else if(base==='M'){
      bCls='b-red'; cCls='c-red'; pCls='p-red'; pillTxt='Matin'; hours=day.shift_hours||'06:00 – 14:00';
    } else if(base==='S'){
      bCls='b-orange'; cCls='c-orange'; pCls='p-orange'; pillTxt='Soir'; hours=day.shift_hours||'14:00 – 22:00';
    } else if(base==='12H'){
      bCls='b-violet'; cCls='c-violet'; pCls='p-violet'; pillTxt='12H'; hours=day.shift_hours||'07:00 – 19:00';
    } else if(base==='08H'){
      bCls='b-teal'; cCls='c-teal'; pCls='p-teal'; pillTxt='08H'; hours=day.shift_hours||'08:00 – 16:00';
    } else if(base==='36'||base==='38'){
      bCls='b-purple'; cCls='c-purple'; pCls='p-purple'; pillTxt='Repos '+base;
    }

    const todayCls = isToday ? 'wc-today' : '';
    const remarkHtml = (day.remark && day.remark.trim())
      ? `<div class="wc-remark"><span class="remark-dot" style="flex-shrink:0"></span>${day.remark}</div>` : '';

    cardsHtml += `<div class="week-card ${bCls} ${cCls} ${todayCls}" onclick="openDayModal('${dt}')">
      <div class="wc-date">
        <div class="wc-dow">${WV_DOW[i]}</div>
        <div class="wc-num${isToday?' wc-num-today':''}">${dn}</div>
        <div class="wc-month">${WV_MON[mo]}</div>
      </div>
      <div class="wc-shift-col">
        <div class="wc-shift ${pCls}">${pillTxt}</div>
        ${hours?`<div class="wc-hours">${hours}</div>`:''}
      </div>
      <div class="wc-info">
        ${reasonTxt?`<div class="wc-reason">${reasonTxt}</div>`:''}
        ${remarkHtml}
      </div>
      <div class="wc-chevron">›</div>
    </div>`;
  });

  wv.innerHTML = balHtml + (cardsHtml || '<div class="wv-empty">Aucune donnée pour cette semaine</div>');
}

// ── CALENDAR ──
async function renderCalendar() {
  document.getElementById('period-label').textContent = `${MN[curMonth-1]} ${curYear}`;
  // Mode équipe sans agent
  if(!curAgent && curTeamOffset !== null) {
    const cal = await fetch(`/api/calendar/team/${curTeamOffset}/${curYear}/${curMonth}`).then(r=>r.json());
    renderGrid(cal);
    document.getElementById('entitlements-bar').style.display='none';
    return;
  }
  if(!curAgent){ clearCalendar(); document.getElementById('entitlements-bar').style.display='none'; return; }
  const [cal,bal,stats,ent] = await Promise.all([
    fetch(`/api/calendar/${curAgent}/${curYear}/${curMonth}`).then(r=>r.json()),
    fetch(`/api/balance/${curAgent}/${curYear}`).then(r=>r.json()),
    fetch(`/api/stats/${curAgent}/${curYear}`).then(r=>r.json()),
    fetch(`/api/entitlements/${curAgent}/${curYear}`).then(r=>r.json()),
  ]);
  renderGrid(cal);
  renderBalance(bal);
  renderMonthStats(stats[curMonth-1]);
  renderEntitlements(ent);
}

// ── ENTITLEMENTS ──
let _lastEnt = null;
let _entDetailsOpen = true;  // détails toujours ouverts par défaut

function toggleEntDetails() {
  _entDetailsOpen = !_entDetailsOpen;
  const btn = document.getElementById('btn-ent-details');
  const det = document.getElementById('ent-details');
  if(_entDetailsOpen && _lastEnt) {
    det.style.display = 'block';
    renderEntDetails(_lastEnt);
    if(btn){ btn.textContent = 'Réduire ▴'; btn.style.color='var(--text)'; }
  } else {
    det.style.display = 'none';
    if(btn){ btn.textContent = 'Détails ▾'; btn.style.color=''; }
  }
}

function renderEntitlements(ent) {
  _lastEnt = ent;
  const bar  = document.getElementById('entitlements-bar');
  const cont = document.getElementById('entitlements-content');
  const lbl  = document.getElementById('ent-year-label');
  const det  = document.getElementById('ent-details');
  if(!ent || ent.error){ bar.style.display='none'; return; }
  bar.style.display = 'block';
  lbl.textContent = `${ent.year} · ${ent.agent}`;

  const v   = ent.vacances;
  const m   = ent.maladie;
  const vPct = v.total>0 ? Math.min(Math.round(v.utilise/v.total*100),100) : 0;
  const vC   = v.solde<=5?'var(--red)':v.solde<=8?'var(--orange)':'var(--green)';

  // ── Pill vacances ──
  const vManualTag = v.manual ? ' <span style="color:var(--orange);font-weight:900">✎</span>' : '';
  const vNotSet = !v.manual && v.droit === 0;
  let vBody = '';
  if(vNotSet) {
    vBody = '<div style="font-size:13px;color:var(--orange);font-weight:700;margin:4px 0">⚠ À définir</div>'
          + '<div style="font-size:10px;color:var(--muted)">' + v.utilise + 'j déjà pris</div>';
  } else {
    const reliqHtml = v.reliquat > 0
      ? ' + <span style="color:var(--accent)">' + v.reliquat + 'j reliquat</span> = ' + v.total + 'j total'
      : '';
    vBody = '<div style="display:flex;align-items:baseline;gap:4px">'
          + '<span style="font-size:24px;font-weight:900;color:' + vC + '">' + v.solde + '</span>'
          + '<span style="font-size:11px;color:var(--muted)">j restants</span></div>'
          + '<div style="height:3px;background:var(--border);border-radius:2px;margin:4px 0;overflow:hidden">'
          + '<div style="width:' + vPct + '%;height:100%;background:' + vC + ';border-radius:2px"></div></div>'
          + '<div style="font-size:10px;color:var(--muted)">' + v.droit + 'j droit' + reliqHtml + ' · ' + v.utilise + 'j pris</div>';
  }
  const vBtnColor = (vNotSet || v.manual) ? 'var(--orange)' : 'var(--accent)';
  const vBtnText  = vNotSet ? '⚡ Saisir quota' : (v.manual ? '🔧 Quota: ' + v.droit + 'j' : '✏ Quota: ' + v.droit + 'j');
  let html = '<div style="padding:10px 14px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)">'
    + '<div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:3px">🏖️ VACANCES ' + ent.year + vManualTag + '</div>'
    + vBody
    + '<div style="font-size:10px;margin-top:4px;display:flex;gap:8px;flex-wrap:wrap">'
    + '<span onclick="editReliquat()" style="cursor:pointer;color:var(--accent);text-decoration:underline">✏ Reliquat: ' + v.reliquat + 'j</span>'
    + '<span onclick="editCapitalVacances()" style="cursor:pointer;color:' + vBtnColor + ';font-weight:' + (vNotSet?'700':'400') + ';text-decoration:underline">' + vBtnText + '</span>'
    + '</div></div>';

  // ── Pill maladie capital ──
  if(m){
    const mPct = m.capital>0 ? Math.min(Math.round(m.utilise/m.capital*100),100) : 0;
    const mC   = m.solde < 21?'var(--red)':m.solde < 63?'var(--orange)':'var(--green)';
    const mNotSet = !m.manual && m.capital === 0;
    const mManTag = m.manual ? ' <span style="color:var(--orange);font-weight:900">✎</span>' : '';
    let mBody = '';
    if(mNotSet) {
      mBody = '<div style="font-size:13px;color:var(--orange);font-weight:700;margin:4px 0">⚠ À définir</div>'
            + '<div style="font-size:10px;color:var(--muted)">' + m.utilise + 'j déjà pris</div>';
    } else {
      mBody = '<div style="display:flex;align-items:baseline;gap:4px">'
            + '<span style="font-size:24px;font-weight:900;color:' + mC + '">' + m.solde + '</span>'
            + '<span style="font-size:11px;color:var(--muted)">j restants / ' + m.capital + 'j</span></div>'
            + '<div style="height:3px;background:var(--border);border-radius:2px;margin:4px 0;overflow:hidden">'
            + '<div style="width:' + mPct + '%;height:100%;background:' + mC + ';border-radius:2px"></div></div>'
            + '<div style="font-size:10px;color:var(--muted)">' + m.utilise + 'j pris (toutes années)</div>';
    }
    const mBtnColor = (mNotSet || m.manual) ? 'var(--orange)' : 'var(--accent)';
    const mBtnText  = mNotSet ? '⚡ Saisir capital' : (m.manual ? '🔧 Capital: ' + m.capital + 'j' : '✏ Capital: ' + m.capital + 'j');
    html += '<div style="padding:10px 14px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)">'
      + '<div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:3px">🤒 CAP. MALADIE' + mManTag + '</div>'
      + mBody
      + '<div style="font-size:10px;margin-top:4px">'
      + '<span onclick="editCapitalMaladie()" style="cursor:pointer;color:' + mBtnColor + ';font-weight:' + (mNotSet?'700':'400') + ';text-decoration:underline">' + mBtnText + '</span>'
      + '</div></div>';
  }

  // ── Pill ancienneté ──
  if(m && m.service_months != null){
    const sy = Math.floor(m.service_months/12), smo = m.service_months%12;
    const nextCap = (Math.floor(m.service_months/12)+1)*21;
    html += `<div style="padding:10px 14px;border-right:1px solid var(--border);border-bottom:1px solid var(--border)">
      <div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:3px">📅 ANCIENNETÉ</div>
      <div style="font-size:22px;font-weight:900;color:var(--text)">${sy}<span style="font-size:12px;color:var(--muted)">a</span> ${smo}<span style="font-size:12px;color:var(--muted)">m</span></div>
      <div style="font-size:10px;color:var(--muted);margin-top:4px">Prochain cap auto: ${nextCap}j</div>
    </div>`;
  }

  // ── Pill âge ──
  const ageTxt = ent.age_this_year !== undefined ? ent.age_this_year : '?';
  let nextBracketHtml = '';
  if(ent.next_bracket){
    const nb = ent.next_bracket;
    const d  = new Date(nb.date);
    const fmt = d.toLocaleDateString('fr-BE',{day:'numeric',month:'short'});
    nextBracketHtml = `<div style="font-size:9px;color:var(--accent);margin-top:3px">
      ▶ ${fmt}: ${nb.age} ans → ${nb.days}j (+${nb.delta}j)</div>`;
  }
  html += `<div style="padding:10px 14px;border-bottom:1px solid var(--border)">
    <div style="font-size:10px;color:var(--muted);font-weight:700;margin-bottom:3px">👤 ÂGE au 01/01/${ent.year}</div>
    <div style="font-size:22px;font-weight:900;color:var(--text)">${ageTxt}<span style="font-size:12px;color:var(--muted)"> ans</span></div>
    <div style="font-size:10px;color:var(--muted);margin-top:4px">${v.droit}j/an BOSA</div>
    ${nextBracketHtml}
    ${!ent.birth_date?`<div style="font-size:9px;color:var(--orange);margin-top:2px">⚠ Ajouter date naiss.</div>`:''}
  </div>`;

  cont.innerHTML = html;

  // Re-render détails si ouverts
  if(_entDetailsOpen) renderEntDetails(ent);
  else det.innerHTML = '';
}

// Congés de circonstance à afficher avec leurs quotas légaux
const CIRC_CONFIG = [
  {code:'CIRC_NAI',         icon:'👶', quota:20,  label:'Naissance / coparentalité'},
  {code:'CIRC_DEC_CONJOINT',icon:'🖤', quota:10,  label:'Décès conjoint / enfant'},
  {code:'CIRC_DEC_PARENT',  icon:'🖤', quota:4,   label:'Décès père/mère/beau-parent'},
  {code:'CIRC_DEC_AUTRE',   icon:'🖤', quota:2,   label:'Décès frère/sœur/grd-parents'},
  {code:'CIRC_MAR_AGENT',   icon:'💍', quota:4,   label:'Mariage de l\'agent'},
  {code:'CIRC_MAR_ENFANT',  icon:'💍', quota:1,   label:'Mariage enfant/frère/sœur'},
  {code:'CIRC_COM',         icon:'✝️', quota:1,   label:'Communion / cérémonie laïque'},
  {code:'CIRC_DON',         icon:'🩸', quota:null, label:'Don de sang / organe'},
  {code:'CIRC_JURY',        icon:'⚖️', quota:null, label:'Jury Cour d\'Assises'},
];
const OTHER_CONFIG = [
  {code:'ACC_TRAV',   label:'Accident travail / mal. prof.'},
  {code:'CONG_MAT',   label:'Congé de maternité (semaines)'},
  {code:'CONG_PAR',   label:'Congé parental'},
  {code:'SOINS_FAM',  label:'Soins à un proche'},
  {code:'SYNDI',      label:'Congé syndical'},
  {code:'FORM',       label:'Formation / examen'},
  {code:'RECUP',      label:'Récupération / compensé'},
  {code:'INTERR_CARR',label:'Interruption de carrière'},
  {code:'COMPEN',     label:'Compensation férié weekend'},
];

function renderEntDetails(ent) {
  const det  = document.getElementById('ent-details');
  const used = ent.conges_detail || {};

  // Compter les congés de circonstance actifs cette année
  let circHtml = '';
  CIRC_CONFIG.forEach(cfg => {
    const u = used[cfg.code]?.used || 0;
    const q = cfg.quota;
    const pct = q ? Math.min(Math.round(u/q*100),100) : 0;
    const col = !q ? 'var(--muted)' : (u>=q ? 'var(--red)' : u>0 ? 'var(--orange)' : 'var(--green)');
    const solde = q ? `${u}/${q}j` : (u>0 ? `${u}j pris` : '—');
    circHtml += `<div style="background:var(--bg);border-radius:6px;padding:8px 10px;border:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:11px;font-weight:600">${cfg.icon} ${cfg.label}</span>
        <span style="font-size:13px;font-weight:700;color:${col}">${solde}</span>
      </div>
      ${q?`<div style="height:2px;background:var(--border);border-radius:1px;margin-top:5px;overflow:hidden"><div style="width:${pct}%;height:100%;background:${col};border-radius:1px"></div></div>`:''}
    </div>`;
  });

  // Autres congés utilisés cette année
  let otherHtml = '';
  OTHER_CONFIG.forEach(cfg => {
    const u = (used[cfg.code]?.used || 0);
    if(u === 0) return;
    otherHtml += `<div style="background:var(--bg);border-radius:6px;padding:8px 10px;border:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:11px;font-weight:600">${cfg.label}</span>
        <span style="font-size:13px;font-weight:700;color:var(--green)">${u}j</span>
      </div>
    </div>`;
  });

  det.innerHTML = `<div style="border-top:1px solid var(--border);padding-top:12px">
    <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin-bottom:8px">
      Congés de circonstance · ${ent.year}
    </div>
    <div class="ent-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:6px">
      ${circHtml}
    </div>
    ${otherHtml ? `<div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700;margin:12px 0 8px">
      Autres congés utilisés · ${ent.year}
    </div>
    <div class="ent-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:6px">
      ${otherHtml}
    </div>` : ''}
  </div>`;
}

async function editReliquat() {
  if(!curAgent) return;
  const cur = _lastEnt?.vacances?.reliquat ?? 0;
  const val = prompt(`Reliquat vacances reporté de ${curYear-1} vers ${curYear}\n(jours non pris l'an passé) :`, cur);
  if(val === null) return;
  const num = parseInt(val);
  if(isNaN(num) || num < 0){ toast('Valeur invalide','error'); return; }
  const r = await fetch(`/api/reliquat/${curAgent}/${curYear}`,{
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({reliquat: num})
  });
  if(r.ok){ toast(`Reliquat ${curYear}: ${num}j enregistré`); renderCalendar(); }
  else toast('Erreur','error');
}

async function editCapitalVacances() {
  if(!curAgent) return;
  const v = _lastEnt?.vacances;
  const isManual = v?.manual ?? false;
  const curVal   = isManual ? String(v.droit) : '';
  const msg = `Quota vacances annuel ${curYear}\n`
    + (isManual ? `Valeur actuelle : ${v.droit}j\n` : `Aucun quota défini (compteur à 0)\n`)
    + `\nEntrez le nombre de jours de vacances accordés par votre RH :`
    + (isManual ? `\n(Laissez vide pour remettre à 0)` : '');
  const val = prompt(msg, curVal);
  if(val === null) return;
  let body;
  if(val.trim() === '') {
    body = {vacances: null};   // remettre à 0
  } else {
    const num = parseInt(val);
    if(isNaN(num) || num < 0){ toast('Valeur invalide','error'); return; }
    body = {vacances: num};
  }
  const r = await fetch(`/api/capitals/${curAgent}/${curYear}`,{
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  if(r.ok){ toast(body.vacances===null ? `Quota vacances remis à 0` : `Quota vacances ${curYear} : ${body.vacances}j`,'ok'); renderCalendar(); }
  else toast('Erreur','error');
}

async function editCapitalMaladie() {
  if(!curAgent) return;
  const m = _lastEnt?.maladie;
  const isManual = m?.manual ?? false;
  const curVal   = isManual ? String(m.capital) : '';
  const msg = `Capital maladie ${curYear}\n`
    + (isManual ? `Valeur actuelle : ${m.capital}j\n` : `Aucun capital défini (compteur à 0)\n`)
    + `\nEntrez votre capital maladie (jours restants selon RH/Medex) :`
    + (isManual ? `\n(Laissez vide pour remettre à 0)` : '');
  const val = prompt(msg, curVal);
  if(val === null) return;
  let body;
  if(val.trim() === '') {
    body = {maladie: null};  // supprimer override
  } else {
    const num = parseInt(val);
    if(isNaN(num) || num < 0){ toast('Valeur invalide','error'); return; }
    body = {maladie: num};
  }
  const r = await fetch(`/api/capitals/${curAgent}/${curYear}`,{
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)
  });
  if(r.ok){ toast(body.maladie===null ? `Capital maladie remis à 0` : `Capital maladie : ${body.maladie}j`,'ok'); renderCalendar(); }
  else toast('Erreur','error');
}

function clearCalendar() {
  document.getElementById('cal-grid').innerHTML='<div style="color:var(--muted);font-size:13px;padding:20px;grid-column:span 7">Sélectionnez un agent pour afficher le calendrier.</div>';
}

function renderGrid(cal) {
  const grid = document.getElementById('cal-grid');
  const days_hdr = ['Lun','Mar','Mer','Jeu','Ven','Sam','Dim'];
  let html = days_hdr.map(d=>`<div class="cal-header">${d}</div>`).join('');

  for(let i=0;i<cal.first_weekday;i++) html+=`<div class="cal-day empty"></div>`;

  cal.days.forEach(day=>{
    let bCls, cCls, nCls, pCls, pillTxt, reasonTxt='';
    // En mode "base": ignorer le 4/5, afficher le poste du cycle brut
    const code = (viewBase && day.code==='4/5') ? null : day.code;
    const base = day.base;  // toujours disponible dans la réponse API

    if(code==='FERIE'||code==='PONT'){
      bCls='b-blue'; cCls='c-blue'; nCls='n-blue'; pCls='p-blue';
      const _sp={M:'MATIN',S:'SOIR',R:'REPOS','36':'REPOS-36','38':'REPOS-38'};
      pillTxt=_sp[base]||'REPOS';
      reasonTxt=(code==='PONT'?'[Pont] ':'[Ferie] ')+(day.label||'');
    } else if(code==='4/5'){
      bCls='b-green'; cCls='c-green'; nCls='n-green'; pCls='p-green';
      pillTxt='4/5'; reasonTxt='Régime 4/5';
    } else if(code==='REPOS-R'||code==='REPOS-38'){
      bCls='b-green'; cCls='c-green'; nCls='n-green'; pCls='p-green';
      pillTxt='REPOS';
      reasonTxt=code==='REPOS-38'?'38h decale':'R decale';
    } else if(code){
      bCls='b-green'; cCls='c-green'; nCls='n-green'; pCls='p-green';
      pillTxt='CONGÉ';
      const lbl=day.label||code;
      reasonTxt=lbl.length>26?lbl.substring(0,24)+'…':lbl;
    } else if(base==='M'){
      bCls='b-red';    cCls='c-red';    nCls='n-red';    pCls='p-red';    pillTxt='MATIN';
    } else if(base==='S'){
      bCls='b-orange'; cCls='c-orange'; nCls='n-orange'; pCls='p-orange'; pillTxt='SOIR';
    } else if(base==='12H'){
      bCls='b-violet'; cCls='c-violet'; nCls='n-violet'; pCls='p-violet'; pillTxt='12H';
    } else if(base==='08H'){
      bCls='b-teal';   cCls='c-teal';   nCls='n-teal';   pCls='p-teal';   pillTxt='08H';
    } else if(base==='36'||base==='38'){
      bCls='b-purple'; cCls='c-purple'; nCls='n-purple'; pCls='p-purple'; pillTxt='REPOS '+base;
    } else {
      bCls='b-green';  cCls='c-green';  nCls='n-green';  pCls='p-green';  pillTxt='REPOS';
    }

    const todayCls = day.is_today?'today':'';
    const weCls    = (day.weekday>=5)?'weekend':'';
    const hasRemark  = day.remark && day.remark.trim() !== '';
    const dataStr    = JSON.stringify(day).replace(/"/g,'&quot;');
    const remarkHtml = hasRemark
      ? '<div class="day-remark"><span class="remark-dot"></span>' + day.remark + '</div>'
      : '';
    const decaleHtml = day.decale_38
      ? '<div class="badge-decale">38h &#8595;</div>'
      : (day.decale_r ? '<div class="badge-decale">R &#8595;</div>' : '');

    html+='<div class="cal-day '+bCls+' '+cCls+' '+nCls+' '+todayCls+' '+weCls+'"'
        +' onclick="openDayModal('+dataStr+')"'
        +' title="'+day.day_name+' '+day.day_num+' — '+pillTxt+(reasonTxt?' : '+reasonTxt:'')+(hasRemark?' | Note: '+day.remark:'')+'">'
        +'<div class="day-top">'
        +'<span class="day-abbr">'+day.day_name+'</span>'
        +'<span class="shift-pill '+pCls+'">'+pillTxt+'</span>'
        +'</div>'
        +'<div class="day-num">'+day.day_num+'</div>'
        +(reasonTxt?'<div class="day-reason">'+reasonTxt+'</div>':'')
        +decaleHtml
        +remarkHtml
        +'</div>';
  });
  grid.innerHTML=html;
}

function renderBalance(bal) {
  document.getElementById('bal-year').textContent=bal.year;
  const v=bal.vacances, m=bal.maladie;
  const vPct=Math.round((v.utilise/v.droit)*100);
  const mPct=Math.round((m.utilise/m.droit)*100);
  const vCls=vPct>80?'danger':vPct>60?'warn':'';
  const mCls=mPct>80?'danger':mPct>60?'warn':'';
  let detail='';
  Object.entries(bal.detail||{}).forEach(([k,v])=>{
    if(k!=='VAC'&&k!=='MAL'&&v>0) detail+=`<div class="balance-row"><span class="balance-label">${catalog[k]?.label||k}</span><span class="balance-value" style="color:var(--green)">${v}j</span></div>`;
  });
  document.getElementById('balance-content').innerHTML=`
    <div class="balance-row">
      <div style="flex:1">
        <div style="display:flex;justify-content:space-between"><span class="balance-label">Vacances</span><span class="balance-value" style="color:var(--green)">${v.solde}/${v.droit}j</span></div>
        <div class="balance-bar"><div class="balance-fill ${vCls}" style="width:${vPct}%"></div></div>
      </div>
    </div>
    <div class="balance-row">
      <div style="flex:1">
        <div style="display:flex;justify-content:space-between"><span class="balance-label">Maladie</span><span class="balance-value" style="color:${mPct>80?'var(--red)':'var(--green)'}">${m.solde}/${m.droit}j</span></div>
        <div class="balance-bar"><div class="balance-fill ${mCls}" style="width:${mPct}%"></div></div>
      </div>
    </div>
    ${detail}`;
}

function renderMonthStats(s) {
  if(!s){document.getElementById('month-stats').innerHTML='';return;}
  const total=s.M+s.S+s.R+s.F+s.C;
  document.getElementById('month-stats').innerHTML=`
    <div class="stat-bar">
      <div style="width:${s.M/total*100}%;background:var(--red);border-radius:6px 0 0 6px" title="Matin ${s.M}j"></div>
      <div style="width:${s.S/total*100}%;background:var(--orange)" title="Soir ${s.S}j"></div>
      <div style="width:${(s.R+s.C)/total*100}%;background:var(--green)" title="Repos/Congés ${s.R+s.C}j"></div>
      <div style="width:${s.F/total*100}%;background:var(--green-dark);border-radius:0 6px 6px 0" title="Fériés ${s.F}j"></div>
    </div>
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--muted)">
      <span><span style="color:var(--red)">●</span> Matin: ${s.M}j</span>
      <span><span style="color:var(--orange)">●</span> Soir: ${s.S}j</span>
      <span><span style="color:var(--green)">●</span> Repos: ${s.R}j</span>
      <span><span style="color:var(--green-dark)">●</span> Fériés: ${s.F}j</span>
      ${s.C?`<span><span style="color:#86efac">●</span> Congés: ${s.C}j</span>`:''}
    </div>`;
}

// ── ANNUAL VIEW ──
async function renderAnnual() {
  document.getElementById('period-label').textContent = curYear.toString();
  if(!curAgent){document.getElementById('annual-grid').innerHTML='<p style="color:var(--muted)">Sélectionnez un agent.</p>';return;}
  const grid=document.getElementById('annual-grid');
  grid.innerHTML='<div style="color:var(--muted);font-size:13px">Chargement…</div>';
  const months = await Promise.all(
    Array.from({length:12},(_,i)=>fetch(`/api/calendar/${curAgent}/${curYear}/${i+1}`).then(r=>r.json()))
  );
  grid.innerHTML=months.map(cal=>renderMiniMonth(cal)).join('');
}

function renderMiniMonth(cal) {
  let html=`<div class="card" style="padding:14px">
    <div style="font-weight:700;font-size:13px;margin-bottom:10px;color:var(--text)">${cal.month_name}</div>
    <div style="display:grid;grid-template-columns:repeat(7,1fr);gap:2px">
    ${['L','M','M','J','V','S','D'].map(d=>`<div style="text-align:center;font-size:9px;font-weight:700;color:var(--muted);padding:2px 0">${d}</div>`).join('')}`;
  for(let i=0;i<cal.first_weekday;i++) html+=`<div></div>`;
  cal.days.forEach(d=>{
    let bc, tc;
    if(d.code==='FERIE'||d.code==='PONT'){bc='rgba(59,130,246,.35)';tc='#93c5fd';}
    else if(d.code){bc='rgba(34,197,94,.28)';tc='#86efac';}
    else if(d.base==='M'){bc='rgba(239,68,68,.35)';tc='#fca5a5';}
    else if(d.base==='S'){bc='rgba(249,115,22,.35)';tc='#fdba74';}
    else if(d.base==='36'||d.base==='38'){bc='rgba(168,85,247,.28)';tc='#d8b4fe';}
    else{bc='rgba(34,197,94,.22)';tc='#86efac';}
    const ring=d.is_today?'box-shadow:0 0 0 1.5px #f8fafc;':'';
    html+=`<div style="background:${bc};border-radius:3px;text-align:center;font-size:10px;font-weight:800;padding:4px 1px;cursor:pointer;color:${tc};${ring}"
      title="${d.day_name} ${d.day_num} — ${d.label||d.code||d.base}"
      onclick="showView('calendar');curMonth=${cal.month};renderCalendar()">${d.day_num}</div>`;
  });
  html+=`</div></div>`;
  return html;
}

// ── DAY MODAL ──
let _dayDate=null;

const COLOR_MAP={
  red:   {bg:'rgba(239,68,68,.15)',border:'#ef4444',text:'#fca5a5'},
  orange:{bg:'rgba(249,115,22,.15)',border:'#f97316',text:'#fdba74'},
  green: {bg:'rgba(34,197,94,.15)', border:'#22c55e',text:'#86efac'},
  blue:  {bg:'rgba(59,130,246,.15)',border:'#3b82f6',text:'#93c5fd'},
  purple:{bg:'rgba(139,92,246,.15)',border:'#8b5cf6',text:'#c4b5fd'},
  teal:  {bg:'rgba(20,184,166,.15)',border:'#14b8a6',text:'#5eead4'},
};
const SHIFT_FULL={M:'MATIN',S:'SOIR','12H':'JOUR 12H','08H':'JOUR 08H',R:'REPOS','36':'REPOS-36','38':'REPOS-38'};
const DAY_FR=['Lundi','Mardi','Mercredi','Jeudi','Vendredi','Samedi','Dimanche'];
const MO_FR=['janvier','février','mars','avril','mai','juin','juillet','août','septembre','octobre','novembre','décembre'];

async function openDayModal(dayOrStr) {
  let dateStr;
  if(typeof dayOrStr==='string') dateStr=dayOrStr;
  else dateStr=dayOrStr.date||`${curYear}-${String(curMonth).padStart(2,'0')}-${String(dayOrStr.day_num).padStart(2,'0')}`;
  if(!curAgent) return;
  _dayDate=dateStr;
  openModal('day-modal');
  await renderDayModal(dateStr);
}

async function renderDayModal(dateStr) {
  const d=await fetch(`/api/day/${curAgent}/${dateStr}`).then(r=>r.json());
  if(d.error){toast(d.error,'error');return;}
  _dayDate=dateStr;

  // Nav buttons
  document.getElementById('dm-prev').onclick=()=>renderDayModal(d.prev_date);
  document.getElementById('dm-next').onclick=()=>renderDayModal(d.next_date);

  // Header
  const parts=dateStr.split('-');
  const mo=parseInt(parts[1])-1;
  document.getElementById('dm-weekday').textContent=DAY_FR[d.weekday];
  document.getElementById('dm-maindate').textContent=`${d.day_num} ${MO_FR[mo]} ${parts[0]}`;

  // Shift block
  const clr=COLOR_MAP[d.color]||COLOR_MAP.green;
  const shiftLabel=d.code&&d.code!=='FERIE'&&d.code!=='PONT'
    ? (d.label||d.code)
    : (SHIFT_FULL[d.effective]||d.effective);
  document.getElementById('dm-shift-block').innerHTML=`
    <div style="border-left:4px solid ${clr.border};padding-left:12px;height:100%;display:flex;flex-direction:column;justify-content:center;gap:5px;background:${clr.bg};border-radius:10px;padding:14px 14px 14px 16px">
      <div class="dm-shift-code" style="color:${clr.text}">${d.effective}</div>
      <div class="dm-shift-label" style="color:${clr.text}">${shiftLabel}</div>
      <div class="dm-shift-hours" style="color:${clr.text}">${d.shift_hours}</div>
      <div class="dm-shift-cycle">Cycle · position ${d.cycle_pos} / ${d.cycle_len}</div>
    </div>`;

  // Week strip
  const wdays=['L','M','M','J','V','S','D'];
  let wcHTML='<div class="dm-ws-title">Semaine en cours</div><div class="dm-week-cells">';
  d.week.forEach((wd,i)=>{
    const wclr=COLOR_MAP[wd.color]||COLOR_MAP.green;
    const isAct=wd.date===dateStr;
    const pill=wd.code&&wd.code!=='FERIE'&&wd.code!=='PONT'?'C':(wd.code==='FERIE'||wd.code==='PONT'?'F':(SHIFT_FULL[wd.base]||wd.base).slice(0,1));
    wcHTML+=`<div class="dm-wc${isAct?' dm-wc-active':''}"
      style="background:${wclr.bg};border-color:${isAct?'#f8fafc':wclr.border}"
      onclick="renderDayModal('${wd.date}')" title="${DAY_FR[i]} ${wd.day_num}">
      <div class="dm-wc-d" style="color:${wclr.text}">${wdays[i]}</div>
      <div class="dm-wc-n" style="color:${wclr.text}">${wd.day_num}</div>
      <div class="dm-wc-s" style="color:${wclr.text}">${pill}</div>
    </div>`;
  });
  wcHTML+='</div>';
  document.getElementById('dm-week-strip').innerHTML=wcHTML;

  // Events
  let evHTML=`<div class="dm-sec-title">Congés / Absences</div>`;
  if(d.events&&d.events.length>0){
    evHTML+=d.events.map(e=>`
      <div class="dm-event-row">
        <div>
          <div class="dm-ev-label">${e.label}</div>
          <div class="dm-ev-meta">${e.date_start} au ${e.date_end}${e.note?' · '+e.note:''}</div>
        </div>
        <button class="btn btn-danger btn-sm" onclick="removeEvent('${e.agent_id}','${e.date_start}','${e.code}')">Suppr.</button>
      </div>`).join('');
  } else {
    evHTML+=`<div style="color:var(--muted);font-size:12px;padding:10px 0">Aucun congé enregistré pour ce jour.</div>`;
  }
  document.getElementById('dm-events').innerHTML=evHTML;

  // Remarque
  const remEl = document.getElementById('dm-remark-area');
  if(remEl) remEl.value = d.remark || '';

  // Shift override buttons — surligner le poste actuel
  const curBase = d.base; // poste effectif (peut être override)
  ['M','S','12H','08H','R'].forEach(s=>{
    const btn=document.getElementById('sb-'+s);
    if(btn) btn.classList.toggle('sb-active', curBase===s);
  });
  // Bouton reset : visible seulement si override actif
  const resetBtn=document.getElementById('sb-reset');
  if(resetBtn) resetBtn.style.display = d.shift_overridden ? 'block' : 'none';
}

async function setShiftOverride(shift) {
  if(!curAgent||!_dayDate) return;
  const r=await fetch('/api/shift_override/'+curAgent+'/'+_dayDate,{
    method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({shift:shift})
  });
  if(r.ok){
    const labels={M:'MATIN 🌅',S:'SOIR 🌆','12H':'12H 🕛','08H':'08H 🕗',R:'REPOS 🛌'};
    toast('Poste changé : '+(labels[shift]||shift),'ok');
    await renderDayModal(_dayDate);
    renderCalendar();
  } else toast('Erreur','error');
}

async function resetShiftOverride() {
  if(!curAgent||!_dayDate) return;
  const r=await fetch('/api/shift_override/'+curAgent+'/'+_dayDate,{method:'DELETE'});
  if(r.ok){
    toast('Poste original restauré','ok');
    await renderDayModal(_dayDate);
    renderCalendar();
  } else toast('Erreur','error');
}

async function saveDayRemark() {
  if(!curAgent || !_dayDate) return;
  const txt = (document.getElementById('dm-remark-area').value || '').trim();
  const r = await fetch('/api/remarks/'+curAgent+'/'+_dayDate, {
    method:'PUT', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({remark: txt})
  });
  if(r.ok){
    toast(txt ? 'Remarque enregistrée' : 'Remarque supprimée', 'ok');
    renderCalendar();
  } else toast('Erreur','error');
}

function openLeaveFromDay() {
  if(!_dayDate) return;
  closeModal('day-modal');
  document.getElementById('leave-start').value=_dayDate;
  document.getElementById('leave-end').value=_dayDate;
  openModal('leave-modal');
}

// ── LEAVE MODAL ──
function openLeaveModal() {
  const today=new Date();
  const ds=today.toISOString().split('T')[0];
  document.getElementById('leave-start').value=ds;
  document.getElementById('leave-end').value=ds;
  openModal('leave-modal');
}

async function submitLeave() {
  const code=document.getElementById('leave-code').value;
  const start=document.getElementById('leave-start').value;
  const end=document.getElementById('leave-end').value;
  const note=document.getElementById('leave-note-text').value;
  if(!curAgent){toast('Sélectionnez un agent','error');return;}
  if(!code||!start||!end){toast('Remplissez tous les champs','error');return;}
  if(start>end){toast('Date fin < date début','error');return;}
  const r=await fetch('/api/events',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent_id:curAgent,code,date_start:start,date_end:end,note})});
  if(r.ok){toast('Congé enregistré');closeModal('leave-modal');renderCalendar();}
  else toast('Erreur lors de l\'enregistrement','error');
}

async function removeEvent(agent,start,code) {
  const r=await fetch('/api/events',{method:'DELETE',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent_id:agent,date_start:start,code})});
  if(r.ok){toast('Congé supprimé');renderCalendar();if(_dayDate) renderDayModal(_dayDate);}
}

// ── MODALS ──
function openModal(id){document.getElementById(id).classList.add('open')}
function closeModal(id){document.getElementById(id).classList.remove('open')}
function openAgentModal(){
  loadAgents();
  const c=document.getElementById('acc-career');
  if(c && _allAgents[curAgent]) c.value = _allAgents[curAgent].career_start || '';
  openModal('agent-modal');
}

// ── TOAST ──
function toast(msg,type='ok'){
  const el=document.getElementById('toast');
  el.textContent=msg; el.className='show'+(type==='error'?' error':'');
  setTimeout(()=>el.className='',2500);
}

// ─────────────── ÉCHANGES DE SERVICE ───────────────
let _exchFilter = 'all';
let _allExchanges = [];

async function renderExchanges() {
  const balArea = document.getElementById('exch-balance-area');
  const listEl  = document.getElementById('exch-list');
  if(!curAgent){
    balArea.innerHTML = '<div style="color:var(--muted);padding:20px 0">Sélectionnez un agent pour voir les échanges.</div>';
    listEl.innerHTML  = ''; return;
  }
  const [exs, balance] = await Promise.all([
    fetch(`/api/exchanges/${curAgent}`).then(r=>r.json()),
    fetch(`/api/exchanges/${curAgent}/balance`).then(r=>r.json()),
  ]);
  _allExchanges = exs;
  renderExchangeBalance(balance);
  renderExchangeList(_allExchanges, _exchFilter);
}

function renderExchangeBalance(balance) {
  const area = document.getElementById('exch-balance-area');
  let html = '<div class="exch-balance-grid">';
  // Hiérarchie
  const hp = balance.hierarchie_pending || 0;
  html += `<div class="exch-balance-card">
    <div class="ebc-title">🏛 Hiérarchie</div>
    <div class="ebc-num" style="color:${hp>0?'var(--orange)':'var(--green)'}">${hp}</div>
    <div class="ebc-sub">compensation${hp>1?'s':''} à récupérer</div>
  </div>`;
  // Collègues
  const cols = balance.collegues || {};
  const names = Object.keys(cols);
  if(names.length===0){
    html += `<div class="exch-balance-card">
      <div class="ebc-title">👥 Collègues</div>
      <div class="ebc-num" style="color:var(--green)">0</div>
      <div class="ebc-sub">aucun échange en cours</div>
    </div>`;
  } else {
    names.forEach(name=>{
      const c   = cols[name];
      const bal = c.balance;
      const bC  = bal>0?'var(--green)':(bal<0?'var(--red)':'var(--muted)');
      const bTxt= bal>0?`me doit ${bal}`:(bal<0?`je dois ${-bal}`:'soldé');
      html += `<div class="exch-balance-card">
        <div class="ebc-title">👤 ${name}</div>
        <div class="ebc-num" style="color:${bC}">${Math.abs(bal)}</div>
        <div class="ebc-sub">${bTxt} service${Math.abs(bal)>1?'s':''}</div>
      </div>`;
    });
  }
  html += '</div>';
  area.innerHTML = html;
}

const POSTE_COLORS={M:'var(--red)',S:'var(--orange)','12H':'#a78bfa','08H':'#2dd4bf',R:'var(--green)'};
function posteTag(p){ if(!p)return''; const c=POSTE_COLORS[p]||'var(--muted)'; return `<span style="font-size:11px;font-weight:800;color:${c};margin-left:4px">${p}</span>`;}

function renderExchangeList(exs, filter) {
  const list = document.getElementById('exch-list');
  const data  = filter==='all' ? exs : exs.filter(e=>e.status===filter);
  if(!data.length){
    list.innerHTML = `<div style="color:var(--muted);text-align:center;padding:30px;background:var(--card);border-radius:10px;border:1px solid var(--border)">Aucun échange enregistré.</div>`;
    return;
  }
  list.innerHTML = data.map(ex=>{
    const isHier = ex.type==='hierarchie';
    const isDone = ex.status==='done';
    const ds     = new Date(ex.date_service+'T00:00:00');
    const dsf    = ds.toLocaleDateString('fr-BE',{day:'2-digit',month:'2-digit',year:'numeric'});
    // Type badge
    const dir    = ex.direction;
    const typeCls= isHier?'hier':(dir==='donne'?'col-donne':'col-recu');
    const typeTxt= isHier?'🏛 Hiérarchie':(dir==='donne'?'↗ J\'ai donné':'↙ J\'ai reçu');
    // Who
    const who    = isHier?'<span style="color:var(--muted);font-style:italic">Hiérarchie</span>'
      :`<span style="font-weight:800">${ex.colleague||'?'}</span>`;
    // Remboursement
    let rembHtml = '';
    if(isDone && ex.date_remboursement){
      const dr  = new Date(ex.date_remboursement+'T00:00:00');
      const drf = dr.toLocaleDateString('fr-BE',{day:'2-digit',month:'2-digit',year:'numeric'});
      rembHtml  = `<span style="font-size:11px;color:var(--green);margin-left:10px">✅ rembours. ${drf}${ex.poste_remboursement?posteTag(ex.poste_remboursement):''}</span>`;
    }
    return `<div class="exch-row">
      <div class="exr-date">${dsf}</div>
      <div class="exr-type ${typeCls}">${typeTxt}</div>
      <div class="exr-who">${who}${posteTag(ex.poste_service)}${rembHtml}</div>
      ${ex.note?`<div class="exr-note" title="${ex.note}">📝 ${ex.note}</div>`:''}
      <div class="exr-status ${ex.status}">${isDone?'✅ Soldé':'⏳ En attente'}</div>
      <div class="exr-actions">
        ${!isDone?`<button class="btn btn-sm btn-green" onclick="markRepaid('${ex.id}')" title="Marquer remboursé">✓ Soldé</button>`:''}
        <button class="btn btn-sm btn-danger" onclick="deleteExchange('${ex.id}')" title="Supprimer">🗑</button>
      </div>
    </div>`;
  }).join('');
}

function filterExchanges(f,btn){
  _exchFilter=f;
  document.querySelectorAll('.exch-filter button').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderExchangeList(_allExchanges,f);
}

function onExTypeChange(){
  const t=document.getElementById('ex-type').value;
  document.getElementById('ex-collegue-section').style.display=t==='collegue'?'block':'none';
}

function openExchangeModal(){
  document.getElementById('ex-type').value          = 'collegue';
  document.getElementById('ex-direction').value     = 'donne';
  document.getElementById('ex-colleague').value     = '';
  document.getElementById('ex-date-service').value  = new Date().toISOString().slice(0,10);
  document.getElementById('ex-poste-service').value = 'M';
  document.getElementById('ex-date-remb').value     = '';
  document.getElementById('ex-poste-remb').value    = '';
  document.getElementById('ex-note').value          = '';
  document.getElementById('ex-collegue-section').style.display='block';
  document.getElementById('exchange-modal').classList.add('open');
}

async function saveExchange(){
  if(!curAgent) return;
  const type = document.getElementById('ex-type').value;
  const colleague = (document.getElementById('ex-colleague').value||'').trim();
  if(type==='collegue' && !colleague){ toast('Nom du collègue requis','error'); return; }
  const dateService = document.getElementById('ex-date-service').value;
  if(!dateService){ toast('Date du service requise','error'); return; }
  const payload = {
    agent_id:            curAgent,
    type,
    direction:           document.getElementById('ex-direction').value,
    date_service:        dateService,
    poste_service:       document.getElementById('ex-poste-service').value,
    colleague,
    date_remboursement:  document.getElementById('ex-date-remb').value||null,
    poste_remboursement: document.getElementById('ex-poste-remb').value||null,
    note:                document.getElementById('ex-note').value.trim(),
  };
  const r = await fetch('/api/exchanges',{
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload)
  });
  if(r.ok){ closeModal('exchange-modal'); toast('Échange enregistré ✅'); renderExchanges(); }
  else toast('Erreur','error');
}

async function markRepaid(eid){
  const today = new Date().toISOString().slice(0,10);
  const dateRemb = prompt('Date de remboursement :', today);
  if(dateRemb===null) return;
  const posteOpts = ['M — Matin','S — Soir','R — Repos',''];
  const posteRemb = prompt('Poste remboursement (M / S / R ou laisser vide) :', '')
  const r = await fetch(`/api/exchanges/${eid}`,{
    method:'PATCH', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      date_remboursement: dateRemb||null,
      poste_remboursement: (posteRemb||'').toUpperCase()||null
    })
  });
  if(r.ok){ toast('Échange soldé ✅'); renderExchanges(); }
  else toast('Erreur','error');
}

async function deleteExchange(eid){
  if(!confirm('Supprimer cet échange de service ?')) return;
  const r = await fetch(`/api/exchanges/${eid}`,{method:'DELETE'});
  if(r.ok){ toast('Supprimé'); renderExchanges(); }
  else toast('Erreur','error');
}

// ── KEYBOARD ──
document.addEventListener('keydown',e=>{if(e.key==='Escape'){
  document.querySelectorAll('.modal-overlay.open').forEach(m=>m.classList.remove('open'));
}});

// ── AUTH PAR AGENT ────────────────────────────────────────────
let _authMode = 'login';
function authMode(m){
  _authMode = m;
  const reg = m === 'register';
  document.getElementById('register-fields').style.display = reg ? 'block' : 'none';
  document.getElementById('auth-submit').textContent = reg ? 'Créer mon compte' : 'Connexion';
  document.getElementById('auth-error').style.display = 'none';
  const tl = document.getElementById('tab-login'), tr = document.getElementById('tab-register');
  tl.style.background = reg ? 'transparent' : '#3b82f6'; tl.style.color = reg ? '#94a3b8' : '#fff';
  tl.style.border = reg ? '1px solid #475569' : 'none';
  tr.style.background = reg ? '#3b82f6' : 'transparent'; tr.style.color = reg ? '#fff' : '#94a3b8';
  tr.style.border = reg ? 'none' : '1px solid #475569';
}
function _authErr(msg){
  const e = document.getElementById('auth-error');
  e.textContent = msg; e.style.display = 'block';
}
async function doAuth(){
  const id  = document.getElementById('auth-id').value.trim().toUpperCase();
  const pin = document.getElementById('auth-pin').value.trim();
  if(!id || !pin){ _authErr('Identifiant et PIN obligatoires'); return; }
  let url, payload;
  if(_authMode === 'register'){
    const name = document.getElementById('auth-name').value.trim();
    if(!name){ _authErr('Nom obligatoire'); return; }
    const r45 = document.getElementById('auth-45').value;
    payload = { id, pin, name,
      offset: parseInt(document.getElementById('auth-offset').value),
      regime_4_5: r45 === '' ? null : parseInt(r45),
      birth_date: document.getElementById('auth-birth').value || null };
    url = '/api/auth/register';
  } else {
    payload = { id, pin };
    url = '/api/auth/login';
  }
  const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const j = await r.json().catch(()=>({}));
  if(r.ok){
    document.getElementById('login-overlay').style.display = 'none';
    document.getElementById('auth-pin').value = '';
    init();
  } else {
    _authErr(j.error || 'Erreur');
  }
}
async function doLogout(){
  await fetch('/api/auth/logout',{method:'POST'});
  location.reload();
}

(async function checkAuth(){
  const me = await fetch('/api/auth/me').then(r=>r.json()).catch(()=>({logged_in:false}));
  if(me.logged_in){
    document.getElementById('login-overlay').style.display = 'none';
    init();
  } else {
    authMode('login');
    document.getElementById('login-overlay').style.display = 'flex';
    document.getElementById('auth-id').focus();
  }
})();
</script>
<script>
/* ── Live-reload : rafraîchit le browser quand le serveur redémarre ── */
(function(){
  let _v=null;
  function check(){
    fetch('/dev-version').then(r=>r.json()).then(d=>{
      if(_v===null){_v=d.v;return;}
      if(d.v!==_v){location.reload();}
    }).catch(()=>{});
  }
  setInterval(check,2000);
})();
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0"
    import socket
    local_ip = socket.gethostbyname(socket.gethostname())
    print("=" * 55)
    print("  HoraireManager — Prison de Namur / SPF Justice")
    print(f"  Local  : http://localhost:{port}")
    print(f"  Reseau : http://{local_ip}:{port}  <-- telephone/tablette")
    print("  Ctrl+C pour arrêter")
    print("=" * 55)
    app.run(host=host, port=port, debug=False, use_reloader=True)
