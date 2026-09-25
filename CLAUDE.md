# HoraireManager — Prison de Namur

Flask PWA de gestion des horaires rotatifs pour agents SPF Justice.
Déployé sur **PythonAnywhere** : https://tseli.pythonanywhere.com (compte gratuit).
Port local : 5050. (`render.yaml` / `Procfile` = anciens essais Render, service abandonné.)

## Architecture

```
app_horaire.py      # Flask app principale (127 KB) — routes + HTML inline
horaire_agent.py    # Moteur de cycle (56 jours, 8 équipes), extraction PDF
conges_bosa.py      # Catalogue congés BOSA, jours fériés, reliquats
fiche_rh.py         # Parseur PDF « Fiche de congé (Après RT) » (RH DG EPI) + rapprochement
agenda_agent.py     # Sync Google Agenda / flux iCal
lib/                # Dépendances vendorisées (flask, werkzeug, jinja2…)
agenda_data.json    # Store JSON persistant (agents, events, congés)
2026.pdf / 2027.pdf # Grilles d'horaires source
render.yaml         # Config Render (disk /data 1 GB, env DATA_DIR=/data)
```

## Modèle de données (`agenda_data.json`)

```json
{
  "agents":         { "<id>": { "name", "team_offset", "regime_4_5" } },
  "events":         [ { "aid", "date", "type", "label", "days" } ],
  "reliquats":      { "<aid>": { "<year>": float } },
  "capitals":       { "<aid>": { "sick_capital": int } },
  "exchanges":      [ { "id", "aid1", "aid2", "date1", "date2", "status" } ],
  "remarks":        { "<aid>_<date>": "texte" },
  "shift_overrides":{ "<aid>_<date>": "M|S|R|…" },
  "fiches_rh":      { "<aid>": { "<year>": { "fiche", "importee_le", "historique" } } }
}
```
`agents.<id>.matricule` (optionnel) : numéro de matricule SPF Justice, clé de
correspondance avec la fiche RH.

## Fiche RH (PDF « Fiche de congé (Après RT) »)

- Onglet **Fiche RH** : import du PDF remis par le service du personnel (Crystal
  Reports, 1 fiche = 3 pages par agent, un PDF peut contenir plusieurs agents).
  Seule la fiche dont le matricule = `agent.matricule` est conservée.
- Rubriques 5 colonnes (Réserve/Report/Droit/Pris/Solde) : CONGE, FERIES,
  COMPENSATION, JOURS DE PONT, REPOS 38H, REPOS 36H, REPOS.
  **Solde = Réserve + Report + Droit − Pris − Réduction**. « Réduction 19 j. abs »
  = 1 jour de repos 36h/38h retiré par tranche de 19 j d'absence (proratisé).
- HEURES SUPP. : Solde final = Réserve + Report N-1 + Σ mouvements (minutes).
- Rapprochement dates RH ↔ app : CONGE↔VAC, MALADIE↔MAL/MSC/MAL_LONG,
  REPOS 36H/38H ↔ jours `36`/`38` du planning (`get_day_info().base`).
- **Additif** : l'import n'écrit jamais dans `events`, `capitals`, `reliquats`.
  Seul `POST /api/fiche_rh/<aid>/<year>/apply` (bouton explicite + confirm)
  recopie CONGE.droit → `capitals.vacances` et report+réserve → `reliquats`.
- Ré-import même année : remplace, trace dans `historique` (max 12).
- Rubrique inconnue (nouveau format PDF) → stockée brute dans `fiche.inconnues`
  et signalée dans l'UI, jamais une erreur.
- Nécessite `pdfplumber` (requirements.txt). Sur PythonAnywhere :
  `pip install --user pdfplumber` si l'import renvoie « PDF illisible ».

## Logique métier clé

- **`get_shift(d, offset)`** — position dans `MASTER_CYCLE[56]`, ancre `2026-01-01`.
- **Régime 4/5** — jour désigné (lun=0…ven=4) toujours vert ; si ce jour est R/38 dans le cycle, il glisse vers la droite jusqu'au 1er M/S/36h.
- **Fériés** — conservent le poste réel mais affichent la couleur bleue.
- **iCal** — route `/ical/<aid>.ics` génère un flux VCALENDAR compatible Google.

## Routes API

| Méthode | Path | Description |
|---------|------|-------------|
| GET | `/api/agents` | Liste tous les agents |
| POST | `/api/agents` | Créer un agent |
| PATCH | `/api/agents/<aid>` | Modifier un agent |
| DELETE | `/api/agents/<aid>` | Supprimer un agent |
| GET | `/api/day/<aid>/<YYYY-MM-DD>` | Info jour complet |
| GET/PUT | `/api/remarks/<aid>/<date>` | Remarques journalières |
| PUT/DELETE | `/api/shift_override/<aid>/<date>` | Surcharge de poste |
| POST | `/api/events` | Ajouter un congé/événement |
| DELETE | `/api/events` | Supprimer un événement |
| GET | `/api/leaves_catalog` | Catalogue congés BOSA |
| GET | `/api/exchanges/<aid>` | Échanges d'un agent |
| POST | `/api/exchanges` | Créer un échange |
| PATCH | `/api/exchanges/<eid>` | Valider/refuser échange |
| GET | `/ical/<aid>.ics` | Flux iCal |
| GET | `/api/medex/status` | État de la liaison MEDEX |
| POST/DELETE | `/api/medex/link` | Relier / délier MEDEX |
| GET | `/api/fiche_rh/<aid>` | Années de fiches RH importées + matricule |
| POST | `/api/fiche_rh/<aid>/import` | Import PDF (multipart `pdf`) |
| GET/DELETE | `/api/fiche_rh/<aid>/<year>` | Fiche + rapprochement / retrait |
| POST | `/api/fiche_rh/<aid>/<year>/apply` | Recopie explicite du quota vacances RH |

