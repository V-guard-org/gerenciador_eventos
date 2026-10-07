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

    # Telemetria a 10 Hz durante 30 s, com um evento no meio.
    for i in range(300):
        ts = 1000.0 + i / 10
        manager.update("imu", {"ax": i}, ts)
        manager.update("gps", {"lat": -25.4, "lon": -49.2}, ts)

        if i == 150:
            event_id = manager.record_event("fadiga", 0.9, JPEG, ts)

            # A parte anterior da janela já está no SD: 10 s, 2 fontes, 10 Hz.
            window = manager.window(event_id)
            assert len(window) == 202
            assert window[0]["ts"] == 1005.0 and window[-1]["ts"] == 1015.0

            # A janela ainda está aberta: o evento não entra na fila.
            assert manager.pending_events() == []

        if i == 160:
            # Cooldown vale por tipo.
            assert manager.record_event("fadiga", 0.9, JPEG, ts) is None
            assert manager.record_event("distracao", 0.7, None, ts)

    # No SD, a telemetria contínua é uma linha por segundo.
    assert manager.pending_counts()["telemetry"] == 30

    # Janela completa: 10 s antes e 10 s depois, sem amostra repetida.
    window = manager.window(event_id)
    assert len(window) == 402
    assert window[0]["ts"] == 1005.0 and window[-1]["ts"] == 1025.0

    # Evento: snapshot das fontes disponíveis e foto no disco.
    # Sem OBD-II o evento é registrado do mesmo jeito (RNF20).
    event, other = manager.pending_events()
    assert event["id"] == event_id and event["ts"] == 1015.0
    assert set(event["data"]) == {"imu", "gps"}

    (photo_id, path), = manager.pending_photos()
    assert photo_id == event_id
    assert open(path, "rb").read() == JPEG

    # Envio pelo 4G: metadados confirmados, foto continua pendente (RF18).
    manager.ack_event(event_id)

    # Queda de energia logo após um evento (sem close): a fila volta
    # como estava (RNF25) e o evento fica com a parte anterior da janela.
    cut_id = manager.record_event("fadiga", 0.8, JPEG, ts)
    manager = EventManager(data_dir)

    assert manager.pending_counts() == {
        "telemetry": 30, "events": 2, "photos": 2
    }
    assert [e["id"] for e in manager.pending_events()] == [other["id"], cut_id]
    window = manager.window(cut_id)
    assert window[0]["ts"] >= ts - 10 and window[-1]["ts"] == ts
    assert len(window) >= 200

    # Wi-Fi: fotos e telemetria confirmadas.
    manager.ack_photo(event_id)
    manager.ack_photo(cut_id)
    manager.ack_telemetry([t["id"] for t in manager.pending_telemetry()])
    counts = manager.pending_counts()
    assert counts["photos"] == 0 and counts["telemetry"] == 0

    # Confirmar não apaga nada do SD.
    assert os.path.exists(path) and len(manager.window(event_id)) == 402

    manager.close()
    print("OK")


if __name__ == "__main__":
    main()
