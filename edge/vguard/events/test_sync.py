"""
Verificação da sincronização contra um backend falso local. Executar:

    python test_sync.py
    python3 test_sync.py
"""

import http.server
import json
import tempfile
import threading

import sync
from event_manager import EventManager

JPEG = b"\xff\xd8\xff\xe0 foto de teste \xff\xd9"


class Backend(http.server.BaseHTTPRequestHandler):

    received = []   # (caminho, corpo) do que foi confirmado
    fault = None    # (sufixo do caminho, "drop" ou status HTTP), uma vez só

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        assert self.headers["Authorization"] == "Bearer segredo"

        status = 200
        if Backend.fault and self.path.endswith(Backend.fault[0]):
            status, Backend.fault = Backend.fault[1], None

        if status == "drop":
            # Conexão cai no meio do envio, sem resposta.
            self.close_connection = True
            return

        if status == 200:
            Backend.received.append((self.path, body))

        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT

    def log_message(self, *args):
        pass


def received(suffix):
    return [body for path, body in Backend.received if path.endswith(suffix)]


def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Backend)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    sync.BACKEND_URL = "http://127.0.0.1:%d/api" % server.server_port
    sync.DEVICE_TOKEN = "segredo"

    manager = EventManager(tempfile.mkdtemp())
    for i in range(300):
        ts = 1000.0 + i / 10
        manager.update("imu", {"ax": i}, ts)
        if i == 100:
            first = manager.record_event("fadiga", 0.9, JPEG, ts)
        if i == 110:
            bad = manager.record_event("distracao", 0.7, JPEG, ts)

    link = [None]
    service = sync.Sync(manager, link=lambda: link[0])

    # Sem enlace: nada sai, tudo pendente.
    assert service.sync_once() is None and Backend.received == []
    assert manager.pending_counts() == {
        "telemetry": 30, "events": 2, "photos": 2
    }

    # 4G: saúde, telemetria e metadados (com janela); nenhuma foto.
    # O backend recusa um dos eventos (422): vira FAILED e sai da fila.
    link[0] = "cellular"
    Backend.fault = ("/events/" + bad, 422)
    assert service.sync_once() == "cellular"

    assert len(json.loads(received("/telemetry")[0])["items"]) == 30
    event, = [json.loads(body) for body in received("/events/" + first)]
    assert event["type"] == "fadiga" and len(event["window"]) == 201
    assert received("/photo") == [] and received("/events/" + bad) == []
    assert manager.pending_counts() == {
        "telemetry": 0, "events": 0, "photos": 1
    }
    assert service.last_contact is not None

    # Nada é reenviado no ciclo seguinte, nem o evento recusado.
    count = len(Backend.received)
    service.sync_once()
    assert len(Backend.received) == count + 1   # só a saúde

    # Wi-Fi com upload interrompido: a foto continua pendente...
    link[0] = "wifi"
    Backend.fault = ("/photo", "drop")
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except OSError:
        pass
    assert received("/photo") == []
    assert [i for i, _ in manager.pending_photos()] == [first]

    # ...e é reenviada inteira no ciclo seguinte.
    assert service.sync_once() == "wifi"
    assert received("/photo") == [JPEG]
    assert manager.pending_photos() == []

    # Backend respondendo 500: o dado novo continua pendente.
    manager.update("imu", {"ax": 0}, 2000.0)
    Backend.fault = ("/telemetry", 500)
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except sync.Retry:
        pass
    assert manager.pending_counts()["telemetry"] == 1

    # Backend fora do ar: idem.
    server.shutdown()
    server.server_close()
    try:
        service.sync_once()
        assert False, "deveria ter falhado"
    except OSError:
        pass
    assert manager.pending_counts()["telemetry"] == 1

    manager.close()
    print("OK")


if __name__ == "__main__":
    main()
