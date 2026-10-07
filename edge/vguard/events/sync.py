"""
V-GUARD — Sincronização com o backend por Wi-Fi ou 4G.

Envia o que o EventManager tem pendente e só marca como confirmado o
que o backend respondeu com 2xx (RF22):

    Wi-Fi autorizado   saúde + telemetria + eventos + fotografias
    4G                 saúde + telemetria + eventos (sem fotografias)
    sem enlace         nada; tudo continua pendente no SD

Roda em uma thread do mesmo processo do EventManager:

    Sync(manager).start()
"""

import http.client
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.parse

from event_manager import CONFIRMED, FAILED


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Contrato PROVISÓRIO, até a API do backend ser definida:
#
#   POST /health               JSON
#   POST /telemetry            JSON {"items": [{id, ts, data}, ...]}
#   PUT  /events/<id>          JSON {id, type, ts, score, data, window}
#   PUT  /events/<id>/photo    image/jpeg
#
# Os PUT levam o id do evento na URL: reenviar não duplica (RNF26).

BACKEND_URL = os.environ.get("VGUARD_BACKEND", "https://vguard.example/api")
DEVICE_ID = os.environ.get("VGUARD_DEVICE", "vguard-01")
DEVICE_TOKEN = os.environ.get("VGUARD_TOKEN", "")

# Nomes das conexões Wi-Fi do NetworkManager autorizadas a levar fotos.
AUTHORIZED_WIFI = []

# Interfaces do módulo 4G quando ele aparece como placa de rede USB.
# (Se aparecer como modem "gsm" no NetworkManager, já é reconhecido.)
CELLULAR_DEVICES = ["usb0", "wwan0"]

# Intervalo entre ciclos de envio (RF29). Cada ciclo custa uma conexão
# HTTPS (~6 kB) além dos dados: é o que mais pesa na franquia do 4G.
SYNC_INTERVAL = 60.0

# Após uma falha o intervalo dobra a cada tentativa, até este teto.
RETRY_MAX = 600.0

TELEMETRY_BATCH = 200
HTTP_TIMEOUT = 20.0

# Respostas em que o backend recusa o dado em si: reenviar não adianta.
# Qualquer outro erro (rede, 5xx, 401, 404...) é tentado de novo.
REJECTED = (400, 413, 422)


class Retry(Exception):
    """O backend respondeu, mas não confirmou: tentar de novo depois."""


# ============================================================
# ENLACE
# ============================================================

def active_link():
    """
    Retorna "wifi", "cellular" ou None.

    Pergunta ao kernel por qual interface o tráfego até o backend sai de
    fato. A precedência do Wi-Fi sobre o 4G (RF30) é a métrica de rota do
    sistema: a conexão Wi-Fi precisa ter métrica menor que a do 4G.
    """
    try:
        host = urllib.parse.urlsplit(BACKEND_URL).hostname
        route = _run("ip", "-o", "route", "get", socket.gethostbyname(host))
        device = route.split()[route.split().index("dev") + 1]

        if device in CELLULAR_DEVICES:
            return "cellular"

        kind, connection = _run(
            "nmcli", "-g", "GENERAL.TYPE,GENERAL.CONNECTION",
            "device", "show", device
        ).splitlines()[:2]

    except (OSError, ValueError, subprocess.SubprocessError):
        return None

    if kind == "gsm":
        return "cellular"
    if kind == "wifi" and connection in AUTHORIZED_WIFI:
        return "wifi"
    return None


def _run(*command):
    return subprocess.run(
        command, capture_output=True, text=True, timeout=5, check=True
    ).stdout


def _read_number(path):
    try:
        with open(path) as f:
            return int(f.read())
    except (OSError, ValueError):
        return None


# ============================================================
# SINCRONIZAÇÃO
# ============================================================

class Sync:

    def __init__(self, manager, link=active_link):
        self.manager = manager
        self.link = link
        self.last_contact = None
        self._stop = threading.Event()

    def sync_once(self):
        """
        Um ciclo de envio. Retorna o enlace usado (ou None).

        Erro de rede ou Retry interrompem o ciclo; o que não foi
        confirmado continua pendente.
        """
        link = self.link()
        if link is None:
            return None

        manager = self.manager
        url = urllib.parse.urlsplit(BACKEND_URL)
        connection = (
            http.client.HTTPSConnection if url.scheme == "https"
            else http.client.HTTPConnection
        )(url.netloc, timeout=HTTP_TIMEOUT)

        def send(method, path, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body).encode()

            connection.request(method, url.path + path, body, {
                "Content-Type": content_type,
                "Authorization": "Bearer " + DEVICE_TOKEN,
                "X-Device-Id": DEVICE_ID,
            })
            response = connection.getresponse()
            response.read()

            if response.status in REJECTED:
                logging.warning("backend recusou %s: %d", path, response.status)
                return FAILED
            if not 200 <= response.status < 300:
                raise Retry("%s: HTTP %d" % (path, response.status))

            self.last_contact = time.time()
            return CONFIRMED

        try:
            send("POST", "/health", self.health(link))

            while batch := manager.pending_telemetry(TELEMETRY_BATCH):
                state = send("POST", "/telemetry", {"items": batch})
                manager.ack_telemetry([item["id"] for item in batch], state)

            # a janela vai junto com os metadados, inclusive no 4G
            # (~150 kB por evento com IMU a 100 Hz). Se pesar na franquia,
            # dar a ela um estado próprio e mandar só por Wi-Fi.
            for event in manager.pending_events():
                event["window"] = manager.window(event["id"])
                state = send("PUT", "/events/" + event["id"], event)
                manager.ack_event(event["id"], state)

            for event_id, path in manager.pending_photos():
                # reconfere o enlace a cada foto, mas se o Wi-Fi cair
                # entre a conferência e o envio, essa foto pode sair pelo 4G.
                # Para garantia total, prender o socket à wlan0 (SO_BINDTODEVICE).
                if self.link() != "wifi":
                    break

                with open(path, "rb") as f:
                    jpeg = f.read()

                state = send(
                    "PUT", "/events/" + event_id + "/photo", jpeg, "image/jpeg"
                )
                manager.ack_photo(event_id, state)

        finally:
            connection.close()

        return link

    def health(self, link):
        """
        Saúde do dispositivo (RF32).
        """
        temperature = _read_number("/sys/class/thermal/thermal_zone0/temp")

        # Bytes desde o boot: o contador zera a cada reinicialização.
        cellular_bytes = sum(
            _read_number(
                "/sys/class/net/%s/statistics/%s" % (device, counter)
            ) or 0
            for device in CELLULAR_DEVICES
            for counter in ("rx_bytes", "tx_bytes")
        )

        return {
            "ts": time.time(),
            "link": link,
            "pending": self.manager.pending_counts(),
            "storage": self.manager.storage_status(),
            "temperature": temperature and temperature / 1000,
            "cellular_bytes": cellular_bytes,
            "last_contact": self.last_contact,
        }

    # --------------------------------------------------------

    def run(self):
        delay = SYNC_INTERVAL

        while not self._stop.wait(delay):
            try:
                self.sync_once()
                delay = SYNC_INTERVAL

            except (OSError, http.client.HTTPException, Retry) as error:
                delay = min(delay * 2, RETRY_MAX)
                logging.warning(
                    "sincronização falhou (%s); nova tentativa em %.0f s",
                    error, delay
                )

    def start(self):
        threading.Thread(target=self.run, daemon=True).start()

    def stop(self):
        self._stop.set()
