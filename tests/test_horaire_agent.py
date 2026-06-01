# -*- coding: utf-8 -*-
"""Tests unitaires — moteur de cycle horaire."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from datetime import date
import pytest
from horaire_agent import get_shift, MASTER_CYCLE, CYCLE_LEN, ANCHOR, N_TEAMS, OFFSET_PER_TEAM


# ── cycle integrity ───────────────────────────────────────────
def test_cycle_length():
    assert len(MASTER_CYCLE) == 56

def test_cycle_contains_only_valid_codes():
    valid = {'M', 'S', 'R', '36', '38'}
    assert set(MASTER_CYCLE) <= valid

def test_anchor_offset_zero():
    """L'ancre 2026-01-01 avec offset 0 doit retourner MASTER_CYCLE[0]."""
    assert get_shift(ANCHOR, 0) == MASTER_CYCLE[0]

def test_cycle_wraps():
    """56 jours après l'ancre = même poste."""
    d = ANCHOR
    from datetime import timedelta
    d2 = d + timedelta(days=CYCLE_LEN)
    assert get_shift(d2, 0) == get_shift(d, 0)

def test_all_8_teams_return_valid_shift():
    for offset in range(0, CYCLE_LEN, OFFSET_PER_TEAM):
        shift = get_shift(ANCHOR, offset)
        assert shift in {'M', 'S', 'R', '36', '38'}

def test_team_offsets_differ():
    """8 équipes différentes → au moins 2 postes distincts sur l'ancre."""
    shifts = {get_shift(ANCHOR, i * OFFSET_PER_TEAM) for i in range(N_TEAMS)}
    assert len(shifts) > 1


# ── known dates from 2026/2027 PDFs ──────────────────────────
# Équipe 4 (offset=21) : valeurs calculées depuis MASTER_CYCLE
@pytest.mark.parametrize("d,expected", [
    (date(2026, 1,  1), 'M'),   # pos=(0-21)%56=35 → MASTER_CYCLE[35]='M'
    (date(2026, 1,  5), 'S'),   # pos=(4-21)%56=39 → MASTER_CYCLE[39]='S'
    (date(2026, 1, 12), 'M'),   # pos=(11-21)%56=46 → MASTER_CYCLE[46]='M'
])
def test_known_shifts_team4(d, expected):
    assert get_shift(d, 21) == expected


# ── app smoke tests ───────────────────────────────────────────
@pytest.fixture
def client(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib
    import app_horaire
    importlib.reload(app_horaire)
    app_horaire.app.config["TESTING"] = True
    with app_horaire.app.test_client() as c:
        yield c

def test_index_returns_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"<!DOCTYPE html>" in r.data or b"<html" in r.data

def test_manifest_json(client):
    r = client.get("/manifest.json")
    assert r.status_code == 200
    data = r.get_json()
    assert data["display"] == "standalone"

def test_dev_version(client):
    r = client.get("/dev-version")
    assert r.status_code == 200
    assert "v" in r.get_json()

def test_api_agents_empty(client):
    r = client.get("/api/agents")
    assert r.status_code == 200
    assert r.get_json() == {}

def test_create_and_delete_agent(client):
    payload = {"id": "agent-test-001", "name": "Test Agent", "offset": 0}
    r = client.post("/api/agents", json=payload)
    assert r.status_code == 200
    aid = r.get_json()["id"]

    r = client.get("/api/agents")
    assert aid in r.get_json()

    r = client.delete(f"/api/agents/{aid}")
    assert r.status_code == 200

    r = client.get("/api/agents")
    assert aid not in r.get_json()

def test_icon_svg(client):
    r = client.get("/icon.svg")
    assert r.status_code == 200
    assert b"<svg" in r.data

def test_leaves_catalog(client):
    r = client.get("/api/leaves_catalog")
    assert r.status_code == 200
    catalog = r.get_json()
    assert isinstance(catalog, dict)
    assert len(catalog) > 0
