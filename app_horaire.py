# -*- coding: utf-8 -*-
"""
HoraireManager - Prison de Namur / SPF Justice
Application web de gestion des horaires rotatifs
Lancement: python app_horaire.py  -> http://localhost:5050
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'lib'))

import json, uuid as _uuid
from datetime import date, timedelta
from calendar import monthrange, isleap
from pathlib import Path

from flask import Flask, jsonify, request, render_template_string, Response

from horaire_agent import get_shift, MONTH_NAMES_FR, DAY_NAMES_FR, CYCLE_LEN, ANCHOR
from conges_bosa import LEAVE_CATALOG, get_public_holidays, get_vac_entitlement, get_sick_capital

app = Flask(__name__)
_data_dir = Path(os.environ.get("DATA_DIR", str(Path(__file__).parent)))
DATA_FILE  = _data_dir / "agenda_data.json"

# ─────────────────────── ROOT + PWA ──────────────────────────
@app.route("/")
def index():
    return Response(HTML, mimetype='text/html; charset=utf-8')

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
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
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
    if shift_ov in ("M", "S", "R"):
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

    # ── JOUR 4/5 (priorité sous férié/congé) ─────────────────────────────────
    if regime is not None and code is None and shift_ov is None:
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
    if code is None:
        color = "red" if base == "M" else ("orange" if base == "S" else "green")
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
    return jsonify(load()["agents"])

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Remet toutes les données à zéro — pour transmettre l'app à un collègue."""
    empty = {"agents": {}, "events": [], "reliquats": {}, "capitals": {}, "exchanges": [], "remarks": {}, "shift_overrides": {}}
    save(empty)
    return jsonify({"ok": True})

@app.route("/api/agents", methods=["POST"])
def api_add_agent():
    data = load()
    body = request.json
    aid  = body["id"]
    r45  = body.get("regime_4_5")
    data["agents"][aid] = {
        "name":         body["name"],
        "birth_date":   body.get("birth_date") or None,
        "career_start": body.get("career_start") or None,
        "team_offset":  int(body.get("offset", 0)),
        "regime_4_5":   int(r45) if r45 is not None and r45 != "" else None,
    }
    save(data)
    return jsonify({"ok": True, "id": aid})

@app.route("/api/agents/<aid>", methods=["PATCH"])
def api_patch_agent(aid):
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
    save(data)
    return jsonify({"ok": True, "agent": data["agents"][aid]})

@app.route("/api/agents/<aid>", methods=["DELETE"])
def api_del_agent(aid):
    data = load()
    data["agents"].pop(aid, None)
    data["events"] = [e for e in data["events"] if e["agent_id"] != aid]
    save(data)
    return jsonify({"ok": True})

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
    if shift not in ("M", "S", "R"):
        return jsonify({"error": "Shift invalide (M/S/R)"}), 400
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
        # Jour presté = poste de base M ou S, sans congé/événement posé
        if day_info["base"] in ("M", "S") and day_info["code"] is None:
            worked_days.append(day_info)
    month_name = MONTH_NAMES_FR[month - 1]
    matin_count = sum(1 for d in worked_days if d["base"] == "M")
    soir_count  = sum(1 for d in worked_days if d["base"] == "S")
    day_names_full = {"Lun": "Lundi", "Mar": "Mardi", "Mer": "Mercredi",
                      "Jeu": "Jeudi", "Ven": "Vendredi", "Sam": "Samedi", "Dim": "Dimanche"}
    rows_html = ""
    for i, day in enumerate(worked_days, 1):
        is_matin = day["base"] == "M"
        pill_label = "MATIN" if is_matin else "SOIR"
        pill_bg    = "#dbeafe" if is_matin else "#ffedd5"
        pill_color = "#1e40af" if is_matin else "#9a3412"
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
    shift_hours = {"M": "06:00 – 14:00", "S": "14:00 – 22:00"}.get(info["base"], "Hors service")
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
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex}

/* ── SIDEBAR ── */
#sidebar{width:260px;background:var(--sidebar);display:flex;flex-direction:column;border-right:1px solid var(--border);flex-shrink:0}
#sidebar .logo{padding:24px 20px;border-bottom:1px solid var(--border)}
#sidebar .logo h1{font-size:16px;font-weight:700;color:var(--text);letter-spacing:.5px}
#sidebar .logo p{font-size:11px;color:var(--muted);margin-top:3px}
.nav-section{padding:16px 12px 8px;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:1px}
.nav-btn{display:flex;align-items:center;gap:10px;padding:10px 16px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--muted);transition:.15s;border:none;background:none;width:100%;text-align:left}
.nav-btn:hover,.nav-btn.active{background:var(--card2);color:var(--text)}
.nav-btn svg{width:16px;height:16px;opacity:.7}

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

