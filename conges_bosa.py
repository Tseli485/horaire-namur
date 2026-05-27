# -*- coding: utf-8 -*-
"""
BOSA Leave Catalog - Prison de Namur / SPF Justice
Tous les types de conges reconnus par BOSA pour la fonction publique federale
Source: bosa.belgium.be (AR 19/11/1998, AR 02/10/1937, AR 28/08/1963)
"""

# ============================================================
# CATALOGUE OFFICIEL DES CONGES (BOSA - Secteur Public Federal)
# ============================================================

LEAVE_CATALOG = {

    # --- VACANCES ANNUELLES ---
    "VAC": {
        "label": "Conge annuel de vacances",
        "category": "VACANCES",
        "color": "#2ecc71",
        "base_ref": "AR 30/01/1979 - AR 19/11/1998",
        "note": "Nombre de jours selon l'age atteint dans l'annee de reference",
        "days_by_age": {
            (0,44):  26,
            (45,49): 27,
            (50,54): 28,
            (55,59): 29,
            (60,61): 30,
            (62,62): 31,
            (63,63): 32,
            (64,64): 33,
            (65,65): 34,
            (66,99): 35,
        },
        "max_carry": 100,   # jours accumulables max
        "annual_save": {    # jours epargnables par an selon age (BOSA)
            (0,44):2,(45,49):3,(50,54):4,(55,59):5,
            (60,61):6,(62,62):7,(63,63):8,(64,64):9,(65,65):10,(66,99):11
        },
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },

    # --- MALADIE ---
    "MAL": {
        "label": "Conge de maladie",
        "category": "MALADIE",
        "color": "#e74c3c",
        "base_ref": "AR 19/11/1998 art. 57-84",
        "note": "21 jours/an garantis; apres epuisement -> disponibilite",
        "annual_days": 21,
        "certificate_required_after": 1,  # jours avant certificat
        "fractional": True,
        "requires_approval": False,
        "impacts_sick_balance": True,
    },
    "MAL_LONG": {
        "label": "Longue maladie",
        "category": "MALADIE",
        "color": "#c0392b",
        "base_ref": "AR 19/11/1998 art. 83",
        "note": "Apres epuisement des 21j/an pendant 5 ans cumules",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": True,
    },
    "ACC_TRAV": {
        "label": "Accident de travail / maladie professionnelle",
        "category": "MALADIE",
        "color": "#e67e22",
        "base_ref": "Loi 03/07/1967",
        "note": "Ne compte pas dans le contingent maladie",
        "fractional": True,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },

    # --- CONGES DE CIRCONSTANCE ---
    "CIRC_MAR_AGENT": {
        "label": "Mariage de l'agent",
        "category": "CIRCONSTANCE",
        "color": "#9b59b6",
        "base_ref": "AR 19/11/1998 art. 34",
        "days": 4,
        "note": "A prendre le jour du mariage ou autour",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_MAR_ENFANT": {
        "label": "Mariage enfant / frere / soeur",
        "category": "CIRCONSTANCE",
        "color": "#8e44ad",
        "base_ref": "AR 19/11/1998 art. 34",
        "days": 1,
        "note": "Jour du mariage uniquement",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_NAI": {
        "label": "Naissance / co-parentalite",
        "category": "CIRCONSTANCE",
        "color": "#3498db",
        "base_ref": "AR 19/11/1998 - loi 10/05/2007",
        "days": 20,
        "note": "Dans les 4 mois suivant la naissance. 10j obligatoires non-fractionnes",
        "fractional": True,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_DEC_CONJOINT": {
        "label": "Deces conjoint ou enfant",
        "category": "CIRCONSTANCE",
        "color": "#2c3e50",
        "base_ref": "AR 19/11/1998 art. 34 - modifie 2018",
        "days": 10,
        "note": "3j autour des funerailles + 7j libres dans l'annee",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_DEC_PARENT": {
        "label": "Deces pere/mere ou beau-parent",
        "category": "CIRCONSTANCE",
        "color": "#34495e",
        "base_ref": "AR 19/11/1998 art. 34",
        "days": 4,
        "note": "3j autour des funerailles + 1j libre dans l'annee",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_DEC_AUTRE": {
        "label": "Deces frere/soeur/grands-parents (cohabitant)",
        "category": "CIRCONSTANCE",
        "color": "#7f8c8d",
        "base_ref": "AR 19/11/1998 art. 34",
        "days": 2,
        "note": "1j si non-cohabitant",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_COM": {
        "label": "Communion solennelle / celebration laique",
        "category": "CIRCONSTANCE",
        "color": "#f39c12",
        "base_ref": "AR 19/11/1998 art. 34",
        "days": 1,
        "note": "Jour de la ceremonie",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_DON": {
        "label": "Don de sang / organe / moelle",
        "category": "CIRCONSTANCE",
        "color": "#e74c3c",
        "base_ref": "AR 19/11/1998",
        "days": None,  # duree variable selon medecin
        "note": "Duree determinee par le medecin",
        "fractional": True,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CIRC_JURY": {
        "label": "Participation jury Cour d'Assises",
        "category": "CIRCONSTANCE",
        "color": "#16a085",
        "base_ref": "AR 19/11/1998",
        "days": None,
        "note": "Duree de la session",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },

    # --- CONGES FAMILIAUX ---
    "CONG_MAT": {
        "label": "Conge de maternite",
        "category": "FAMILIAL",
        "color": "#f1c40f",
        "base_ref": "AR 19/11/1998 art. 85-105",
        "days": 15,  # minimum legal (15 semaines = 105j calendrier)
        "note": "15 semaines minimum dont 5 obligatoires avant accouchement",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "CONG_PAR": {
        "label": "Conge parental",
        "category": "FAMILIAL",
        "color": "#1abc9c",
        "base_ref": "AR 19/11/1998 art. 100ter",
        "days": None,
        "note": "4 mois par enfant (max 12 ans), fractionnable en demi-jours",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
    "SOINS_FAM": {
        "label": "Conge pour soins a un proche gravement malade",
        "category": "FAMILIAL",
        "color": "#27ae60",
        "base_ref": "AR 19/11/1998 art. 100quater",
        "days": None,
        "note": "12 mois max sur carriere, attestation medicale requise",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },

    # --- JOURS FERIES ET PONTS ---
    "FERIE": {
        "label": "Jour ferie legal",
        "category": "FERIE",
        "color": "#f39c12",
        "base_ref": "Loi 04/01/1974",
        "days": 1,
        "note": "Attribution automatique - 10 jours/an + extras secteur public",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "PONT": {
        "label": "Jour de pont officiel",
        "category": "FERIE",
        "color": "#e67e22",
        "base_ref": "Circulaire BOSA / AR annuel",
        "days": 1,
        "note": "Vendredi apres Ascension (auto depuis 2027) + pont ministeriel",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },
    "COMPEN": {
        "label": "Compensation ferie tombe weekend",
        "category": "FERIE",
        "color": "#d35400",
        "base_ref": "AR 19/11/1998",
        "days": 1,
        "note": "A prendre entre le 27 et 31 decembre de l'annee",
        "fractional": False,
        "requires_approval": False,
        "impacts_sick_balance": False,
    },

    # --- AUTRES CONGES ---
    "SYNDI": {
        "label": "Conge syndical",
        "category": "SPECIAL",
        "color": "#95a5a6",
        "base_ref": "AR 28/09/1984",
        "days": None,
        "note": "Quota selon organisation syndicale",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
    "FORM": {
        "label": "Formation / examen reconnu",
        "category": "SPECIAL",
        "color": "#3498db",
        "base_ref": "AR 02/06/2010",
        "days": None,
        "note": "Formation agréée par l'administration",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
    "RECUP": {
        "label": "Recuperation / conge compense",
        "category": "SPECIAL",
        "color": "#1abc9c",
        "base_ref": "Reglement interieur",
        "days": None,
        "note": "Compense les heures prestees en dehors des horaires normaux",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
    "INTERR_CARR": {
        "label": "Interruption de carriere / conge sans solde",
        "category": "SPECIAL",
        "color": "#bdc3c7",
        "base_ref": "Loi 10/04/1995 - AR 07/05/1999",
        "days": None,
        "note": "Max 5 ans cumulés sur carrière",
        "fractional": False,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
    "TP": {
        "label": "Conge temps partiel / mi-temps medical",
        "category": "SPECIAL",
        "color": "#8b5cf6",
        "base_ref": "AR 19/11/1998 - Loi 10/04/1995",
        "days": None,
        "note": "Travail a temps partiel accorde par l'administration",
        "fractional": True,
        "requires_approval": True,
        "impacts_sick_balance": False,
    },
}

# ============================================================
# JOURS FERIES BELGES (secteur public federal) 2026-2030
# ============================================================

from datetime import date, timedelta

def _easter(year):
    a=year%19;b=year//100;c=year%100;d=b//4;e=b%4
    f=(b+8)//25;g=(b-f+1)//3;h=(19*a+b-d-g+15)%30
    i=c//4;k=c%4;l=(32+2*e+2*i-h-k)%7
    m=(a+11*h+22*l)//451
    month=(h+l-7*m+114)//31;day=((h+l-7*m+114)%31)+1
    return date(year,month,day)

def get_vac_entitlement(age_this_year: int) -> dict:
    """Droits vacances BOSA selon l'age atteint dans l'annee de reference."""
    vac = LEAVE_CATALOG["VAC"]
    days = next(v for (lo, hi), v in vac["days_by_age"].items() if lo <= age_this_year <= hi)
    save = next(v for (lo, hi), v in vac["annual_save"].items() if lo <= age_this_year <= hi)
    return {"days": days, "annual_save": save, "age": age_this_year}


def get_sick_capital(service_months: int) -> dict:
    """Capital maladie statutaire BOSA.
    Accumule 21j ouvrables par 12 mois d'anciennete.
    Avant 36 mois de service : avance garantie de 63j.
    """
    complete_years = service_months // 12
    capital = complete_years * 21
    is_advance = service_months < 36
    if is_advance:
        capital = 63
    return {"capital": capital, "is_advance": is_advance, "service_months": service_months}


def get_public_holidays(year: int) -> list:
    """Retourne la liste des jours feries (date, code, label) pour une annee."""
    e = _easter(year)
    holidays = [
        (date(year,1,1),   "FERIE", "Nouvel An"),
        (e+timedelta(1),   "FERIE", "Lundi de Paques"),
        (date(year,5,1),   "FERIE", "Fete du Travail"),
        (e+timedelta(39),  "FERIE", "Ascension"),
        (e+timedelta(40),  "PONT",  "Pont Vendredi/Ascension"),
        (e+timedelta(50),  "FERIE", "Lundi de Pentecote"),
        (date(year,7,21),  "FERIE", "Fete Nationale"),
        (date(year,8,15),  "FERIE", "Assomption"),
        (date(year,11,1),  "FERIE", "Toussaint"),
        (date(year,11,2),  "FERIE", "Jour des morts (public)"),
        (date(year,11,11), "FERIE", "Armistice"),
        (date(year,11,15), "FERIE", "Fete du Roi (public)"),
        (date(year,12,25), "FERIE", "Noel"),
        (date(year,12,26), "FERIE", "Saint-Etienne (public)"),
    ]
    if year == 2026:
        holidays.append((date(2026,7,20), "PONT", "Pont 20 juillet 2026"))
    return sorted(holidays)