## Commandes utiles

```bash
python app_horaire.py          # dev server :5050
python -m pytest tests/ -v     # tests
pip install -r requirements.txt
```

## Déploiement PythonAnywhere (prod réelle)

- URL prod : **https://tseli.pythonanywhere.com** (QR `qrcode_horaire.png` distribué aux agents)
- Compte **gratuit** : l'app doit être ré-armée chaque mois (routine « Run until 3 months
  from today » sur l'onglet Web) — Tseli a une routine mensuelle pour ça.
- **Publication manuelle** (pas d'auto-deploy) :
  1. `git push origin master` (GitHub `Tseli485/horaire-namur`)
  2. Console Bash PythonAnywhere → `git pull` dans le dossier du projet
  3. Onglet **Web** → bouton **Reload**
- Les navigateurs des utilisateurs se rechargent seuls (polling `/dev-version`, mtime de
  `app_horaire.py`) et affichent l'avis de mise à jour une fois par version.
- ⚠ `render.yaml` / `Procfile` / `horaire-namur.onrender.com` : ancien essai Render
  abandonné, version obsolète — ne pas s'y fier.

## Token Management

- Utilise RTK (installé globalement) : toutes les commandes Bash passent par `rtk` automatiquement
- Préfère `Grep`/`Glob` à `Bash(find/grep)` pour économiser les tokens
- Ne relis jamais `app_horaire.py` en entier — utilise `Read` avec `offset`+`limit` ou le graph MCP
- `agenda_data.json` ne doit jamais dépasser 500 KB — si c'est le cas, archiver les événements anciens
- En prod PythonAnywhere : `agenda_data.json` vit dans le dossier du projet (ou `DATA_DIR` si défini) — jamais dans `/tmp`

## Code Graph

Ce projet utilise le MCP `code-review-graph`. Toujours faire
`detect_changes` + `get_impact_radius` avant de modifier `horaire_agent.py`
ou `conges_bosa.py` — ils sont importés par `app_horaire.py`.

## Liaison MEDEX Manager (calendrier commun) — depuis le 25/09/2026

- **Facultative, par agent** : Mon compte → « Liaison MEDEX Manager » (e-mail + mot de passe
  du compte MEDEX, jamais stocké). Sans liaison, comportement strictement inchangé.
- **Store commun** : créé à la première liaison (champ `pid` = profil MEDEX propriétaire) ;
  tant qu'il n'existe pas, MEDEX garde son agenda habituel. Supabase MEDEX, table `donnees_agent`, collection `calendrier_commun`
  `{evenements[], remarques{}, postes{}, echanges[], version}` — la même que MEDEX lit/écrit.
  Clé anon `MEDEX_SB_KEY` (publique, RLS par utilisateur) ; jeton de rafraîchissement stocké
  dans `agenda_data.json` → `agents[aid].medex` (rotation gérée par `_medex_access`).
- **`load()` / `save()`** : `load()` superpose le commun sur les events/remarks/overrides de
  l'agent lié (`medex_superposer`, cache 10 s) ; `save()` pousse le diff (`medex_pousser`,
  instantané `g.medex_snap`) appliqué à une version FRAÎCHE du commun (`_appliquer` : une
  entrée supprimée ailleurs n'est pas ressuscitée).
- **Hors ligne / MEDEX injoignable** : lecture sur la copie locale, écriture calendrier
  REFUSÉE (`MedexIndisponible` → 503 JSON, rien n'est enregistré). Pas de file d'attente.
  Coupe-circuit : 60 s sans nouvel essai après une panne ; timeout réseau 4 s.
  Jeton révoqué → `agents[aid].medex.a_refaire` → l'agent doit « Relier à nouveau »
  (même compte MEDEX = pas de nouvelle fusion).
- **Secrets** : `_SECRETS_AGENT = (pin_hash, google, medex)` jamais renvoyés au navigateur.
- **Ids uniques** : chaque event lié porte `uid` (= id commun). Préfixes : `h-…` créé dans HM,
  `doc-cert-…` / `doc-dem-…` / `doc-soins-…` = document MEDEX (champ `doc`).
- **Anti-doublon** : première liaison = fusion clé (code, début, fin) ; `POST /api/events`
  refuse un event identique (409). Côté MEDEX, la partie d'une absence simple couverte par
  un document de la même famille est MASQUÉE (`rattache_a` = id du document), jamais
  supprimée : elle réapparaît si le document disparaît/raccourcit. HM n'affiche pas les
  entrées `rattache_a` et ne les modifie jamais.
- **Events `doc`** : suppression / confirmation refusées dans HM (400) → à gérer dans MEDEX.
- **Délier** : retire `doc`, `_commun`, `uid` des events de l'agent (redeviennent normaux).
- Routes : `GET /api/medex/status`, `POST|DELETE /api/medex/link`.
- Tests bout-en-bout HM⇄MEDEX : `outils_captures_horaire.py` (dépôt MEDEX) les rejoue avec un
  Supabase simulé en mémoire et régénère les captures du mode d'emploi.
- Mode d'emploi HoraireManager : `partage/HoraireManager_Mode_emploi.pdf` du dépôt MEDEX,
  servi sur https://medex-manager.onrender.com/download/mode-emploi-horaire.pdf.
