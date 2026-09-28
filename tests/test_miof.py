# -*- coding: utf-8 -*-
"""Tests unitaires — congé MIOF (motifs impérieux d'ordre familial, AR 19/11/1998 art. 38-40)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from conges_bosa import LEAVE_CATALOG


# ── catalogue BOSA (conges_bosa.py) ─────────────────────────────
def test_miof_in_leave_catalog():
    assert "MIOF" in LEAVE_CATALOG
    entry = LEAVE_CATALOG["MIOF"]
    assert entry["category"] == "FAMILIAL"
    assert entry["base_ref"] == "AR 19/11/1998 art. 38-40 (stat/stag/mandataires) + AR 11/10/1991 (ctr, cong. raisons imperieuses)"
    assert entry["days"] == 20

def test_miof_flags_match_business_rule():
    """Fractionnable (jour/demi-jour), soumis a justification, sans impact
    sur le contingent maladie (AR 19/11/1998 art. 38-40)."""
    entry = LEAVE_CATALOG["MIOF"]
    assert entry["fractional"] is True
    assert entry["requires_approval"] is True
    assert entry["impacts_sick_balance"] is False


# ── app smoke tests (mêmes fixtures que test_horaire_agent.py) ──
@pytest.fixture
def client(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib
    import app_horaire
    importlib.reload(app_horaire)
    app_horaire.app.config["TESTING"] = True
    with app_horaire.app.test_client() as c:
        yield c

def _register(client, aid="AGENT-MIOF-001"):
    r = client.post("/api/auth/register",
                    json={"id": aid, "name": "Test MIOF", "pin": "1234", "offset": 0})
    assert r.status_code == 200
    return r.get_json()["id"]

def test_miof_exposed_via_leaves_catalog_api(client):
    _register(client)
    r = client.get("/api/leaves_catalog")
    assert r.status_code == 200
    catalog = r.get_json()
    assert "MIOF" in catalog
    assert catalog["MIOF"]["category"] == "FAMILIAL"

def test_create_miof_event_reflected_in_day_info(client):
    aid = _register(client)
    r = client.post("/api/events", json={
        "agent_id": aid, "code": "MIOF",
        "date_start": "2026-11-10", "date_end": "2026-11-10",
        "note": "Enfant malade",
    })
    assert r.status_code == 200
    assert r.get_json()["ok"] is True

    r = client.get(f"/api/day/{aid}/2026-11-10")
    assert r.status_code == 200
    day = r.get_json()
    assert day["code"] == "MIOF"
    assert day["label"] == LEAVE_CATALOG["MIOF"]["label"]


# ── plafond annuel 20j (AR 19/11/1998 art. 38-40 + AR 11/10/1991) ──
def test_miof_short_request_within_quota_accepted(client):
    aid = _register(client)
    r = client.post("/api/events", json={
        "agent_id": aid, "code": "MIOF",
        "date_start": "2026-03-02", "date_end": "2026-03-08",  # 1 semaine
    })
    assert r.status_code == 200

def test_miof_over_quota_rejected(client):
    aid = _register(client)
    r = client.post("/api/events", json={
        "agent_id": aid, "code": "MIOF",
        "date_start": "2026-01-01", "date_end": "2026-12-31",  # largement > 20j travailles
    })
    assert r.status_code == 400
    assert "MIOF" in r.get_json()["error"]

def test_miof_quota_visible_in_entitlements(client):
    aid = _register(client)
    r = client.post("/api/events", json={
        "agent_id": aid, "code": "MIOF",
        "date_start": "2026-03-02", "date_end": "2026-03-08",
        "status": "accepte",
    })
    assert r.status_code == 200

    r = client.get(f"/api/entitlements/{aid}/2026")
    assert r.status_code == 200
    detail = r.get_json()["conges_detail"]["MIOF"]
    assert detail["quota"] == 20
    assert 1 <= detail["used"] <= 7
