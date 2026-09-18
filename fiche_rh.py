# -*- coding: utf-8 -*-
"""
Fiche RH — Prison de Namur / SPF Justice
Parseur du PDF « FICHE DE CONGE (Après RT) » (export Crystal Reports de la DG EPI)
et rapprochement avec les données d'HoraireManager.

Module autonome : aucune dépendance vers app_horaire.py. Il ne lit ni n'écrit
agenda_data.json — l'app appelle `parse_pdf()` puis stocke le résultat.

Structure d'une fiche (une par agent, 3 pages) :
  - rubriques « 5 colonnes » : Réserve / Report / Droit / Pris / Solde
      CONGE, FERIES, COMPENSATION, JOURS DE PONT, REPOS 38H, REPOS 36H, REPOS
      + ajustements éventuels (Révision de contrat, Réduction Absence,
        Réduction 19 j. abs) — Solde = Réserve + Report + Droit − Pris − Réduction
  - rubriques « 3 colonnes » : EPARGNE-TEMPS (Epargne/Pris/Solde),
      FORMATION et PROMO. SOCIALE (Droit/Pris/Solde en hh:mm)
  - rubriques « total » : MALADIE, DON DE SANG, CONGE SYNDICAL, ABSENCE ILLEGALE,
      ACCIDENT DE TRAVAIL, force majeure, motif impérieux (45 j.)
  - HEURES SUPP. : Report N-1 / Réserve / Solde final + mouvements ±hh:mm
      Solde final = Réserve + Report N-1 + Σ mouvements
Une rubrique inconnue (évolution future du PDF) est conservée brute dans
`inconnues` — jamais une erreur.
"""
import re
from datetime import date

SECTIONS_5 = ["CONGE", "FERIES", "COMPENSATION", "JOURS DE PONT",
              "REPOS 38H", "REPOS 36H", "REPOS"]
SECTIONS_3 = ["EPARGNE-TEMPS", "FORMATION", "PROMO. SOCIALE"]
SECTIONS_1 = ["ACCIDENT DE TRAVAIL", "MALADIE", "DON DE SANG", "CONGE SYNDICAL",
              "ABSENCE ILLEGALE",
              "CONGE EXCEPTIONNEL POUR CAUSE DE FORCE MAJEURE",
              "CONGE POUR MOTIF IMPERIEUX D'ORDRE FAMILIAL (45 J.)"]
SECTION_HS = "HEURES SUPP."
KNOWN_SECTIONS = sorted(SECTIONS_5 + SECTIONS_3 + SECTIONS_1 + [SECTION_HS],
                        key=len, reverse=True)

# Libellés lisibles + correspondance avec les codes de congé de l'app
# (None = pas d'équivalent dans HoraireManager : affichage seul)
RUBRIQUES_INFO = {
    "CONGE": {"label": "Congé annuel de vacances", "app_codes": ["VAC"]},
    "EPARGNE-TEMPS": {"label": "Épargne-temps (jours épargnés)", "app_codes": None},
    "FERIES": {"label": "Fériés travaillés (récupération)", "app_codes": None},
    "COMPENSATION": {"label": "Compensation fériés (week-end)", "app_codes": None},
    "JOURS DE PONT": {"label": "Jours de pont", "app_codes": None},
    "REPOS 38H": {"label": "Repos 38h", "app_codes": ["__38"]},
    "REPOS 36H": {"label": "Repos 36h", "app_codes": ["__36"]},
    "REPOS": {"label": "Repos de cycle", "app_codes": None},
    "FORMATION": {"label": "Formation (heures)", "app_codes": None},
    "PROMO. SOCIALE": {"label": "Promotion sociale (heures)", "app_codes": None},
    "ACCIDENT DE TRAVAIL": {"label": "Accident de travail", "app_codes": ["ACC_TRAV"]},
    "MALADIE": {"label": "Maladie", "app_codes": ["MAL", "MSC", "MAL_LONG"]},
    "DON DE SANG": {"label": "Don de sang", "app_codes": ["CIRC_DON"]},
    "CONGE SYNDICAL": {"label": "Congé syndical", "app_codes": ["SYNDI"]},
    "ABSENCE ILLEGALE": {"label": "Absence illégale", "app_codes": None},
    "CONGE EXCEPTIONNEL POUR CAUSE DE FORCE MAJEURE": {"label": "Congé exceptionnel — force majeure", "app_codes": None},
    "CONGE POUR MOTIF IMPERIEUX D'ORDRE FAMILIAL (45 J.)": {"label": "Motif impérieux d'ordre familial (45 j.)", "app_codes": None},
    SECTION_HS: {"label": "Heures supplémentaires", "app_codes": None},
}

