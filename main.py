#!/usr/bin/env python3
"""Sonde reseau -- Bureau BPO Tunis. Voir docs/network_probe.md pour le detail."""

import argparse
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    import psutil
except ImportError:
    psutil = None

# Fix courant sur macOS : le Python installe depuis python.org n'a pas
# toujours acces aux certificats racine du systeme, ce qui fait planter
# toute requete HTTPS avec "CERTIFICATE_VERIFY_FAILED". On utilise
# certifi si disponible (pip3 install certifi), sinon le contexte par
# defaut (fonctionne sur la plupart des installations Homebrew/Xcode).
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


# --- Configuration ---------------------------------------------------

PING_TARGET = "1.1.1.1"
PING_COUNT = 20

CLOUDFLARE_DOWN_URL = "https://speed.cloudflare.com/__down"
CLOUDFLARE_UP_URL = "https://speed.cloudflare.com/__up"
DOWNLOAD_TEST_BYTES = 25_000_000
UPLOAD_TEST_BYTES = 10_000_000

# Cloudflare bloque les requetes sans User-Agent "credible" (anti-bot).
# Le User-Agent par defaut d'urllib ("Python-urllib/3.x") declenche un
# 403 Forbidden -- on se fait donc passer pour un navigateur standard.
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}

# A completer toi-meme avec les process a surveiller. Deja pre-rempli
# avec les apps d'appel dediees mentionnees (detection fiable car elles
# tournent en process natif). Google Meet est a part : il tourne dans
# le navigateur (Chrome/Safari), donc pas de process dedie a detecter --
# c'est la detection "microphone actif" plus bas qui le couvre.
CALL_PROCESS_KEYWORDS = [
    "zoom.us",     # Zoom
    "zoom",
    "teams",       # Microsoft Teams
    "msteams",
    "ringover",    # Ringover (app desktop)
    "webex",
    "skype",
]

# Seuils d'alerte definis dans le projet (warning / critical).
# "higher_is_worse": True -> on alerte quand la valeur DEPASSE le seuil
# (jitter, perte, latence). Garde ici pour pouvoir reutiliser la meme
# logique meme si un jour on ajoute une metrique ou "plus bas = pire"
# (ex. debit).
THRESHOLDS = {
    "jitter_ms":            {"label": "Jitter",                    "unit": "ms", "warning": 20,  "critical": 30,  "higher_is_worse": True},
    "packet_loss_pct":      {"label": "Perte de paquets",          "unit": "%",  "warning": 0.5, "critical": 1,   "higher_is_worse": True},
    "latency_ms":           {"label": "Latence a vide",            "unit": "ms", "warning": 100, "critical": 150, "higher_is_worse": True},
    "latency_download_ms":  {"label": "Latence sous charge (down)", "unit": "ms", "warning": 150, "critical": 200, "higher_is_worse": True},
    "latency_upload_ms":    {"label": "Latence sous charge (up)",   "unit": "ms", "warning": 150, "critical": 200, "higher_is_worse": True},
}


# --- Detection d'appel actif ------------------------------------------

def is_call_process_running() -> list:
    if psutil is None or not CALL_PROCESS_KEYWORDS:
        return []
    found = []
    for proc in psutil.process_iter(attrs=["name"]):
        name = (proc.info.get("name") or "").lower()
        for keyword in CALL_PROCESS_KEYWORDS:
            if keyword.lower() in name:
                found.append(name)
                break
    return found


def is_microphone_in_use() -> bool:
    """
    Detection best-effort de l'utilisation du micro -- utile pour capter
    les appels dans le navigateur (Google Meet) qui n'ont pas de process
    dedie a chercher dans la liste CALL_PROCESS_KEYWORDS.

    A VALIDER sur le materiel reel : lance
        ioreg -c AppleH13CamIn -r -l
    pendant un appel Google Meet actif dans le navigateur, et compare
    avec le resultat au repos, pour confirmer/ajuster la cle cherchee
    ci-dessous. Retourne False (fail-safe) si indeterminable, plutot
    que de bloquer un test sur un doute technique.
    """
    try:
        result = subprocess.run(
            ["ioreg", "-c", "AppleH13CamIn", "-r", "-l"],
            capture_output=True, text=True, timeout=5,
        )
        return "\"IOAudioEngineState\" = 1" in result.stdout
    except Exception:
        return False


