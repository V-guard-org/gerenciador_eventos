"""
Verificação do gerenciador de eventos. Executar:

    python test_event_manager.py
    python3 test_event_manager.py
"""

import os
import random
import signal
import subprocess
import sys
import tempfile
import time

import event_manager
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

    # Envio pelo 4G: metadados confirmados, foto continua pendente (RF18).
    # A foto só entra na fila depois dos metadados.
    assert manager.pending_photos() == []
    manager.ack_event(event_id)

    (photo_id, path), = manager.pending_photos()
    assert photo_id == event_id
    assert open(path, "rb").read() == JPEG

    # Queda de energia logo após um evento (sem close): a fila volta
    # como estava (RNF25) e o evento fica com a parte anterior da janela.
    cut_id = manager.record_event("fadiga", 0.8, JPEG, ts)

    # Restos de um corte no meio da gravação de uma foto: somem no boot.
    for name in ("meia-foto.jpg.tmp", "sem-evento.jpg"):
        open(os.path.join(manager.photo_dir, name), "wb").write(JPEG)

    manager = EventManager(data_dir)
    assert len(os.listdir(manager.photo_dir)) == 2

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

    # Limpeza: com o SD abaixo do alerta, nada é apagado.
    assert manager.storage_status()["level"] == "ok"
    manager.cleanup()
    assert os.path.exists(path)

    # No alerta, sai só o que o backend confirmou por inteiro. `cut_id`
    # ainda tem os metadados pendentes: fica com foto e janela.
    event_manager.STORAGE_WARN = 0.0
    manager.cleanup()
    event_manager.STORAGE_WARN = 0.80

    assert not os.path.exists(path) and manager.window(event_id) == []
    assert [e["id"] for e in manager.pending_events()] == [other["id"], cut_id]
    assert os.listdir(manager.photo_dir) == [cut_id + ".jpg"]
    assert len(manager.window(cut_id)) >= 200

    manager.close()
    print("OK")


# ============================================================
# CORTE ABRUPTO
# ============================================================

BIG_JPEG = JPEG * 20000   # ~500 kB, para o corte cair no meio de uma foto


def writer(data_dir):
    """
    Grava telemetria e eventos sem parar, até ser morto.
    """
    manager = EventManager(data_dir)
    i = 0

    while True:
        i += 1
        manager.update("imu", {"ax": i}, i * 0.1)

        if i % 60 == 0:
            manager.record_event("fadiga", 0.9, BIG_JPEG, i * 0.1)


def test_abrupt_cut(rounds=10):
    # ponytail: SIGKILL corta o processo, não a energia: valida a recuperação
    # do banco e das fotos, mas não o fsync. O corte real é puxar a fonte da
    # Raspberry com este writer rodando e depois conferir com check().
    data_dir = tempfile.mkdtemp()

    for _ in range(rounds):
        process = subprocess.Popen(
            [sys.executable, __file__, "writer", data_dir]
        )
        time.sleep(random.uniform(0.3, 1.0))
        assert process.poll() is None
        process.send_signal(signal.SIGKILL)
        process.wait()

        events = check(data_dir)

    assert events > 0
    print("OK: %d cortes, %d eventos íntegros" % (rounds, events))


def check(data_dir):
    """
    Reabre o diretório e confere que tudo que ficou está íntegro.
    """
    manager = EventManager(data_dir)
    db = manager._db

    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    # Toda foto no disco pertence a um evento, e vice-versa, inteira.
    photos = [r[0] for r in db.execute("SELECT photo FROM events")]
    assert sorted(os.listdir(manager.photo_dir)) == sorted(photos)
    for name in photos:
        assert os.path.getsize(
            os.path.join(manager.photo_dir, name)
        ) == len(BIG_JPEG)

    # A fila volta inteira: todo evento gravado está pendente.
    counts = manager.pending_counts()
    assert counts["events"] == counts["photos"] == len(photos)
    assert len(manager.pending_events(limit=10**6)) == len(photos)

    manager.close()
    return len(photos)


if __name__ == "__main__":
    if sys.argv[1:2] == ["writer"]:
        writer(sys.argv[2])
    elif sys.argv[1:2] == ["check"]:
        print("OK: %d eventos íntegros" % check(sys.argv[2]))
    else:
        main()
        test_abrupt_cut()
