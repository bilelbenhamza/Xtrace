# Sonde réseau — Bureau BPO Tunis

Documentation du script `main.py`.

## À quoi il sert

Le script tourne en local sur chaque Mac Mini M4, mesure la qualité de
la connexion 5G du modem auquel la machine est branchée, et sort un
JSON prêt à être envoyé au backend central (VPS OVH).

Il est conçu pour être lancé par `launchd` (pas `cron`) selon deux
modes :

| Mode | Fréquence | Portée | Ce qu'il mesure |
|---|---|---|---|
| `--mode ping` | Toutes les 15–30 min | Les 4 machines du modem | Latence à vide, jitter, perte de paquets |
| `--mode full` | 1x/heure | 1 seule machine par modem, en rotation | Tout ce qui précède + débit download/upload + latence sous charge |

Avant de lancer un test de débit (mode `full`), le script vérifie
qu'aucun appel n'est en cours sur la machine. Si c'est le cas, il
annule le cycle — le test sera retenté au prochain déclenchement
`launchd`.

## Pourquoi Cloudflare pour le test de débit

Plutôt que de dépendre d'un paquet pip tiers (type `speedtest-cli`,
qui dépend de l'infra Ookla), le script appelle directement les
endpoints publics utilisés par le testeur officiel Cloudflare
(https://speed.cloudflare.com) :

- `GET https://speed.cloudflare.com/__down?bytes=N` — télécharge N
  octets, sert à mesurer le débit descendant.
- `POST https://speed.cloudflare.com/__up` — on envoie un corps de N
  octets, sert à mesurer le débit montant.

Avantages :
- Zéro dépendance externe (juste `urllib`, déjà dans la lib standard
  Python).
- Infra Cloudflare fiable et proche géographiquement (edge network).
- Contrôle total sur la taille des fichiers testés — important pour
  ne pas saturer un modem partagé par 4 agents en plein appel.

Tailles utilisées par défaut : **25 Mo** en download, **10 Mo** en
upload (configurable via `DOWNLOAD_TEST_BYTES` / `UPLOAD_TEST_BYTES`
en haut du script).

## Dépendances

```bash
pip3 install psutil
```

`psutil` sert uniquement à lister les process actifs pour la
détection d'appel — tout le reste (ping, test de débit) n'utilise que
la librairie standard Python.

## Détection d'appel actif

La liste `CALL_PROCESS_KEYWORDS` (en haut du script) est vide par
défaut — chaque bureau a ses propres outils. À compléter avec le(s)
nom(s) exact(s) de process à surveiller :

```python
CALL_PROCESS_KEYWORDS = ["zoom.us", "teams", "monsoftphone"]
```

Pour trouver le nom exact d'un process sur macOS pendant un appel :
- Ouvrir **Moniteur d'activité** et regarder la colonne "Nom du
  processus", ou
- Lancer `ps aux | grep -i zoom` dans un terminal pendant que l'appel
  est actif.

La recherche est insensible à la casse et par sous-chaîne (`"zoom"`
matche `"zoom.us"`, `"ZoomOpener"`, etc.).

## Fonctions du script

### Détection d'appel

- **`is_call_process_running()`** — Garde-fou principal avant un test
  de débit. Parcourt tous les process actifs et retourne ceux qui
  correspondent à un mot-clé de `CALL_PROCESS_KEYWORDS`. Retourne une
  liste vide si `psutil` n'est pas installé ou si la liste de
  mots-clés est vide (fail-open volontaire : mieux vaut louper une
  détection que planter tout le script).

- **`call_is_active(force)`** — Centralise la décision "on peut
  lancer le test de débit ou pas". Le flag `--force` bypass cette
  vérification pour les tests manuels/debug.

### Latence, jitter, perte de paquets

- **`run_ping(target, count)`** — Mesure de base, exécutée à chaque
  cycle (`ping` et `full`), car peu coûteuse en bande passante. Lance
  la commande `ping` système (format BSD/macOS) et parse la sortie
  pour extraire :
  - `latency_ms` : moyenne des temps de réponse individuels
  - `jitter_ms` : moyenne des écarts absolus entre pings consécutifs
    (définition courante en supervision réseau, proche de RFC 3550)
  - `packet_loss_pct` : pourcentage de paquets perdus

  Retourne des valeurs à `None`/`100.0` si la ligne est totalement
  coupée, plutôt que de lever une exception.

- **`run_ping_under_load(duration_s)`** — La latence "à vide" ne
  suffit pas à détecter le problème le plus critique pour des appels
  vocaux : la latence qui augmente quand la ligne est chargée
  (bufferbloat). Cette fonction relance un ping (plus court) pendant
  qu'un transfert est en cours ailleurs dans le script, via un thread
  séparé.

### Test de débit

- **`cloudflare_download_test(num_bytes)`** — `GET` vers
  `/__down?bytes=N`, chronomètre le téléchargement, convertit en
  Mbps : `Mbps = (octets reçus × 8) / (secondes × 1_000_000)`.
  Retourne `None` en cas d'échec réseau plutôt que de planter le
  cycle de mesure.

- **`cloudflare_upload_test(num_bytes)`** — Génère `num_bytes` de
  données aléatoires en mémoire, les envoie en `POST` vers `/__up`,
  chronomètre l'envoi, convertit en Mbps de la même façon.

- **`run_speedtest()`** — Fonction appelée en mode `full`. Lance le
  download puis l'upload Cloudflare (jamais en même temps, pour ne
  pas cumuler leur impact sur la bande passante partagée du modem),
  et mesure en parallèle (thread séparé) la latence "sous charge"
  pendant chacun des deux via `run_ping_under_load()`.

