# -*- coding: utf-8 -*-
"""Tests — parseur « Fiche de congé (Après RT) » + routes /api/fiche_rh.
Fixture textuelle ANONYMISÉE reproduisant la mise en page réelle (pdfplumber
extract_text) : aucune donnée personnelle dans le dépôt."""
import sys, os, json, io
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'lib'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import fiche_rh

PAGE1 = """FICHE DE CONGE (Après RT)
Nom : DUPONT Jean Numéro de matricule : 012345
Situation du 01/01/2025 au 15/12/2025 inclus Date d'impression : 16/12/2025
CONGE Réserve Report Droit (année) Pris Solde
0,00 j 0,00 j 25,00 j 25,00 j 0,00 j
Révision de contrat
-6,00 j
Réduction Absence
2,00 j
Pris le
[01] 21/01/2025 1,00 j [02] 23/01/2025 1,00 j [03] 24/01/2025 1,00 j
EPARGNE-TEMPS Epargne Pris Solde
0,00 j 0,00 j 0,00 j
Pris le
FERIES Réserve Report Droit Pris Solde
0,00 j -1,00 j 5,00 j 4,00 j 0,00 j
Pris le
[01] 29/05/2025 [02] 20/06/2025 [03] 24/10/2025 [04] 28/11/2025
REPOS 38H Réserve Report Droit Pris Solde
0,00 j -0,60 j 9,60 j 9,00 j -0,80 j
Réduction 19 j. abs
0,80 j
Pris le
[01] 29/01/2025 1,00 j [02] 21/02/2025 1,00 j
REPOS 36H Réserve Report Droit Pris Solde
0,00 j -1,00 j 1,00 j 1,00 j -1,00 j
Réduction 19 j. abs
0,00 j
Pris le
Page 1 / 3"""

PAGE2 = """FICHE DE CONGE (Après RT)
Nom : DUPONT Jean Numéro de matricule : 012345
Situation du 01/01/2025 au 15/12/2025 inclus Date d'impression : 16/12/2025
[01] 26/03/2025 1,00 j
REPOS Réserve Report Droit Pris Solde
0,00 j 0,00 j 100,00 j 100,00 j 0,00 j
FORMATION Droit Pris Solde
00h00m 00h00m 00h00m
Pris le
MALADIE Total 3,00 j
Pris le
[01] 08/01/2025 [02] 12/02/2025 [03] 13/02/2025
CONGE POUR MOTIF IMPERIEUX D'ORDRE FAMILIAL (45 J.) Pris
2,00 j
Pris le
[01] 09/01/2025 1,00 j [02] 10/01/2025 1,00 j
NOUVELLE RUBRIQUE FUTURE Droit Pris Solde
1,00 j 0,00 j 1,00 j
Page 2 / 3"""

PAGE3 = """FICHE DE CONGE (Après RT)
Nom : DUPONT Jean Numéro de matricule : 012345
Situation du 01/01/2025 au 15/12/2025 inclus Date d'impression : 16/12/2025
HEURES SUPP. Report année précédente 05h05m Solde final 01h18m
Réserve 02h36m
Prestées (+) / Récupérées (-)
[001] 01/01/2025 : 08h00m [002] 28/10/2025 : 01h32m [003] 31/10/2025 : -07h55m [004] 05/11/2025 : -08h00m
Page 3 / 3"""

PAGE_OTHER = PAGE1.replace("DUPONT Jean", "MARTIN Paul").replace("012345", "099999")


@pytest.fixture
def fiche():
    return fiche_rh.parse_pages([PAGE1, PAGE2, PAGE3, PAGE_OTHER])


# ── parseur ────────────────────────────────────────────────────
def test_agents_et_entete(fiche):
    assert set(fiche["agents"]) == {"012345", "099999"}
    f = fiche["agents"]["012345"]
    assert f["nom"] == "DUPONT Jean"
    assert f["periode"] == ["2025-01-01", "2025-12-15"]
    assert f["imprimee_le"] == "2025-12-16"
    assert fiche_rh.fiche_year(f) == 2025


def test_rubrique_5_colonnes_et_ajustements(fiche):
    c = fiche["agents"]["012345"]["rubriques"]["CONGE"]
    assert (c["reserve"], c["report"], c["droit"], c["pris"], c["solde"]) == (0, 0, 25, 25, 0)
    assert c["ajustements"] == {"Révision de contrat": -6.0, "Réduction Absence": 2.0}
    assert [d["date"] for d in c["dates"]] == ["2025-01-21", "2025-01-23", "2025-01-24"]
    assert all(d["jours"] == 1.0 for d in c["dates"])


