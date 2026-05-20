# -*- coding: utf-8 -*-
"""
AgendaAgent - Gestionnaire d'horaire complet
Prison de Namur / SPF Justice - Equipe 4 (ou toute equipe)
Integre : horaire rotatif 56j + jours feries BOSA + tous types de conges
"""

import json
import os
from datetime import date, timedelta
from pathlib import Path
from horaire_agent import get_shift, MONTH_NAMES_FR, DAY_NAMES_FR, CYCLE_LEN, ANCHOR
from conges_bosa import LEAVE_CATALOG, get_public_holidays

# ============================================================
# FICHIER DE DONNEES (JSON persistant)
# ============================================================

DATA_FILE = Path(__file__).parent / "agenda_data.json"


def _load() -> dict:
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {
        "agents": {},       # agent_id -> {name, age, team_offset, year_start}
        "events": [],       # liste de tous les evenements
    }


def _save(data: dict):
    DATA_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8"
    )


# ============================================================
# GESTION DES AGENTS
# ============================================================

def add_agent(agent_id: str, name: str, age: int, team_offset: int = 0, year_start: int = 2026):
    """Enregistre un agent dans le systeme."""
    data = _load()
    data["agents"][agent_id] = {
        "name": name,
        "age": age,
        "team_offset": team_offset,
        "year_start": year_start,
    }
    _save(data)
    print(f"Agent enregistre: {name} (ID={agent_id}, age={age}, offset={team_offset}j)")


def get_vacation_days(age: int) -> int:
    """Retourne le nb de jours de vacances selon l'age (BOSA)."""
    catalog = LEAVE_CATALOG["VAC"]["days_by_age"]
    for (low, high), days in catalog.items():
        if low <= age <= high:
            return days
    return 26


# ============================================================
# GESTION DES EVENEMENTS
# ============================================================

def add_event(agent_id: str, leave_code: str,
              date_start: date, date_end: date,
              note: str = "", half_day: str = None):
    """
    Ajoute un evenement (conge, maladie, etc.) pour un agent.
    half_day: 'AM' (matin) ou 'PM' (apres-midi) pour demi-journee.
    """
    data = _load()

    if agent_id not in data["agents"]:
        raise ValueError(f"Agent '{agent_id}' inconnu. Creez-le d'abord avec add_agent().")
    if leave_code not in LEAVE_CATALOG:
        codes = list(LEAVE_CATALOG.keys())
        raise ValueError(f"Code '{leave_code}' inconnu. Codes valides: {codes}")

    event = {
        "agent_id": agent_id,
        "code": leave_code,
        "label": LEAVE_CATALOG[leave_code]["label"],
        "category": LEAVE_CATALOG[leave_code]["category"],
        "date_start": date_start.isoformat(),
        "date_end": date_end.isoformat(),
        "note": note,
        "half_day": half_day,
        "created": date.today().isoformat(),
    }
    data["events"].append(event)
    _save(data)

    delta = (date_end - date_start).days + 1
    print(f"Evenement ajoute: [{leave_code}] {LEAVE_CATALOG[leave_code]['label']}")
    print(f"  Agent: {data['agents'][agent_id]['name']}")
    print(f"  Periode: {date_start} -> {date_end} ({delta} jour(s))")
    if note:
        print(f"  Note: {note}")


def remove_event(agent_id: str, date_start: date, leave_code: str = None):
    """Supprime un evenement par agent + date de debut (+ code optionnel)."""
    data = _load()
    before = len(data["events"])
    data["events"] = [
        e for e in data["events"]
        if not (e["agent_id"] == agent_id
                and e["date_start"] == date_start.isoformat()
                and (leave_code is None or e["code"] == leave_code))
    ]
    removed = before - len(data["events"])
    _save(data)
    print(f"{removed} evenement(s) supprime(s).")


# ============================================================
# CALENDRIER COMPLET D'UN AGENT
# ============================================================

