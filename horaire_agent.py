# -*- coding: utf-8 -*-
"""
HoraireAgent - Prison de Namur
Systeme de gestion d'horaires rotatifs - 8 equipes
Cycle detecte: 56 jours | Validation: 0 erreur sur 2026-2027
"""

import pdfplumber
from datetime import date, timedelta
import json
import sys

# ============================================================
# CYCLE MASTER (56 jours) - detecte depuis l'Equipe 4, 2026
# Ancre: 2026-01-01 = position 0 (index 0)
# ============================================================
MASTER_CYCLE = [
    'S','S','S','S','R','M','M','M','M','R','R','36',
    'S','S','S','R','M','M','M','M','M','R','38','R','R',
    'S','S','R','S','S','S','S','M','R','M','M','M','R','R',
    'S','S','S','R','36','M','M','M','M','R','M','M','R','R','38','R','S'
]
assert len(MASTER_CYCLE) == 56, "Cycle doit etre 56 jours"

ANCHOR = date(2026, 1, 1)
CYCLE_LEN = 56
N_TEAMS = 8
OFFSET_PER_TEAM = CYCLE_LEN // N_TEAMS  # = 7 jours

MONTH_NAMES_FR = [
    'JANVIER','FEVRIER','MARS','AVRIL','MAI','JUIN',
    'JUILLET','AOUT','SEPTEMBRE','OCTOBRE','NOVEMBRE','DECEMBRE'
]
DAY_NAMES_FR = {0:'Lun',1:'Mar',2:'Mer',3:'Jeu',4:'Ven',5:'Sam',6:'Dim'}
REPOS_CODES = {'R','36','38'}


def get_shift(d: date, team_offset: int = 0) -> str:
    """Retourne le poste pour une date donnee et un offset d'equipe."""
    delta = (d - ANCHOR).days
    pos = (delta - team_offset) % CYCLE_LEN
    return MASTER_CYCLE[pos]


def extract_pdf_schedule(pdf_path: str) -> dict:
    """Extrait l'horaire d'un PDF (format Prison de Namur)."""
    schedule = {}
    with pdfplumber.open(pdf_path) as pdf:
        tables = pdf.pages[0].extract_tables()
    for table in tables:
        if not table:
            continue
        month = table[0][0]
        if not month:
            continue
        days = []
        for row in table[1:]:
            if row and len(row) >= 3 and row[0] and row[1]:
                try:
                    days.append({
                        'day_name': row[0],
                        'day_num': int(row[1]),
                        'shift': row[2] if row[2] else '?'
                    })
                except (ValueError, TypeError):
                    continue
        schedule[month] = days
    return schedule


def detect_team_offset(pdf_path: str, year: int) -> tuple:
    """
    Detecte automatiquement l'offset d'equipe depuis un PDF.
    Retourne (offset, equipe_numero, score_confiance).
    """
    schedule = extract_pdf_schedule(pdf_path)

    # Aplatir en sequence (date, shift_reel)
    real_sequence = []
    for m_idx, m_name in enumerate(MONTH_NAMES_FR, 1):
        if m_name not in schedule:
            continue
        for d in schedule[m_name]:
            try:
                dt = date(year, m_idx, d['day_num'])
                real_sequence.append((dt, d['shift']))
            except ValueError:
                continue

    if not real_sequence:
        return None, None, 0

    # Tester tous les offsets possibles (multiples de 7)
    best_offset = 0
    best_matches = 0

    for offset in range(CYCLE_LEN):
        matches = sum(
            1 for dt, sh in real_sequence
            if get_shift(dt, offset) == sh
        )
        if matches > best_matches:
            best_matches = matches
            best_offset = offset

    confidence = best_matches / len(real_sequence) * 100

    # Deduire le numero d'equipe (hypothese: equipes decalees de 7j)
    # Equipe 4 = offset 0 par convention
    team_num = None
    for t in range(1, N_TEAMS + 1):
        if (t - 4) * OFFSET_PER_TEAM % CYCLE_LEN == best_offset:
            team_num = t
            break

    return best_offset, team_num, confidence