def test_formule_solde(fiche):
    r = fiche["agents"]["012345"]["rubriques"]["REPOS 38H"]
    red = r["ajustements"]["Réduction 19 j. abs"]
    assert round(r["reserve"] + r["report"] + r["droit"] - r["pris"] - red, 2) == r["solde"] == -0.8


def test_dates_sans_jours_et_continuation_page(fiche):
    rub = fiche["agents"]["012345"]["rubriques"]
    assert [d["date"] for d in rub["FERIES"]["dates"]] == ["2025-05-29", "2025-06-20", "2025-10-24", "2025-11-28"]
    assert [d["date"] for d in rub["REPOS 38H"]["dates"]] == ["2025-01-29", "2025-02-21"]
    # la date du REPOS 36H est sur la page suivante, sans en-tête (continuation)
    assert [d["date"] for d in rub["REPOS 36H"]["dates"]] == ["2025-03-26"]


def test_rubriques_total_et_heures(fiche):
    rub = fiche["agents"]["012345"]["rubriques"]
    assert rub["MALADIE"]["total"] == 3.0 and len(rub["MALADIE"]["dates"]) == 3
    assert rub["CONGE POUR MOTIF IMPERIEUX D'ORDRE FAMILIAL (45 J.)"]["total"] == 2.0
    assert rub["FORMATION"] == {"dates": [], "ajustements": {}, "droit": 0, "pris": 0, "solde": 0, "unite": "min"}


def test_heures_supp(fiche):
    hs = fiche["agents"]["012345"]["rubriques"]["HEURES SUPP."]
    assert (hs["report_n1_min"], hs["solde_final_min"], hs["reserve_min"]) == (305, 78, 156)
    assert [m["minutes"] for m in hs["mouvements"]] == [480, 92, -475, -480]
    assert hs["reserve_min"] + hs["report_n1_min"] + sum(m["minutes"] for m in hs["mouvements"]) == hs["solde_final_min"]
    assert fiche_rh.min_to_hm(-475) == "-07h55"


def test_rubrique_inconnue_conservee_sans_erreur(fiche):
    f = fiche["agents"]["012345"]
    assert [u["titre"] for u in f["inconnues"]] == ["NOUVELLE RUBRIQUE FUTURE"]
    assert "1,00 j" in f["inconnues"][0]["texte"]
    # et elle n'a pas pollué la rubrique précédente
    assert len(f["rubriques"]["CONGE POUR MOTIF IMPERIEUX D'ORDRE FAMILIAL (45 J.)"]["dates"]) == 2


def test_reconcile(fiche):
    f = fiche["agents"]["012345"]
    app = {"VAC": {"2025-01-21", "2025-01-23", "2025-02-01", "2026-01-05"},
           "MAL": {"2025-01-08"}, "MSC": {"2025-02-12"}, "__38": {"2025-01-29"}}
    r = fiche_rh.reconcile(f, app)
    assert r["CONGE"]["rh_seulement"] == ["2025-01-24"]
    assert r["CONGE"]["app_seulement"] == ["2025-02-01"]       # 2026 hors période → ignoré
    assert r["CONGE"]["communes"] == 2
    assert r["MALADIE"]["rh_seulement"] == ["2025-02-13"]
    assert r["REPOS 38H"]["rh_seulement"] == ["2025-02-21"]
    assert r["REPOS 36H"]["rh_seulement"] == ["2025-03-26"]
    assert "FERIES" not in r   # pas d'équivalent app → pas de rapprochement


def test_situation_reelle_rien_apres_periode(fiche):
    f = fiche["agents"]["012345"]
    assert fiche_rh.situation_reelle(f, {}, today="2025-12-20") == {}


def test_situation_reelle_evenements_apres_impression(fiche):
    f = fiche["agents"]["012345"]
    app = {"MAL": {"2025-12-20", "2025-12-22"}, "VAC": {"2025-12-18"}}
    r = fiche_rh.situation_reelle(f, app, today="2025-12-23")
    assert r["MALADIE"]["jours_depuis"] == 2
    assert r["MALADIE"]["dates_depuis"] == ["2025-12-20", "2025-12-22"]
    assert r["MALADIE"]["total_actualise"] == 5.0        # 3,00 j (fiche) + 2 pris depuis
    conge = f["rubriques"]["CONGE"]
    assert r["CONGE"]["jours_depuis"] == 1
    assert r["CONGE"]["pris_actualise"] == round(conge["pris"] + 1, 2)
    assert r["CONGE"]["solde_actualise"] == round(conge["solde"] - 1, 2)