def get_day_status(d: date, agent_id: str, data: dict) -> dict:
    """Retourne le statut complet d'une journee pour un agent."""
    agent = data["agents"][agent_id]
    offset = agent["team_offset"]

    # Shift de base (rotation)
    base_shift = get_shift(d, offset)

    # Jours feries de l'annee
    holidays = {h[0]: (h[1], h[2]) for h in get_public_holidays(d.year)}

    # Evenements de l'agent ce jour
    day_events = []
    for e in data["events"]:
        if e["agent_id"] != agent_id:
            continue
        es = date.fromisoformat(e["date_start"])
        ee = date.fromisoformat(e["date_end"])
        if es <= d <= ee:
            day_events.append(e)

    # Statut effectif
    effective = base_shift
    leave_code = None
    leave_label = None

    if d in holidays:
        effective = holidays[d][0]   # FERIE ou PONT
        leave_code = holidays[d][0]
        leave_label = holidays[d][1]

    if day_events:
        ev = day_events[0]           # priorite premier evenement
        effective = ev["code"]
        leave_code = ev["code"]
        leave_label = ev["label"]

    return {
        "date": d,
        "day_name": DAY_NAMES_FR[d.weekday()],
        "base_shift": base_shift,
        "effective": effective,
        "is_worked": base_shift in ("M", "S") and leave_code is None and d not in holidays,
        "is_holiday": d in holidays,
        "holiday_label": holidays.get(d, (None, None))[1],
        "leave_code": leave_code,
        "leave_label": leave_label,
        "events": day_events,
    }


# ============================================================
# SOLDES DE CONGES
# ============================================================

def compute_balance(agent_id: str, year: int) -> dict:
    """Calcule les soldes de conges pour un agent sur une annee."""
    data = _load()
    if agent_id not in data["agents"]:
        raise ValueError(f"Agent '{agent_id}' inconnu.")

    agent = data["agents"][agent_id]
    age = agent["age"]

    # Droits initiaux
    vac_total = get_vacation_days(age)
    sick_total = 21   # jours maladie garantis/an

    # Compteurs d'utilisation
    counters = {}
    for code in LEAVE_CATALOG:
        counters[code] = 0

    for e in data["events"]:
        if e["agent_id"] != agent_id:
            continue
        es = date.fromisoformat(e["date_start"])
        ee = date.fromisoformat(e["date_end"])
        if es.year != year and ee.year != year:
            continue
        # Compter les jours (simplification: tous les jours calendaires)
        delta = (ee - es).days + 1
        code = e["code"]
        counters[code] = counters.get(code, 0) + delta

    vac_used = counters.get("VAC", 0)
    sick_used = counters.get("MAL", 0)

    return {
        "agent": agent["name"],
        "year": year,
        "age": age,
        "vacances": {
            "droit": vac_total,
            "utilise": vac_used,
            "solde": vac_total - vac_used,
        },
        "maladie": {
            "droit_annuel": sick_total,
            "utilise": sick_used,
            "solde": sick_total - sick_used,
        },
        "circonstances": {k: v for k, v in counters.items()
                          if LEAVE_CATALOG.get(k, {}).get("category") == "CIRCONSTANCE" and v > 0},
        "detail": {k: v for k, v in counters.items() if v > 0},
    }


# ============================================================
# AFFICHAGE DU CALENDRIER MENSUEL
# ============================================================