### Assemblage et envoi

- **`build_payload(mode, modem_id, agent_id, force)`** — Point
  d'entrée unique qui assemble le JSON final :
  1. Mesure toujours latence/jitter/perte de paquets.
  2. En mode `full` uniquement : vérifie qu'aucun appel n'est en
     cours. Si détecté, le test de débit est sauté pour ce cycle (le
     champ `skipped` documente pourquoi).
  3. Sinon, lance `run_speedtest()` et fusionne le résultat.

- **`send_payload(payload, endpoint)`** — POST du payload en JSON
  vers le backend, avec timeout court pour ne jamais bloquer le
  script si le backend est injoignable.

## Format du JSON produit

```json
{
  "modem_id": 1,
  "agent_id": "macmini-agent-03",
  "timestamp": "2026-09-10T09:15:00+00:00",
  "download_mbps": 84.2,
  "upload_mbps": 12.7,
  "latency_ms": 22.5,
  "latency_download_ms": 45.1,
  "latency_upload_ms": 61.8,
  "jitter_ms": 8.3,
  "packet_loss_pct": 0.0,
  "skipped": false
}
```

Si un appel est détecté en mode `full` :

```json
{
  "modem_id": 1,
  "agent_id": "macmini-agent-03",
  "timestamp": "2026-09-10T09:15:00+00:00",
  "latency_ms": 22.5,
  "jitter_ms": 8.3,
  "packet_loss_pct": 0.0,
  "skipped": true,
  "skip_reason": "appel actif detecte (['zoom.us'])"
}
```

## Usage

```bash
# Mesure légère seule
python3 main.py --mode ping

# Mesure complète (débit inclus)
python3 main.py --mode full

# Ignorer la détection d'appel (debug uniquement)
python3 main.py --mode full --force

# Préciser modem/agent manuellement
python3 main.py --mode ping --modem-id 3 --agent-id agent-macmini-07

# Envoyer directement au backend
python3 main.py --mode full --endpoint http://localhost:8000/api/metrics
```

Sans `--endpoint`, le script affiche juste le JSON sur `stdout` — pratique
pour tester sans backend.

## Prochaines étapes


pip3 install psutil
python3 main.py --mode full --force

export NETWORK_PROBE_TOKEN="oIk7nlOzhG7uiPIl"

export NETWORK_PROBE_TOKEN="d5648e68808d18c6c9dc40fdef14f297178d28b05f12abf95975274d594a8a95"
python3 main.py --mode full --force \
  --endpoint https://ywinfjuzqipyozzyljil.supabase.co/functions/v1/ingest-metric


  ^Ping continue , send whenever it pass or not during the day^