_NUM = r"(-?\d+,\d{2}) j"
_HMS = r"(-?\d+h\d{2}m)"
_DATE = r"(\d{2}/\d{2}/\d{4})"
_RE_NOM = re.compile(r"Nom\s*:\s*(.+?)\s+Numéro de matricule\s*:\s*(\d+)")
_RE_PERIOD = re.compile(r"Situation du\s+" + _DATE + r"\s+au\s+" + _DATE)
_RE_PRINTED = re.compile(r"Date d'impression\s*:\s*" + _DATE)
_RE_DATE_DAYS = re.compile(r"\[\d+\]\s*" + _DATE + r"(?:\s+" + _NUM + r")?")
_RE_HS_MOV = re.compile(r"\[\d+\]\s*" + _DATE + r"\s*:\s*" + _HMS)
_RE_ADJ = re.compile(r"(Révision de contrat|Réduction Absence|Réduction 19 j\. abs)\s*\n?\s*" + _NUM)
# Titre de rubrique inconnu : MAJUSCULES suivies d'un mot-colonne connu
_RE_UNKNOWN = re.compile(r"^([A-ZÉÈÀÇ'()./0-9 -]{4,}?)\s+(Réserve|Report|Droit|Pris|Total|Epargne)\b")
_RE_NUM_ONLY = re.compile(_NUM)
_RE_HMS_ONLY = re.compile(_HMS)


def _num(s):
    return float(s.replace(",", "."))


def _iso(d):
    dd, mm, yy = d.split("/")
    return f"{yy}-{mm}-{dd}"


def hm_to_min(s):
    """'−07h55m' → −475 ; '01h18m' → 78."""
    sign = -1 if s.startswith("-") else 1
    h, m = s.lstrip("-").rstrip("m").split("h")
    return sign * (int(h) * 60 + int(m))


def min_to_hm(m):
    """−475 → '−07h55' ; 78 → '01h18'."""
    sign = "-" if m < 0 else ""
    m = abs(int(m))
    return f"{sign}{m // 60:02d}h{m % 60:02d}"


def _section_start(line):
    for name in KNOWN_SECTIONS:
        if line.startswith(name + " ") or line.strip() == name:
            return name, True
    m = _RE_UNKNOWN.match(line)
    if m:
        return m.group(1).strip(), False
    return None, False


def _empty_rubrique():
    return {"dates": [], "ajustements": {}}


def parse_pages(pages):
    """Analyse une liste de textes de pages (une chaîne par page).
    Retourne {"agents": {matricule: fiche}, "inconnues": [...]}."""
    agents = {}
    for page in pages:
        m = _RE_NOM.search(page)
        if not m:
            continue
        nom, matricule = m.group(1).strip(), m.group(2)
        ag = agents.get(matricule)
        if ag is None:
            per = _RE_PERIOD.search(page)
            pr = _RE_PRINTED.search(page)
            ag = agents[matricule] = {
                "nom": nom, "matricule": matricule,
                "periode": [_iso(per.group(1)), _iso(per.group(2))] if per else [None, None],
                "imprimee_le": _iso(pr.group(1)) if pr else None,
                "rubriques": {}, "inconnues": [], "_cur": None,
            }
        for line in page.splitlines():
            sec, known = _section_start(line)
            if sec:
                ag["_cur"] = sec
                if known:
                    rub = ag["rubriques"].setdefault(sec, _empty_rubrique())
                    rub["_blob"] = rub.get("_blob", "") + line + "\n"
                else:
                    ag["inconnues"].append({"titre": sec, "texte": line})
                    ag["_cur"] = ("?", len(ag["inconnues"]) - 1)
            elif ag["_cur"]:
                cur = ag["_cur"]
                if isinstance(cur, tuple):
                    ag["inconnues"][cur[1]]["texte"] += "\n" + line
                else:
                    ag["rubriques"][cur]["_blob"] += line + "\n"

    for ag in agents.values():
        ag.pop("_cur", None)
        for sec, rub in ag["rubriques"].items():
            blob = rub.pop("_blob", "")
            head = blob.split("Pris le", 1)[0]
            head_nums = re.split(r"Révision|Réduction", head)[0]
            if sec in SECTIONS_5:
                nums = _RE_NUM_ONLY.findall(head_nums)
                if len(nums) >= 5:
                    rub.update(dict(zip(["reserve", "report", "droit", "pris", "solde"],
                                        map(_num, nums[:5]))))
            elif sec in SECTIONS_3:
                nums = _RE_NUM_ONLY.findall(head)
                if len(nums) >= 3:
                    rub.update(dict(zip(["droit", "pris", "solde"], map(_num, nums[:3]))))
                    rub["unite"] = "j"
                else:
                    hms = _RE_HMS_ONLY.findall(head)
                    if len(hms) >= 3:
                        rub.update(dict(zip(["droit", "pris", "solde"], map(hm_to_min, hms[:3]))))
                        rub["unite"] = "min"
            elif sec == SECTION_HS:
                hs = _RE_HMS_ONLY.findall(blob.split("Prestées", 1)[0])
                if len(hs) >= 3:
                    rub["report_n1_min"], rub["solde_final_min"], rub["reserve_min"] = map(hm_to_min, hs[:3])
                rub["mouvements"] = [{"date": _iso(d), "minutes": hm_to_min(h)}
                                     for d, h in _RE_HS_MOV.findall(blob)]
                rub.pop("dates", None)
                continue
            else:  # SECTIONS_1
                nums = _RE_NUM_ONLY.findall(head)
                if nums:
                    rub["total"] = _num(nums[0])
            for name, val in _RE_ADJ.findall(blob):
                rub["ajustements"][name] = _num(val)
            tail = blob.split("Pris le", 1)[-1] if "Pris le" in blob else blob
            rub["dates"] = [{"date": _iso(d), "jours": _num(j) if j else 1.0}
                            for d, j in _RE_DATE_DAYS.findall(tail)]
    return {"agents": agents}


