"""
Verificação do gerenciador de eventos. Executar:

    python test_event_manager.py
"""

import os
import tempfile

from event_manager import EventManager

JPEG = b"\xff\xd8\xff\xe0 foto de teste \xff\xd9"


def main():
    data_dir = tempfile.mkdtemp()
    manager = EventManager(data_dir)

    # Telemetria a 10 Hz durante 30 s -> uma linha por segundo no SD.
    for i in range(300):
        ts = 1000.0 + i / 10
        manager.update("imu", {"ax": i}, ts)
        manager.update("gps", {"lat": -25.4, "lon": -49.2}, ts)
    assert manager.pending_counts()["telemetry"] == 30

    # Evento: id único, snapshot das fontes disponíveis, foto no disco.
    # Sem OBD-II o evento é registrado do mesmo jeito (RNF20).
    event_id = manager.record_event("fadiga", 0.9, JPEG, ts=1015.0)
    event, = manager.pending_events()
    assert event["id"] == event_id and event["ts"] == 1015.0
    assert set(event["data"]) == {"imu", "gps"}

    (photo_id, path), = manager.pending_photos()
    assert photo_id == event_id
    assert open(path, "rb").read() == JPEG

    # Cooldown vale por tipo.
    assert manager.record_event("fadiga", 0.9, JPEG, ts=1016.0) is None
    assert manager.record_event("distracao", 0.7, None, ts=1016.0)

    # Janela: 10 s antes e 10 s depois.
    window = manager.window(event_id)
    assert window[0]["ts"] >= 1005.0 and window[-1]["ts"] <= 1025.0
    assert len(window) >= 20

    # Envio pelo 4G: metadados confirmados, foto continua pendente (RF18).
    manager.ack_event(event_id)
    counts = manager.pending_counts()
    assert counts["events"] == 1 and counts["photos"] == 1

    # Reinicialização: a fila volta como estava (RNF25).
    manager.close()
    manager = EventManager(data_dir)
    assert manager.pending_counts() == counts
    assert manager.pending_photos()[0][0] == event_id

    # Wi-Fi: foto e telemetria confirmadas.
    manager.ack_photo(event_id)
    manager.ack_telemetry([t["id"] for t in manager.pending_telemetry()])
    counts = manager.pending_counts()
    assert counts["photos"] == 0 and counts["telemetry"] == 0

    # Confirmar não apaga nada do SD.
    assert os.path.exists(path) and len(manager.window(event_id)) >= 20

    print("OK")


if __name__ == "__main__":
    main()