def print_month(agent_id: str, year: int, month: int):
    """Affiche le calendrier mensuel complet d'un agent."""
    from calendar import monthrange
    data = _load()

    if agent_id not in data["agents"]:
        print(f"Agent '{agent_id}' inconnu.")
        return

    agent = data["agents"][agent_id]
    days_in_month = monthrange(year, month)[1]
    month_name = MONTH_NAMES_FR[month - 1]

    print(f"\n{'='*65}")
    print(f"  {month_name} {year} | {agent['name']} | Offset={agent['team_offset']}j")
    print(f"{'='*65}")
    print(f"  {'Jour':<6} {'Date':<12} {'Base':<6} {'Effectif':<14} {'Commentaire'}")
    print(f"  {'-'*60}")

    worked = rest = holidays = leaves = 0
    for day in range(1, days_in_month + 1):
        d = date(year, month, day)
        status = get_day_status(d, agent_id, data)

        base = status["base_shift"]
        eff = status["effective"]

        # Formatage
        flag = ""
        comment = ""
        if status["is_holiday"]:
            flag = "[F]"
            comment = status["holiday_label"] or ""
            holidays += 1
        elif status["leave_code"]:
            cat = LEAVE_CATALOG.get(status["leave_code"], {}).get("category", "")
            flag = f"[{status['leave_code'][:4]}]"
            comment = status["leave_label"] or ""
            leaves += 1
        elif base in ("M", "S"):
            flag = ""
            worked += 1
        else:
            rest += 1

        # Couleur via prefixe texte
        marker = "*" if base in ("M","S") and eff not in ("M","S") else " "
        print(f"  {marker} {status['day_name']:<4} {d.strftime('%d/%m/%Y'):<12} {base:<6} {eff:<14} {flag} {comment}")

    print(f"  {'-'*60}")
    print(f"  Travail: {worked}j | Repos-cycle: {rest}j | Feries: {holidays}j | Conges: {leaves}j")

    # Soldes rapides
    bal = compute_balance(agent_id, year)
    print(f"\n  SOLDES {year}: VAC={bal['vacances']['solde']}/{bal['vacances']['droit']}j restants")
    print(f"           MAL={bal['maladie']['solde']}/{bal['maladie']['droit_annuel']}j restants")


# ============================================================
# RAPPORT ANNUEL
# ============================================================

def print_year_report(agent_id: str, year: int):
    """Rapport annuel complet d'un agent."""
    from calendar import monthrange, isleap
    data = _load()

    if agent_id not in data["agents"]:
        print(f"Agent '{agent_id}' inconnu.")
        return

    agent = data["agents"][agent_id]
    offset = agent["team_offset"]

    print(f"\n{'='*70}")
    print(f"RAPPORT ANNUEL {year} | {agent['name']} | Equipe offset={offset}j")
    print(f"{'='*70}")

    holidays_dates = {h[0]: h[2] for h in get_public_holidays(year)}

    total_M = total_S = total_R = total_ferie = total_leave = 0
    total_vac = total_mal = 0

    days_in_year = 366 if isleap(year) else 365
    for day_num in range(days_in_year):
        d = date(year, 1, 1) + timedelta(days=day_num)
        status = get_day_status(d, agent_id, data)
        base = status["base_shift"]
        eff = status["effective"]

        if d in holidays_dates:
            total_ferie += 1
        elif status["leave_code"]:
            total_leave += 1
            if status["leave_code"] == "VAC":
                total_vac += 1
            elif status["leave_code"] == "MAL":
                total_mal += 1
        elif base == "M":
            total_M += 1
        elif base == "S":
            total_S += 1
        else:
            total_R += 1

    bal = compute_balance(agent_id, year)

    print(f"\n  REPARTITION DES JOURS:")
    print(f"    Travail MATIN  : {total_M:3d}j")
    print(f"    Travail SOIR   : {total_S:3d}j")
    print(f"    Repos (cycle)  : {total_R:3d}j")
    print(f"    Jours feries   : {total_ferie:3d}j")
    print(f"    Conges divers  : {total_leave:3d}j  (dont VAC={total_vac} MAL={total_mal})")
    print(f"    TOTAL          : {total_M+total_S+total_R+total_ferie+total_leave:3d}j")

    print(f"\n  SOLDES CONGES {year}:")
    v = bal["vacances"]
    m = bal["maladie"]
    print(f"    Vacances : {v['utilise']:2d}/{v['droit']:2d}j utilises | {v['solde']:2d}j restants")
    print(f"    Maladie  : {m['utilise']:2d}/{m['droit_annuel']:2d}j utilises | {m['solde']:2d}j restants")

    if bal["circonstances"]:
        print(f"\n  CONGES DE CIRCONSTANCE UTILISES:")
        for code, days in bal["circonstances"].items():
            print(f"    {code:<25}: {days}j")

    print(f"\n  JOURS FERIES {year} avec poste de travail:")
    for d, label in [(d,l) for d,l in holidays_dates.items() if d.year == year]:
        sh = get_shift(d, offset)
        if sh in ("M","S"):
            status = get_day_status(d, agent_id, data)
            eff = status["effective"]
            override = f" -> remplace par [{eff}]" if eff != sh else ""
            print(f"    {d.strftime('%d/%m')} {DAY_NAMES_FR[d.weekday()]} {sh} - {label}{override}")