def call_is_active(force: bool = False) -> dict:
    if force:
        return {"active": False, "reason": "forced", "processes": []}
    procs = is_call_process_running()
    mic = is_microphone_in_use()
    active = bool(procs) or mic
    reason = "process" if procs else ("microphone" if mic else None)
    return {
        "active": active,
        "reason": reason,
        "processes": procs,
    }


# --- Identification appareil / reseau -----------------------------------

def get_device_name() -> str:
    """Nom convivial de la machine (ex. 'MacBook Air de Cyrine'), tel que
    defini dans Reglages Systeme > Partage. Retombe sur le hostname
    reseau si indisponible."""
    try:
        result = subprocess.run(
            ["scutil", "--get", "ComputerName"],
            capture_output=True, text=True, timeout=5,
        )
        name = result.stdout.strip()
        return name if name else socket.gethostname()
    except Exception:
        return socket.gethostname()


def get_connection_info() -> dict:
    """
    Identifie le reseau utilise :
      - Wi-Fi -> nom du reseau (= nom de la flybox si connexion directe)
      - Ethernet -> pas de "nom de reseau" a proprement parler, on
        retourne le nom du port materiel a la place

    Retourne {"connection_type": "wifi"|"ethernet"|"unknown", "network_name": str|None}.
    """
    try:
        route_result = subprocess.run(
            ["route", "get", "default"], capture_output=True, text=True, timeout=5,
        )
        iface_match = re.search(r"interface:\s*(\S+)", route_result.stdout)
        if not iface_match:
            return {"connection_type": "unknown", "network_name": None}
        interface = iface_match.group(1)

        hw_result = subprocess.run(
            ["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5,
        )
        hardware_port = None
        for block in hw_result.stdout.split("\n\n"):
            if f"Device: {interface}" in block:
                port_match = re.search(r"Hardware Port:\s*(.+)", block)
                if port_match:
                    hardware_port = port_match.group(1).strip()
                break

        if hardware_port and "Wi-Fi" in hardware_port:
            ssid_result = subprocess.run(
                ["networksetup", "-getairportnetwork", interface],
                capture_output=True, text=True, timeout=5,
            )
            ssid_match = re.search(r"Current Wi-Fi Network:\s*(.+)", ssid_result.stdout)
            network_name = ssid_match.group(1).strip() if ssid_match else None
            return {"connection_type": "wifi", "network_name": network_name}

        return {"connection_type": "ethernet", "network_name": hardware_port or interface}

    except Exception as e:
        print(f"[get_connection_info] echec: {e}", file=sys.stderr)
        return {"connection_type": "unknown", "network_name": None}


# --- Ping / latence / jitter / perte de paquets ------------------------

def run_ping(target: str = PING_TARGET, count: int = PING_COUNT) -> dict:
    try:
        result = subprocess.run(
            ["ping", "-c", str(count), "-i", "0.3", target],
            capture_output=True, text=True, timeout=count * 2 + 10,
        )
    except subprocess.TimeoutExpired:
        return {"latency_ms": None, "jitter_ms": None, "packet_loss_pct": 100.0}

    output = result.stdout
    loss_match = re.search(r"([\d.]+)%\s+packet loss", output)
    packet_loss_pct = float(loss_match.group(1)) if loss_match else None
    times = [float(t) for t in re.findall(r"time=([\d.]+)\s*ms", output)]

    if not times:
        return {
            "latency_ms": None,
            "jitter_ms": None,
            "packet_loss_pct": packet_loss_pct if packet_loss_pct is not None else 100.0,
        }

    latency_ms = round(sum(times) / len(times), 2)
    if len(times) > 1:
        diffs = [abs(times[i] - times[i - 1]) for i in range(1, len(times))]
        jitter_ms = round(sum(diffs) / len(diffs), 2)
    else:
        jitter_ms = 0.0

    return {
        "latency_ms": latency_ms,
        "jitter_ms": jitter_ms,
        "packet_loss_pct": packet_loss_pct,
    }


def run_ping_under_load(duration_s: int = 8) -> float:
    count = max(3, duration_s // 1)
    return run_ping(target=PING_TARGET, count=count)["latency_ms"]


# --- Test de debit (Cloudflare) -----------------------------------------

def cloudflare_download_test(num_bytes: int = DOWNLOAD_TEST_BYTES) -> Tuple[Optional[float], Optional[str]]:
    """Retourne (mbps, error). mbps est None si error n'est pas None."""
    url = f"{CLOUDFLARE_DOWN_URL}?bytes={num_bytes}"
    try:
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
            data = resp.read()
        elapsed = time.perf_counter() - start
        if elapsed <= 0 or not data:
            return None, "reponse vide ou duree nulle"
        mbps = round((len(data) * 8) / (elapsed * 1_000_000), 2)
        return mbps, None
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[cloudflare_download_test] echec: {error}", file=sys.stderr)
        return None, error


def cloudflare_upload_test(num_bytes: int = UPLOAD_TEST_BYTES) -> Tuple[Optional[float], Optional[str]]:
    """Retourne (mbps, error). mbps est None si error n'est pas None."""
    try:
        payload = os.urandom(num_bytes)
        headers = dict(DEFAULT_HEADERS)
        headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(
            CLOUDFLARE_UP_URL,
            data=payload,
            method="POST",
            headers=headers,
        )
        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
            resp.read()
        elapsed = time.perf_counter() - start
        if elapsed <= 0:
            return None, "duree nulle"
        mbps = round((num_bytes * 8) / (elapsed * 1_000_000), 2)
        return mbps, None
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        print(f"[cloudflare_upload_test] echec: {error}", file=sys.stderr)
        return None, error


def run_speedtest() -> dict:
    import threading

    latency_during = {"download": None, "upload": None}

    def ping_during(phase):
        latency_during[phase] = run_ping_under_load(duration_s=6)

    t_ping = threading.Thread(target=ping_during, args=("download",))
    t_ping.start()
    download_mbps, download_error = cloudflare_download_test()
    t_ping.join()

    t_ping = threading.Thread(target=ping_during, args=("upload",))
    t_ping.start()
    upload_mbps, upload_error = cloudflare_upload_test()
    t_ping.join()

    result = {
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "latency_download_ms": latency_during["download"],
        "latency_upload_ms": latency_during["upload"],
    }
    # Ces champs n'apparaissent que si une erreur reseau s'est produite --
    # ca repond directement a "est-ce que le resultat est reel ou pas".
    if download_error:
        result["download_error"] = download_error
    if upload_error:
        result["upload_error"] = upload_error
    return result


# --- Payload -------------------------------------------------------------

def build_payload(mode: str, modem_id: int, agent_id: str, force: bool) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    connection_info = get_connection_info()

    payload = {
        "modem_id": modem_id,
        "agent_id": agent_id,
        "device_name": get_device_name(),
        "connection_type": connection_info["connection_type"],
        "network_name": connection_info["network_name"],
        "timestamp": timestamp,
        "download_mbps": None,
        "upload_mbps": None,
        "latency_ms": None,
        "latency_download_ms": None,
        "latency_upload_ms": None,
        "jitter_ms": None,
        "packet_loss_pct": None,
    }

    payload.update(run_ping())

    if mode == "full":
        call_status = call_is_active(force=force)
        if call_status["active"]:
            payload["skipped"] = True
            if call_status["reason"] == "microphone":
                payload["skip_reason"] = "microphone actif detecte (probable appel navigateur, ex. Google Meet)"
            else:
                payload["skip_reason"] = f"appel actif detecte ({call_status['processes']})"
            return payload

        payload.update(run_speedtest())
        payload["skipped"] = False

    return payload


def send_payload(payload: dict, endpoint: str) -> None:
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"-> POST envoye, status: {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"-> Echec envoi vers {endpoint}: {e}", file=sys.stderr)


def evaluate_thresholds(payload: dict) -> dict:
    """
    Compare chaque metrique du payload aux seuils du projet.
    Retourne un dict {metrique: {"value":..., "status": "ok"|"warning"|"critical"}}
    plus une cle "global" = le pire statut trouve (utile pour la couleur
    du dashboard plus tard : vert/orange/rouge).
    """
    per_metric = {}
    worst = "ok"
    order = {"ok": 0, "warning": 1, "critical": 2}

    for key, cfg in THRESHOLDS.items():
        value = payload.get(key)
        if value is None:
            per_metric[key] = {"value": None, "status": "n/a"}
            continue
        if value > cfg["critical"]:
            status = "critical"
        elif value > cfg["warning"]:
            status = "warning"
        else:
            status = "ok"
        per_metric[key] = {"value": value, "status": status}
        if order.get(status, 0) > order.get(worst, 0):
            worst = status

    per_metric["global"] = worst
    return per_metric


STATUS_ICON = {"ok": "OK", "warning": "WARNING", "critical": "CRITICAL", "n/a": "N/A"}


def render_comparison_table(payload: dict, evaluation: dict) -> str:
    """Construit un tableau Markdown comparant chaque metrique a ses seuils,
    pret a etre affiche dans le terminal ou ajoute a un fichier .md."""
    lines = []
    lines.append(f"### {payload.get('agent_id')} — modem {payload.get('modem_id')} — {payload.get('timestamp')}")
    lines.append("")
    lines.append(f"- **Appareil** : {payload.get('device_name')}")
    network_label = payload.get("network_name") or "inconnu"
    lines.append(f"- **Connexion** : {payload.get('connection_type')} — {network_label}")
    if payload.get("skipped"):
        lines.append(f"- **Test de debit** : SAUTE — {payload.get('skip_reason')}")
    lines.append("")
    lines.append("| Metrique | Valeur | Warning | Critical | Statut |")
    lines.append("|---|---|---|---|---|")
    for key, cfg in THRESHOLDS.items():
        entry = evaluation.get(key, {"value": None, "status": "n/a"})
        value = entry["value"]
        value_str = f"{value} {cfg['unit']}" if value is not None else "n/a"
        status_str = STATUS_ICON.get(entry["status"], entry["status"])
        lines.append(f"| {cfg['label']} | {value_str} | > {cfg['warning']} {cfg['unit']} | > {cfg['critical']} {cfg['unit']} | {status_str} |")
    if payload.get("download_mbps") is not None:
        lines.append(f"| Debit download | {payload['download_mbps']} Mbps | - | - | info |")
    if payload.get("upload_mbps") is not None:
        lines.append(f"| Debit upload | {payload['upload_mbps']} Mbps | - | - | info |")
    lines.append("")
    lines.append(f"**Statut global : {STATUS_ICON.get(evaluation.get('global'), 'n/a')}**")
    lines.append("")
    return "\n".join(lines)


def save_payload_to_file(payload: dict, filepath: str) -> None:
    """Ajoute le payload (1 ligne JSON) a la fin de `filepath`.
    Format JSONL : chaque run = une ligne, facile a relire ligne par
    ligne plus tard (pandas, jq, etc.) sans reparser tout le fichier."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    print(f"-> Resultat ajoute a {filepath}", file=sys.stderr)


def save_comparison_to_file(comparison_md: str, filepath: str) -> None:
    """Ajoute le comparatif Markdown a la fin de `filepath` (un par appareil).
    Cree le fichier et le dossier au premier appel."""
    directory = os.path.dirname(filepath)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(comparison_md + "\n")
    print(f"-> Comparatif ajoute a {filepath}", file=sys.stderr)


def sanitize_filename(name: str) -> str:
    """Remplace les caracteres problematiques pour un nom de fichier."""
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name)


def main():
    parser = argparse.ArgumentParser(description="Sonde reseau BPO Tunis")
    parser.add_argument("--mode", choices=["ping", "full"], required=True)
    parser.add_argument("--modem-id", type=int, default=1)
    parser.add_argument("--agent-id", type=str, default=socket.gethostname())
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--endpoint", type=str, default=None)
    parser.add_argument("--results-dir", type=str, default="results",
                         help="Dossier ou stocker les fichiers de resultats, un par appareil.")
    parser.add_argument("--output-file", type=str, default=None,
                         help="Chemin du fichier JSONL pour ce run. Par defaut : "
                              "<results-dir>/<agent_id>.jsonl. Vide ('') pour desactiver.")
    parser.add_argument("--comparison-file", type=str, default=None,
                         help="Chemin du fichier .md pour le comparatif de ce run. Par defaut : "
                              "<results-dir>/<agent_id>.md. Vide ('') pour desactiver.")
    args = parser.parse_args()

    payload = build_payload(
        mode=args.mode,
        modem_id=args.modem_id,
        agent_id=args.agent_id,
        force=args.force,
    )

    evaluation = evaluate_thresholds(payload)
    payload["evaluation"] = evaluation

    print(json.dumps(payload, indent=2, ensure_ascii=False))

    comparison_md = render_comparison_table(payload, evaluation)
    print("\n" + comparison_md)

    safe_agent = sanitize_filename(args.agent_id)

    output_file = args.output_file
    if output_file is None:
        output_file = os.path.join(args.results_dir, f"{safe_agent}.jsonl")
    if output_file:
        save_payload_to_file(payload, output_file)

    comparison_file = args.comparison_file
    if comparison_file is None:
        comparison_file = os.path.join(args.results_dir, f"{safe_agent}.md")
    if comparison_file:
        save_comparison_to_file(comparison_md, comparison_file)

    if args.endpoint:
        send_payload(payload, args.endpoint)


if __name__ == "__main__":
    main()