def test_situation_reelle_ignore_dates_dans_la_periode(fiche):
    f = fiche["agents"]["012345"]
    app = {"VAC": {"2025-01-21"}}   # déjà couvert par la fiche → ne doit pas re-compter
    assert fiche_rh.situation_reelle(f, app, today="2025-12-23") == {}


# ── routes Flask (parse_pdf simulé : pas de PDF dans le dépôt) ──
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for m in ("app_horaire",):
        sys.modules.pop(m, None)
    import app_horaire
    app_horaire.app.config["TESTING"] = True
    monkeypatch.setattr(app_horaire._frh, "parse_pdf",
                        lambda raw: fiche_rh.parse_pages([PAGE1, PAGE2, PAGE3, PAGE_OTHER]))
    c = app_horaire.app.test_client()
    r = c.post("/api/auth/register", json={"id": "T1", "name": "Test", "pin": "1234", "offset": 14})
    assert r.status_code == 200
    return c


def _upload(c):
    return c.post("/api/fiche_rh/T1/import",
                  data={"pdf": (io.BytesIO(b"%PDF-fake"), "fiche.pdf")},
                  content_type="multipart/form-data")


def test_import_exige_matricule(client):
    r = _upload(client)
    assert r.status_code == 400 and "matricule" in r.get_json()["error"].lower()


def test_matricule_valide(client):
    assert client.patch("/api/agents/T1", json={"matricule": "12ab"}).status_code == 400
    r = client.patch("/api/agents/T1", json={"matricule": "012345"})
    assert r.status_code == 200 and r.get_json()["agent"]["matricule"] == "012345"


def test_import_matricule_absent_du_pdf(client):
    client.patch("/api/agents/T1", json={"matricule": "777777"})
    r = _upload(client)
    assert r.status_code == 400 and "777777" in r.get_json()["error"]


def test_import_puis_lecture_et_remplacement(client):
    client.patch("/api/agents/T1", json={"matricule": "012345"})
    r = _upload(client)
    j = r.get_json()
    assert r.status_code == 200 and j["year"] == 2025 and j["remplace"] is False
    assert j["inconnues"] == ["NOUVELLE RUBRIQUE FUTURE"]
    # seule la fiche de l'agent connecté est stockée
    assert client.get("/api/fiche_rh/T1").get_json()["years"] == ["2025"]
    p = client.get("/api/fiche_rh/T1/2025").get_json()
    assert p["fiche"]["matricule"] == "012345" and "099999" not in json.dumps(p)
    assert p["rapprochement"]["CONGE"]["nb_rh"] == 3
    # ré-import : remplace + trace dans l'historique
    j2 = _upload(client).get_json()
    assert j2["remplace"] is True
    assert len(client.get("/api/fiche_rh/T1/2025").get_json()["historique"]) == 1


def test_import_ne_touche_pas_aux_donnees_existantes(client):
    client.patch("/api/agents/T1", json={"matricule": "012345"})
    # 23/01/2025 = poste M pour l'équipe offset 14 (jour compté) ; 21/01 serait un R
    client.post("/api/events", json={"agent_id": "T1", "code": "VAC", "date_start": "2025-01-23",
                                     "date_end": "2025-01-23", "status": "accepte"})
    client.put("/api/capitals/T1/2025", json={"vacances": 30})
    _upload(client)
    assert client.get("/api/capitals/T1/2025").get_json() == {"vacances": 30}
    ent = client.get("/api/entitlements/T1/2025").get_json()
    assert ent["vacances"]["droit"] == 30 and ent["vacances"]["utilise"] == 1
    rap = client.get("/api/fiche_rh/T1/2025").get_json()["rapprochement"]["CONGE"]
    assert rap["communes"] == 1 and rap["rh_seulement"] == ["2025-01-21", "2025-01-24"]


def test_apply_explicite_puis_delete(client):
    client.patch("/api/agents/T1", json={"matricule": "012345"})
    _upload(client)
    r = client.post("/api/fiche_rh/T1/2025/apply")
    assert r.status_code == 200 and r.get_json()["apres"] == {"vacances": 25, "reliquat": 0}
    assert client.get("/api/capitals/T1/2025").get_json()["vacances"] == 25
    assert client.delete("/api/fiche_rh/T1/2025").get_json()["removed"] is True
    assert client.get("/api/fiche_rh/T1/2025").status_code == 404
    assert client.get("/api/capitals/T1/2025").get_json()["vacances"] == 25   # inchangé


def test_isolation_entre_agents(client):
    r = client.get("/api/fiche_rh/AUTRE/2025")
    assert r.status_code == 403