def generate_schedule(year: int, team_offset: int = 0) -> dict:
    """Genere l'horaire complet d'une annee pour un offset d'equipe."""
    from calendar import isleap, monthrange

    schedule = {}
    for m_idx, m_name in enumerate(MONTH_NAMES_FR, 1):
        days_in_month = monthrange(year, m_idx)[1]
        month_data = []
        for day in range(1, days_in_month + 1):
            d = date(year, m_idx, day)
            sh = get_shift(d, team_offset)
            month_data.append({
                'day_name': DAY_NAMES_FR[d.weekday()],
                'day_num': day,
                'shift': sh,
                'date': d.isoformat()
            })
        schedule[m_name] = month_data
    return schedule


def schedule_stats(schedule: dict) -> dict:
    """Calcule les statistiques d'un horaire."""
    stats = {'total': 0, 'M': 0, 'S': 0, 'R': 0, 'special_36': 0, 'special_38': 0,
             'travail': 0, 'weekend_M': 0, 'weekend_S': 0, 'weekend_R': 0}
    monthly = {}

    for m_name, days in schedule.items():
        mstats = {'M': 0, 'S': 0, 'R': 0}
        for d in days:
            sh = d['shift']
            is_weekend = d['day_name'] in ('Sam', 'Dim')
            stats['total'] += 1
            if sh == 'M':
                stats['M'] += 1
                mstats['M'] += 1
                stats['travail'] += 1
                if is_weekend: stats['weekend_M'] += 1
            elif sh == 'S':
                stats['S'] += 1
                mstats['S'] += 1
                stats['travail'] += 1
                if is_weekend: stats['weekend_S'] += 1
            elif sh in REPOS_CODES:
                stats['R'] += 1
                mstats['R'] += 1
                if is_weekend: stats['weekend_R'] += 1
                if sh == '36': stats['special_36'] += 1
                if sh == '38': stats['special_38'] += 1
        monthly[m_name] = mstats

    return stats, monthly


def print_schedule(schedule: dict, year: int, team_offset: int):
    """Affiche l'horaire de maniere lisible."""
    team_info = f"offset={team_offset}j"
    for t in range(1, N_TEAMS + 1):
        if (t - 4) * OFFSET_PER_TEAM % CYCLE_LEN == team_offset:
            team_info = f"Equipe {t}"
            break

    print(f"\n{'='*60}")
    print(f"HORAIRE {year} - Prison de Namur - {team_info}")
    print(f"{'='*60}")

    stats, monthly = schedule_stats(schedule)

    for m_name, days in schedule.items():
        ms = monthly[m_name]
        line = ' | '.join(f"{d['day_name']}{d['day_num']}:{d['shift']}" for d in days)
        print(f"\n{m_name} ({len(days)}j) M={ms['M']} S={ms['S']} R={ms['R']}:")
        print(f"  {line}")

    print(f"\n{'='*60}")
    print(f"TOTAL {year}: MATIN={stats['M']} SOIR={stats['S']} REPOS={stats['R']}")
    print(f"  Travail={stats['travail']} | Repos-36={stats['special_36']} | Repos-38={stats['special_38']}")
    print(f"  Weekends: M={stats['weekend_M']} S={stats['weekend_S']} R={stats['weekend_R']}")


def find_next_shifts(team_offset: int, n: int = 14) -> list:
    """Retourne les N prochains jours depuis aujourd'hui."""
    today = date.today()
    result = []
    for i in range(n):
        d = today + timedelta(days=i)
        sh = get_shift(d, team_offset)
        result.append({
            'date': d.isoformat(),
            'day': DAY_NAMES_FR[d.weekday()],
            'shift': sh,
            'label': {'M':'MATIN','S':'SOIR','R':'REPOS','36':'REPOS-36','38':'REPOS-38'}.get(sh, sh)
        })
    return result


