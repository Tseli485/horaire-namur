# -*- coding: utf-8 -*-
"""Service fixe (repos de la grille conservés), 4/5, date de naissance,
échanges avec le planning (jours à rendre) et reprises d'heures."""
import os, sys
from datetime import date, timedelta
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from horaire_agent import get_shift, TEAM_OFFSETS

OFFSETS = sorted(TEAM_OFFSETS.values())


@pytest.fixture
def H(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib, app_horaire
    importlib.reload(app_horaire)
    app_horaire.app.config["TESTING"] = True
    return app_horaire


@pytest.fixture
def client(H):
    with H.app.test_client() as c:
        yield c


def _data(offset, work_regime=None, r45=None):
    return {"agents": {"A": {"team_offset": offset, "work_regime": work_regime, "regime_4_5": r45}},
            "events": [], "shift_overrides": {}, "remarks": {}}


def _days(H, data, start=date(2026, 1, 1), n=3 * 365):
    return [H.get_day_info(start + timedelta(i), "A", data) for i in range(n)]


# ── 1. Service fixe : seules les plages M/S changent, repos de la grille gardés ──
@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize("regime,letter", [("fixe_M", "M"), ("fixe_S", "S")])
def test_service_fixe_garde_tous_les_repos_de_la_grille(H, offset, regime, letter):
    data = _data(offset, regime)
    for i in range(3 * 365):
        d = date(2026, 1, 1) + timedelta(i)
        raw = get_shift(d, offset)
        base = H.get_day_info(d, "A", data)["base"]
        if raw in ("R", "36", "38"):
            assert base == raw, (d, raw, base)          # repos jamais effacés
        else:
            assert base == letter, (d, raw, base)       # M/S -> plage fixe


@pytest.mark.parametrize("offset", OFFSETS)
def test_service_fixe_nuit(H, offset):
    data = _data(offset, "fixe_N")
    for d in (date(2026, 3, 2) + timedelta(i) for i in range(60)):
        raw = get_shift(d, offset)
        base = H.get_day_info(d, "A", data)["base"]
        assert base == (raw if raw in ("R", "36", "38") else "N")


# ── 2. 4/5 : mêmes jours travaillés/repos pour un agent fixe et un agent « cycle » ──
@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize("wd", range(5))
def test_45_meme_rythme_agent_fixe_et_cycle(H, offset, wd):
    cyc = _days(H, _data(offset, None, wd), n=2 * 365)
    fix = _days(H, _data(offset, "fixe_M", wd), n=2 * 365)
    for c, f in zip(cyc, fix):
        assert H.is_worked_shift(c["base"]) == H.is_worked_shift(f["base"]), c["date"]
        assert c["code"] == f["code"], c["date"]
        assert c["decale_38"] == f["decale_38"] and c["decale_r"] == f["decale_r"]
        if H.is_worked_shift(f["base"]):
            assert f["base"] == "M"
        else:
            assert f["base"] == c["base"]


@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize("wd", range(5))
def test_45_jour_designe_toujours_repos_chaque_semaine(H, offset, wd):
    days = _days(H, _data(offset, None, wd), n=2 * 365)
    for d in days:
        dt = date.fromisoformat(d["date"])
        if dt.weekday() == wd:
            assert d["effective"] == "R" and d["code"] == "4/5", d["date"]


@pytest.mark.parametrize("offset", OFFSETS)
@pytest.mark.parametrize("wd", range(5))
def test_45_aucun_repos_de_la_grille_perdu(H, offset, wd):
    """Les repos R/38 de la grille tombant le jour 4/5 sont décalés, jamais perdus."""
    data = _data(offset, None, wd)
    start = date(2026, 1, 12)
    for i in range(0, 56 * 3, 1):
        d = start + timedelta(i)
        if d.weekday() == wd and get_shift(d, offset) in ("R", "38"):
            # le repos déplacé doit apparaître dans les 7 jours suivants
            tgt = [H.get_day_info(d + timedelta(k), "A", data) for k in range(1, 8)]
            # (un jour férié/pont reçoit le repos décalé : il garde alors sa couleur férié)
            assert any(t["base"] in ("R", "38")
                       and t["code"] in ("REPOS-R", "REPOS-38", "FERIE", "PONT") for t in tgt), d


# ── 3. Date de naissance ─────────────────────────────────────────────────────
def test_age_exact_tient_compte_du_mois_et_du_jour(H):
    bd = date(1961, 9, 24)
    assert H.age_exact(bd, date(2026, 9, 23)) == 64
    assert H.age_exact(bd, date(2026, 9, 24)) == 65
    assert H.age_exact(date(2000, 2, 29), date(2026, 2, 28)) == 25
    assert H.age_exact(date(2000, 2, 29), date(2026, 3, 1)) == 26


def test_inscription_exige_la_date_de_naissance(client):
    base = {"id": "B1", "name": "Test", "pin": "1234", "offset": 0}
    assert client.post("/api/auth/register", json=base).status_code == 400
    assert client.post("/api/auth/register", json=dict(base, birth_date="pas-une-date")).status_code == 400
    assert client.post("/api/auth/register", json=dict(base, birth_date="2999-01-01")).status_code == 400
    r = client.post("/api/auth/register", json=dict(base, birth_date="1980-05-15"))
    assert r.status_code == 200
    me = client.get("/api/auth/me").get_json()
    assert me["birth_date"] == "1980-05-15" and isinstance(me["age"], int)
    # on ne peut plus l'effacer ni la corrompre
    assert client.patch("/api/agents/B1", json={"birth_date": ""}).status_code == 400
    assert client.patch("/api/agents/B1", json={"birth_date": "31/02/1980"}).status_code == 400
    assert client.patch("/api/agents/B1", json={"birth_date": "1981-01-02"}).status_code == 200


def test_ancien_compte_sans_naissance_reste_utilisable(H, client):
    """Compte existant sans date : aucune erreur, /me renvoie age=None (la page la demande)."""
    client.post("/api/auth/register", json={"id": "OLD", "name": "Old", "pin": "1234",
                                            "offset": 0, "birth_date": "1980-05-15"})
    d = H.load(); d["agents"]["OLD"].pop("birth_date"); H.save(d)
    me = client.get("/api/auth/me").get_json()
    assert me["logged_in"] and me["birth_date"] is None and me["age"] is None
    assert client.get("/api/entitlements/OLD/2026").status_code == 200


# ── 4. Échanges avec le planning ─────────────────────────────────────────────
def _agent(client, aid="EX1", offset=0):
    r = client.post("/api/auth/register", json={"id": aid, "name": aid, "pin": "1234",
                                                "offset": offset, "birth_date": "1980-05-15"})
    assert r.status_code == 200
    return aid


def _find(offset, wanted, after=date(2026, 6, 1)):
    d = after
    while get_shift(d, offset) != wanted:
        d += timedelta(1)
    return d


def test_planning_jour_de_repos_est_a_rendre(client):
    aid = _agent(client)
    d = _find(0, "R")
    r = client.post("/api/exchanges", json={"agent_id": aid, "type": "hierarchie",
                                            "date_service": d.isoformat(), "poste_service": "M"})
    assert r.status_code == 200 and r.get_json()["a_rendre"] is True
    ex = client.get(f"/api/exchanges/{aid}").get_json()[0]
    assert ex["status"] == "pending" and ex["a_rendre"] and ex["jour_prevu"] == "R"
    bal = client.get(f"/api/exchanges/{aid}/balance").get_json()
    assert bal["a_rendre"] == 1 and bal["hierarchie_pending"] == 1


def test_planning_changement_de_poste_sans_dette(client):
    aid = _agent(client)
    d = _find(0, "M")
    r = client.post("/api/exchanges", json={"agent_id": aid, "type": "hierarchie",
                                            "date_service": d.isoformat(), "poste_service": "S"})
    assert r.get_json()["a_rendre"] is False
    ex = client.get(f"/api/exchanges/{aid}").get_json()[0]
    assert ex["status"] == "done"
    assert client.get(f"/api/exchanges/{aid}/balance").get_json()["a_rendre"] == 0


def test_surcharge_manuelle_ne_masque_pas_la_dette(client):
    """L'agent a déjà mis M sur son jour de repos : le prévu reste le repos."""
    aid = _agent(client)
    d = _find(0, "R")
    client.put(f"/api/shift_override/{aid}/{d.isoformat()}", json={"shift": "M"})
    prev = client.get(f"/api/exchanges/{aid}/preview/{d.isoformat()}").get_json()
    assert prev["prevu_repos"] is True


def test_jour_rendu_solde_et_repere_calendrier(client):
    aid = _agent(client)
    d = _find(0, "R"); back = d + timedelta(10)
    eid = client.post("/api/exchanges", json={"agent_id": aid, "type": "hierarchie",
                      "date_service": d.isoformat(), "poste_service": "S"}).get_json()["id"]
    cal = client.get(f"/api/calendar/{aid}/{d.year}/{d.month}").get_json()
    day = next(x for x in cal["days"] if x["date"] == d.isoformat())
    assert day["echanges"][0]["role"] == "service" and day["echanges"][0]["a_rendre"] is True
    r = client.patch(f"/api/exchanges/{eid}", json={"date_remboursement": back.isoformat(),
                                                    "poste_remboursement": "R"})
    assert r.status_code == 200 and r.get_json()["exchange"]["status"] == "done"
    cal2 = client.get(f"/api/calendar/{aid}/{back.year}/{back.month}").get_json()
    day2 = next(x for x in cal2["days"] if x["date"] == back.isoformat())
    assert day2["echanges"][0]["role"] == "rendu"
    assert client.get(f"/api/exchanges/{aid}/balance").get_json()["a_rendre"] == 0


def test_ancien_echange_hierarchie_reste_a_rendre(H, client):
    """Donnée historique (sans champ a_rendre) : comportement d'avant conservé."""
    aid = _agent(client)
    d = H.load()
    d["exchanges"].append({"id": "old1", "agent_id": aid, "type": "hierarchie", "direction": "donne",
                           "date_service": "2026-09-01", "poste_service": "M", "colleague": "",
                           "date_remboursement": None, "poste_remboursement": None, "note": "",
                           "status": "pending", "created": "2026-09-01"})
    H.save(d)
    bal = client.get(f"/api/exchanges/{aid}/balance").get_json()
    assert bal["a_rendre"] == 1 and bal["plus_ancien_jours"] >= 0


def test_echange_collegue_inchange(client):
    aid = _agent(client)
    client.post("/api/exchanges", json={"agent_id": aid, "type": "collegue", "direction": "donne",
                                        "colleague": "Jean", "date_service": "2026-10-01", "poste_service": "M"})
    bal = client.get(f"/api/exchanges/{aid}/balance").get_json()
    assert bal["collegues"]["Jean"]["balance"] == 1 and bal["a_rendre"] == 0


# ── 5. Reprises d'heures ─────────────────────────────────────────────────────
def test_reprises_heures_et_totaux(client):
    aid = _agent(client)
    assert client.post(f"/api/reprises/{aid}", json={"date": "2026-10-01", "heures": 2, "minutes": 30,
                                                      "note": "récup"}).status_code == 200
    client.post(f"/api/reprises/{aid}", json={"date": "2026-10-05", "heures": 1, "minutes": 45})
    j = client.post(f"/api/reprises/{aid}", json={"date": "2025-12-20", "heures": 4}).get_json()
    assert j["total_minutes"] == 150 + 105 + 240
    assert j["par_annee"] == {"2026": 255, "2025": 240}
    assert [r["date"] for r in j["items"]] == ["2026-10-05", "2026-10-01", "2025-12-20"]
    rid = j["items"][0]["id"]
    assert client.delete(f"/api/reprises/{aid}/{rid}").get_json()["removed"] == 1
    assert client.get(f"/api/reprises/{aid}").get_json()["total_minutes"] == 150 + 240


@pytest.mark.parametrize("body", [{"date": "x", "heures": 1}, {"date": "2026-10-01"},
                                  {"date": "2026-10-01", "heures": 30}, {"date": "2026-10-01", "minutes": "abc"}])
def test_reprises_validation(client, body):
    aid = _agent(client)
    assert client.post(f"/api/reprises/{aid}", json=body).status_code == 400


def test_reprises_isolees_par_agent(client):
    a = _agent(client, "R1")
    client.post(f"/api/reprises/{a}", json={"date": "2026-10-01", "heures": 1})
    assert client.get("/api/reprises/AUTRE").status_code == 403