/* Fonds tintés des cellules */
.c-red   {background:rgba(239,68,68,.14)!important}
.c-orange{background:rgba(249,115,22,.13)!important}
.c-green {background:rgba(34,197,94,.10)!important}
.c-blue  {background:rgba(59,130,246,.14)!important}
.c-purple{background:rgba(168,85,247,.12)!important}

/* Pills plus opaques */
.p-red   {background:rgba(239,68,68,.35);color:#fca5a5;font-weight:900}
.p-orange{background:rgba(249,115,22,.35);color:#fdba74;font-weight:900}
.p-green {background:rgba(34,197,94,.30);color:#86efac;font-weight:900}
.p-blue  {background:rgba(59,130,246,.35);color:#93c5fd;font-weight:900}
.p-purple{background:rgba(168,85,247,.30);color:#d8b4fe;font-weight:900}

/* Numéros colorés par type */
.n-red   .day-num{color:#f87171}
.n-orange .day-num{color:#fb923c}
.n-green  .day-num{color:#4ade80}
.n-blue   .day-num{color:#93c5fd}
.n-purple .day-num{color:#c084fc}
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
   RESPONSIVE — TABLETTE  (≤ 1024px)
══════════════════════════════════════════ */
@media(max-width:1024px){
  #sidebar{width:72px}
  #sidebar .logo p,.nav-section,.agent-section{display:none}
  #sidebar .logo{padding:16px;text-align:center}
  #sidebar .logo h1{font-size:22px}
  .nav-btn{padding:12px 0;justify-content:center;font-size:0;gap:0}
  .nav-btn svg{width:22px;height:22px;opacity:1}
  #mobile-agent-bar{display:flex!important}
  #content{padding:14px;gap:12px}
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
  #topbar{padding:10px 12px;flex-wrap:wrap;gap:6px}
  #topbar h2{font-size:13px}
  .month-nav button{padding:5px 8px;font-size:12px}
  .month-nav span{font-size:13px;min-width:120px}

  /* Barre agent (mobile) */
  #mobile-agent-bar{display:flex!important}

  /* Grille calendrier */
  #content{grid-template-columns:1fr;padding:6px;gap:8px}
  #right-panel{display:none}
  #right-panel.panel-open{display:flex!important;flex-direction:column}
  .cal-grid{gap:3px}
  .cal-header{font-size:10px;padding:6px 0;font-weight:800}
  .cal-day{min-height:68px;padding:5px 5px 5px 9px;gap:2px}
  .cal-day.today .day-num{width:32px;height:32px;font-size:18px}
  .day-num{font-size:22px;font-weight:900}
  .shift-pill{font-size:9px;padding:2px 5px}
  .day-abbr{font-size:9px}
  .day-reason{font-size:9px;-webkit-line-clamp:1}
  .day-remark{font-size:8px}
  /* Topbar mobile amélioré */
  #topbar{padding:8px 10px;gap:4px}
  #topbar h2{font-size:14px;font-weight:800}
  .month-nav button{padding:7px 12px;font-size:14px;min-width:40px;border-radius:8px}
  .month-nav span{font-size:14px;min-width:100px}
  /* Entitlements scrollable */
  #entitlements-bar{padding:6px 8px}
  /* Soldes congés bouton flottant */
  #btn-panel-mobile{font-size:10px;padding:6px 8px}

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

/* entitlements bar scroll sur petit écran */
@media(max-width:640px){
  #entitlements-content{overflow-x:auto;flex-wrap:nowrap;-webkit-overflow-scrolling:touch;padding-bottom:4px}
  #entitlements-content > div{min-width:140px!important;flex:0 0 auto!important}
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
  .cal-day{min-height:58px;padding:4px 3px 4px 6px}
  .day-num{font-size:18px}
  .shift-pill{font-size:8px;padding:1px 4px}
  .day-abbr{display:none}
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
.shift-btn.sb-repos{border-color:#22c55e;color:#86efac}
.shift-btn.sb-reset{border-color:var(--muted);color:var(--muted);font-size:11px}
.shift-btn.sb-active{opacity:1;font-size:14px}
.shift-btn:not(.sb-active){opacity:.55}
.dm-remark-section{padding:12px 16px;border-top:1px solid var(--border)}
.dm-remark-label{font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between}
.dm-remark-save{font-size:11px;padding:4px 12px;border-radius:6px;border:none;background:var(--accent);color:#fff;cursor:pointer;font-weight:600}
.dm-remark-save:hover{background:#2563eb}
</style>
</head>
<body>

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
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75"/></svg>
    Agents
  </button>
  <button class="nav-btn" id="btn-panel-mobile" style="display:none" onclick="toggleMobilePanel()">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 010 14.14M4.93 19.07a10 10 0 010-14.14"/><path d="M15.54 8.46a5 5 0 010 7.07M8.46 15.54a5 5 0 010-7.07"/></svg>
    Réglages
  </button>

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
      <button onclick="printMonth()" title="Imprimer les jours prestés (A4 PDF)" style="margin-left:8px;background:#1e40af;color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px">🖨️ Imprimer</button>
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
          <button id="btn-ent-details" onclick="toggleEntDetails()" style="background:var(--card2);border:1px solid var(--border);color:var(--muted);border-radius:5px;padding:3px 10px;cursor:pointer;font-size:11px;font-weight:700">Détails ▾</button>
        </div>
        <div id="entitlements-content" style="display:flex;gap:0;border-radius:8px;overflow:hidden;border:1px solid var(--border)"></div>
        <div id="ent-details" style="display:none;margin-top:10px"></div>
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
        </div>
      </div>
    </div>
  </div>

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
    <h2>Gestion des agents <span class="close" onclick="closeModal('agent-modal')">✕</span></h2>
    <div id="agent-list" style="margin-bottom:16px"></div>
    <hr style="border-color:var(--border);margin:16px 0">
    <div style="font-size:13px;font-weight:600;margin-bottom:12px">Nouvel agent</div>
    <div class="form-row">
      <div class="form-group"><label>ID</label><input id="na-id" placeholder="EX: TSE"></div>
      <div class="form-group"><label>Nom</label><input id="na-name" placeholder="Prénom NOM"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>Date de naissance</label><input type="date" id="na-birth" max="2007-12-31"></div>
      <div class="form-group">
        <label>Équipe (offset)</label>
        <select id="na-offset">
          <option value="0">Équipe 4 (0j)</option>
          <option value="7">Équipe 5 (7j)</option>
          <option value="14">Équipe 6 (14j)</option>
          <option value="21">Équipe 7 (21j)</option>
          <option value="28">Équipe 8 (28j)</option>
          <option value="35">Équipe 1 (35j)</option>
          <option value="42">Équipe 2 (42j)</option>
          <option value="49">Équipe 3 (49j)</option>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>Date de début de carrière (SPF Justice)</label>
      <input type="date" id="na-career" max="2025-12-31">
      <div class="form-note">Sert au calcul du capital maladie (21j × années de service)</div>
    </div>
    <div class="form-group">
      <label>Régime 4/5 — jour de repos fixe (lun-ven)</label>
      <select id="na-45">
        <option value="">— Pas de régime 4/5 —</option>
        <option value="0">Lundi</option>
        <option value="1">Mardi</option>
        <option value="2">Mercredi</option>
        <option value="3">Jeudi</option>
        <option value="4">Vendredi</option>
      </select>
    </div>
    <div class="modal-footer" style="justify-content:space-between">
      <button class="btn btn-danger btn-sm" onclick="resetAllData()" title="Remet tout à zéro pour transmettre à un collègue">🗑 Remettre à zéro</button>
      <div style="display:flex;gap:8px">
        <button class="btn" onclick="closeModal('agent-modal')" style="background:var(--card2)">Fermer</button>
        <button class="btn btn-primary" onclick="addAgent()">Ajouter</button>
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
let curView  = 'calendar';
let catalog  = {};
let selDay   = null;
let viewBase = false;   // true = masque le 4/5, affiche le planning de base
const MN = ['Janvier','Février','Mars','Avril','Mai','Juin','Juillet','Août','Septembre','Octobre','Novembre','Décembre'];

// ── INIT ──
async function init() {
  catalog = await fetch('/api/leaves_catalog').then(r=>r.json());
  populateCatalog();
  await loadAgents();
  showView('calendar');
  gotoToday();
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
async function loadAgents() {
  const agents = await fetch('/api/agents').then(r=>r.json());
  const sel  = document.getElementById('agent-select');
  const selM = document.getElementById('agent-select-mobile');
  const prev = sel.value || selM.value;
  [sel, selM].forEach(s=>{
    s.innerHTML = '<option value="">— Sélectionner —</option>';
    Object.entries(agents).forEach(([id,a])=>{
      const o = document.createElement('option');
      o.value=id; o.textContent=a.name; s.appendChild(o);
    });
    if(prev && agents[prev]) s.value = prev;
    else if(Object.keys(agents).length>0) s.value = Object.keys(agents)[0];
  });
  curAgent = sel.value || selM.value;
  updateAgentInfo(agents);
  renderAgentList(agents);
  if(curAgent) refresh();
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

const WD_FR=['Lundi','Mardi','Mercredi','Jeudi','Vendredi'];
const WD_SHORT=['Lun','Mar','Mer','Jeu','Ven'];

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
  info += `Offset: ${a.team_offset}j`;
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

function renderAgentList(agents) {
  const el = document.getElementById('agent-list');
  if(Object.keys(agents).length===0){el.innerHTML='<p style="color:var(--muted);font-size:12px">Aucun agent enregistré.</p>';return;}
  el.innerHTML = Object.entries(agents).map(([id,a])=>{
    let meta = id;
    if(a.birth_date) {
      const age = new Date().getFullYear() - parseInt(a.birth_date.split('-')[0]);
      meta += ` · ${age} ans (né ${a.birth_date})`;
    } else if(a.age) meta += ` · ${a.age} ans`;
    meta += ` · offset ${a.team_offset}j`;
    if(a.career_start) meta += ` · carrière depuis ${a.career_start}`;
    const warn = !a.birth_date ? ' <span style="color:#fb923c;font-size:10px">⚠ Ajouter date naissance</span>' : '';
    return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid var(--border)">
      <div>
        <div style="font-size:13px;font-weight:600">${a.name}${warn}</div>
        <div style="font-size:11px;color:var(--muted)">${meta}</div>
      </div>
      <button class="btn btn-danger btn-sm" onclick="deleteAgent('${id}')">Suppr.</button>
    </div>`;
  }).join('');
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
  if(!confirm('Supprimer cet agent et tous ses congés ?')) return;
  await fetch(`/api/agents/${id}`,{method:'DELETE'});
  toast('Agent supprimé');
  await loadAgents();
}

function printMonth() {
  if(!curAgent){ toast('Sélectionnez un agent avant d\'imprimer','error'); return; }
  window.open('/print/'+curAgent+'/'+curYear+'/'+curMonth, '_blank');
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
  refresh();
}
function prevPeriod() {
  if(curView==='calendar'){ if(--curMonth<1){curMonth=12;curYear--;} }
  else curYear--;
  refresh();
}
function nextPeriod() {
  if(curView==='calendar'){ if(++curMonth>12){curMonth=1;curYear++;} }
  else curYear++;
  refresh();
}
function showView(v) {
  curView=v;
  document.getElementById('content').style.display          = v==='calendar'  ? 'grid'  : 'none';
  document.getElementById('annual-content').style.display   = v==='annuel'    ? 'block' : 'none';
  document.getElementById('exchanges-content').style.display= v==='exchanges' ? 'block' : 'none';
  const titles={calendar:'Calendrier mensuel',annuel:'Vue annuelle',exchanges:'Échanges de service'};
  document.getElementById('view-title').textContent = titles[v]||v;
  document.querySelectorAll('.nav-btn').forEach((b,i)=>{
    b.classList.toggle('active',
      (i===0&&v==='calendar')||(i===1&&v==='annuel')||(i===2&&v==='exchanges'));
  });
  refresh();
}
function refresh() {
  if(curView==='calendar')  renderCalendar();
  else if(curView==='annuel') renderAnnual();
  else if(curView==='exchanges') renderExchanges();
}

// ── CALENDAR ──
async function renderCalendar() {
  document.getElementById('period-label').textContent = `${MN[curMonth-1]} ${curYear}`;
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
let _entDetailsOpen = false;

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
  let html = '<div style="flex:1;min-width:170px;padding:10px 14px;border-right:1px solid var(--border)">'
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
    html += '<div style="flex:1;min-width:170px;padding:10px 14px;border-right:1px solid var(--border)">'
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
    html += `<div style="flex:0 0 auto;min-width:120px;padding:10px 14px;border-right:1px solid var(--border)">
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
  html += `<div style="flex:0 0 auto;min-width:110px;padding:10px 14px">
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
      pillTxt=code==='PONT'?'PONT':'FÉRIÉ';
      reasonTxt=day.label||'';
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
};
const SHIFT_FULL={M:'MATIN',S:'SOIR',R:'REPOS','36':'REPOS-36','38':'REPOS-38'};
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
  ['M','S','R'].forEach(s=>{
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
    const labels={M:'MATIN 🌅',S:'SOIR 🌆',R:'REPOS 🛌'};
    toast('Poste changé : '+labels[shift],'ok');
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
function openAgentModal(){loadAgents();openModal('agent-modal')}

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

const POSTE_COLORS={M:'var(--red)',S:'var(--orange)',R:'var(--green)'};
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

init();
</script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print("=" * 55)
    print("  HoraireManager — Prison de Namur / SPF Justice")
    print(f"  http://localhost:{port}")
    print("  Ctrl+C pour arrêter")
    print("=" * 55)
    app.run(host=host, port=port, debug=False)
