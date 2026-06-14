# HoraireManager — Prison de Namur

Flask PWA de gestion des horaires rotatifs pour agents SPF Justice.
Déployé sur **Render** (`render.yaml`). Port local : 5050.

## Architecture

```
app_horaire.py      # Flask app principale (127 KB) — routes + HTML inline
horaire_agent.py    # Moteur de cycle (56 jours, 8 équipes), extraction PDF
conges_bosa.py      # Catalogue congés BOSA, jours fériés, reliquats
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
  "shift_overrides":{ "<aid>_<date>": "M|S|R|…" }
}
```

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

## Commandes utiles

```bash
python app_horaire.py          # dev server :5050
python -m pytest tests/ -v     # tests
pip install -r requirements.txt
```

## Déploiement Render

Disk persistant monté en `/data` (variable `DATA_DIR`). Ne jamais écrire
`agenda_data.json` en dehors de ce dossier en prod.

## Token Management

- Utilise RTK (installé globalement) : toutes les commandes Bash passent par `rtk` automatiquement
- Préfère `Grep`/`Glob` à `Bash(find/grep)` pour économiser les tokens
- Ne relis jamais `app_horaire.py` en entier — utilise `Read` avec `offset`+`limit` ou le graph MCP
- `agenda_data.json` ne doit jamais dépasser 500 KB — si c'est le cas, archiver les événements anciens
- En prod Render : `DATA_DIR=/data` (disk persistant), jamais en local `/tmp`

## Code Graph

Ce projet utilise le MCP `code-review-graph`. Toujours faire
`detect_changes` + `get_impact_radius` avant de modifier `horaire_agent.py`
ou `conges_bosa.py` — ils sont importés par `app_horaire.py`.