# ============================================================
# LISTE DES CODES DE CONGE
# ============================================================

def list_leave_codes():
    """Affiche tous les codes de conge disponibles."""
    print(f"\n{'='*75}")
    print("CODES DE CONGE DISPONIBLES (BOSA - Fonction publique federale)")
    print(f"{'='*75}")
    current_cat = None
    for code, info in sorted(LEAVE_CATALOG.items(), key=lambda x: x[1]["category"]):
        cat = info["category"]
        if cat != current_cat:
            print(f"\n  -- {cat} --")
            current_cat = cat
        days_info = f"{info.get('days','variable')}j" if info.get('days') else "variable"
        frac = "frac." if info.get("fractional") else "bloc"
        print(f"  [{code:<20}] {info['label']:<45} {days_info:<10} {frac}")
    print()


# ============================================================
# CLI INTERACTIF
# ============================================================

if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="AgendaAgent - Gestion horaires Prison de Namur")
    sub = parser.add_subparsers(dest="cmd")

    # add-agent
    p1 = sub.add_parser("add-agent", help="Enregistrer un agent")
    p1.add_argument("--id",     required=True)
    p1.add_argument("--name",   required=True)
    p1.add_argument("--age",    type=int, required=True)
    p1.add_argument("--offset", type=int, default=0)

    # add-leave
    p2 = sub.add_parser("add-leave", help="Ajouter un conge/absence")
    p2.add_argument("--agent",  required=True)
    p2.add_argument("--code",   required=True)
    p2.add_argument("--start",  required=True, help="YYYY-MM-DD")
    p2.add_argument("--end",    required=True, help="YYYY-MM-DD")
    p2.add_argument("--note",   default="")

    # remove-leave
    p3 = sub.add_parser("remove-leave", help="Supprimer un conge")
    p3.add_argument("--agent",  required=True)
    p3.add_argument("--start",  required=True)
    p3.add_argument("--code",   default=None)

    # month
    p4 = sub.add_parser("month", help="Calendrier mensuel")
    p4.add_argument("--agent",  required=True)
    p4.add_argument("--year",   type=int, required=True)
    p4.add_argument("--month",  type=int, required=True)

    # year-report
    p5 = sub.add_parser("year-report", help="Rapport annuel")
    p5.add_argument("--agent",  required=True)
    p5.add_argument("--year",   type=int, required=True)

    # codes
    p6 = sub.add_parser("codes", help="Liste des codes de conge")

    # balance
    p7 = sub.add_parser("balance", help="Solde conges")
    p7.add_argument("--agent",  required=True)
    p7.add_argument("--year",   type=int, required=True)

    args = parser.parse_args()

    if args.cmd == "add-agent":
        add_agent(args.id, args.name, args.age, args.offset)
    elif args.cmd == "add-leave":
        add_event(args.agent, args.code,
                  date.fromisoformat(args.start),
                  date.fromisoformat(args.end),
                  args.note)
    elif args.cmd == "remove-leave":
        remove_event(args.agent,
                     date.fromisoformat(args.start),
                     args.code)
    elif args.cmd == "month":
        print_month(args.agent, args.year, args.month)
    elif args.cmd == "year-report":
        print_year_report(args.agent, args.year)
    elif args.cmd == "codes":
        list_leave_codes()
    elif args.cmd == "balance":
        bal = compute_balance(args.agent, args.year)
        print(json.dumps(bal, ensure_ascii=False, indent=2))
    else:
        parser.print_help()