def query_date(d: date, team_offset: int = 0) -> dict:
    """Retourne le poste pour une date precise."""
    sh = get_shift(d, team_offset)
    return {
        'date': d.isoformat(),
        'day': DAY_NAMES_FR[d.weekday()],
        'shift': sh,
        'label': {'M':'MATIN','S':'SOIR','R':'REPOS','36':'REPOS-36','38':'REPOS-38'}.get(sh, sh)
    }


# ============================================================
# CLI / DEMO
# ============================================================
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='HoraireAgent - Prison de Namur')
    parser.add_argument('--action', choices=['generate','detect','query','next','validate','teams'],
                        default='teams', help='Action a effectuer')
    parser.add_argument('--year', type=int, default=2028, help='Annee a generer')
    parser.add_argument('--pdf', type=str, help='Chemin vers le PDF source')
    parser.add_argument('--pdf-year', type=int, help="Annee du PDF source")
    parser.add_argument('--offset', type=int, default=0, help='Offset equipe (0-55)')
    parser.add_argument('--date', type=str, help='Date format YYYY-MM-DD')
    parser.add_argument('--days', type=int, default=14, help='Nb jours (action=next)')

    args = parser.parse_args()

    if args.action == 'teams':
        print("\n=== OFFSETS DES 8 EQUIPES ===")
        print(f"Cycle: {CYCLE_LEN} jours | Offset inter-equipe: {OFFSET_PER_TEAM} jours")
        print(f"Ancre: Equipe 4 = 2026-01-01 = position 1 du cycle\n")
        for t in range(1, N_TEAMS + 1):
            off = (t - 4) * OFFSET_PER_TEAM % CYCLE_LEN
            d = ANCHOR
            sh = get_shift(d, off)
            print(f"  Equipe {t}: offset={off:2d}j | Jan 1 2026 = {sh}")

    elif args.action == 'generate':
        offset = args.offset
        if args.pdf and args.pdf_year:
            print(f"Detection de l'offset depuis {args.pdf}...")
            offset, team, conf = detect_team_offset(args.pdf, args.pdf_year)
            print(f"Offset detecte: {offset}j | Equipe: {team} | Confiance: {conf:.1f}%")
        sched = generate_schedule(args.year, offset)
        print_schedule(sched, args.year, offset)

    elif args.action == 'detect':
        if not args.pdf or not args.pdf_year:
            print("ERREUR: --pdf et --pdf-year requis")
            sys.exit(1)
        offset, team, conf = detect_team_offset(args.pdf, args.pdf_year)
        print(f"\nFichier: {args.pdf}")
        print(f"Equipe detectee: {team if team else 'inconnue (offset non-standard)'}")
        print(f"Offset: {offset} jours dans le cycle de 56j")
        print(f"Confiance: {conf:.1f}%")

    elif args.action == 'validate':
        for year_str, year_int in [('2026', 2026), ('2027', 2027)]:
            try:
                sched_pdf = extract_pdf_schedule(f'{year_str}.pdf')
                errors = 0
                total = 0
                for m_idx, m_name in enumerate(MONTH_NAMES_FR, 1):
                    if m_name not in sched_pdf:
                        continue
                    for d in sched_pdf[m_name]:
                        try:
                            dt = date(year_int, m_idx, d['day_num'])
                            predicted = get_shift(dt, 0)
                            if predicted != d['shift']:
                                errors += 1
                            total += 1
                        except ValueError:
                            continue
                print(f"Validation {year_int}: {errors}/{total} erreurs ({(total-errors)/total*100:.1f}% correct)")
            except FileNotFoundError:
                print(f"Fichier {year_str}.pdf non trouve")

    elif args.action == 'next':
        print(f"\nProchains {args.days} jours (offset={args.offset}):")
        for entry in find_next_shifts(args.offset, args.days):
            print(f"  {entry['date']} {entry['day']:3s} -> {entry['label']}")

    elif args.action == 'query':
        if not args.date:
            print("ERREUR: --date requis")
            sys.exit(1)
        d = date.fromisoformat(args.date)
        result = query_date(d, args.offset)
        print(f"\n{result['date']} ({result['day']}): {result['label']}")