def parse_pdf(source):
    """`source` : chemin, objet fichier ou bytes. Retourne parse_pages(...)."""
    import io
    import pdfplumber
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    with pdfplumber.open(source) as pdf:
        pages = [p.extract_text() or "" for p in pdf.pages]
    out = parse_pages(pages)
    out["nb_pages"] = len(pages)
    return out


def fiche_year(fiche):
    """Année de référence = année de début de la période."""
    p = (fiche.get("periode") or [None])[0]
    return int(p[:4]) if p else date.today().year


def reconcile(fiche, app_dates_by_code):
    """Rapprochement dates fiche RH ↔ dates de l'app.
    `app_dates_by_code` : {code: set(iso_dates)} — codes de congé de l'app
    ('VAC', 'MAL', …) et pseudo-codes '__36' / '__38' pour les repos de cycle.
    Ne compare que dans la période couverte par la fiche."""
    p0, p1 = fiche.get("periode") or [None, None]
    out = {}
    for sec, rub in fiche["rubriques"].items():
        info = RUBRIQUES_INFO.get(sec, {})
        codes = info.get("app_codes")
        if not codes:
            continue
        rh = {d["date"] for d in rub.get("dates", [])}
        app = set()
        for c in codes:
            app |= app_dates_by_code.get(c, set())
        if p0 and p1:
            app = {d for d in app if p0 <= d <= p1}
        out[sec] = {
            "rh_seulement": sorted(rh - app),    # dans la fiche, absent de l'app
            "app_seulement": sorted(app - rh),   # dans l'app, absent de la fiche
            "communes": len(rh & app),
            "nb_rh": len(rh), "nb_app": len(app),
        }
    return out


def situation_reelle(fiche, app_dates_by_code, today=None):
    """Prolonge les compteurs RH avec les jours pris dans l'app APRÈS la fin
    de la période couverte par la fiche (donc depuis son impression) : congés,
    maladies, etc. saisis dans l'app mais pas encore sur un PDF officiel.
    `app_dates_by_code` doit couvrir la plage depuis la fin de période jusqu'à
    `today` (le filtrage exact est fait ici). Purement informatif — ne modifie
    jamais `fiche` ni les données de l'app."""
    p0, p1 = fiche.get("periode") or [None, None]
    if not p1:
        return {}
    today = today or date.today().isoformat()
    out = {}
    for sec, rub in fiche["rubriques"].items():
        info = RUBRIQUES_INFO.get(sec, {})
        codes = info.get("app_codes")
        if not codes:
            continue
        app = set()
        for c in codes:
            app |= app_dates_by_code.get(c, set())
        depuis = sorted(d for d in app if p1 < d <= today)
        if not depuis:
            continue
        nb = len(depuis)
        entry = {"jours_depuis": nb, "dates_depuis": depuis}
        if "solde" in rub:
            entry["pris_actualise"] = round(rub["pris"] + nb, 2)
            entry["solde_actualise"] = round(rub["solde"] - nb, 2)
            if sec in ("REPOS 38H", "REPOS 36H"):
                entry["approx"] = True   # réduction 19j. abs non recalculée
        elif "total" in rub:
            entry["total_actualise"] = round(rub["total"] + nb, 2)
        out[sec] = entry
    return out
