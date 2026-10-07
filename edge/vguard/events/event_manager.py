"""
V-GUARD — Gerenciador de eventos local-first.

Tudo é gravado no cartão SD antes de qualquer tentativa de envio (RF14):

    dados/
    ├── vguard.db        SQLite: telemetria consolidada, eventos e estados de sincronização
    └── photos/<id>.jpg  fotografia de cada evento (RNF13)

Quem escreve: visão (record_event) e leitores de GPS / ESP32 (update).
Quem lê: o serviço de sincronização (pending_* / ack_*).
"""

import json
import os
import sqlite3
import threading
import time
import uuid


# ============================================================
# CONFIGURAÇÃO
# ============================================================

# Valores INICIAIS, a calibrar em bancada.

# Intervalo de consolidação da telemetria gravada no SD (RNF24).
TELEMETRY_INTERVAL = 1.0

# Tempo mínimo entre dois eventos do mesmo tipo. Sem isso, a visão
# geraria um evento por frame enquanto os olhos estiverem fechados.
EVENT_COOLDOWN = 5.0

# Janela de telemetria antes/depois do evento (RF17).
WINDOW_SECONDS = 10.0


SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry (
    id     INTEGER PRIMARY KEY,
    ts     REAL NOT NULL,
    data   TEXT NOT NULL,
    synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS telemetry_ts ON telemetry (ts);
CREATE INDEX IF NOT EXISTS telemetry_pending ON telemetry (id) WHERE synced = 0;

CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    ts           REAL NOT NULL,
    score        REAL,
    data         TEXT NOT NULL,
    photo        TEXT,
    meta_synced  INTEGER NOT NULL DEFAULT 0,
    photo_synced INTEGER NOT NULL DEFAULT 0
);
"""


class EventManager:

    def __init__(self, data_dir="dados"):
        self.photo_dir = os.path.join(data_dir, "photos")
        os.makedirs(self.photo_dir, exist_ok=True)

        self._lock = threading.Lock()
        self._db = sqlite3.connect(
            os.path.join(data_dir, "vguard.db"),
            check_same_thread=False
        )
        self._db.row_factory = sqlite3.Row

        # WAL + FULL: uma queda de energia perde no máximo a transação
        # em andamento, sem corromper o banco (RNF06).
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)

        self._latest = {}       # fonte -> última amostra
        self._last_flush = 0.0
        self._last_event = {}   # tipo -> ts do último evento

    # --------------------------------------------------------
    # TELEMETRIA
    # --------------------------------------------------------

    def update(self, source, data, ts=None):
        """
        Recebe uma amostra de "gps", "imu" ou "obd" (dict).

        Pode ser chamado na frequência do sensor: só uma linha
        consolidada por TELEMETRY_INTERVAL vai para o SD.
        """
        ts = time.time() if ts is None else ts

        with self._lock:
            self._latest[source] = {"ts": ts, **data}

            if ts - self._last_flush < TELEMETRY_INTERVAL:
                return

            # ponytail: consolida pela última amostra do intervalo; trocar por
            # min/max/média da IMU quando RF34/RF35 (frenagem, impacto) entrarem.
            fresh = {
                s: d for s, d in self._latest.items()
                if d["ts"] > self._last_flush
            }
            with self._db:
                self._db.execute(
                    "INSERT INTO telemetry (ts, data) VALUES (?, ?)",
                    (ts, json.dumps(fresh))
                )
            self._last_flush = ts

    # --------------------------------------------------------
    # EVENTOS
    # --------------------------------------------------------

    def record_event(self, type, score, jpeg=None, ts=None):
        """
        Registra um evento ("fadiga", "distracao") com a telemetria
        mais recente de cada fonte e a fotografia (bytes JPEG).

        Retorna o id do evento, ou None se estiver dentro do
        EVENT_COOLDOWN do último evento do mesmo tipo.
        """
        ts = time.time() if ts is None else ts

        with self._lock:
            if ts - self._last_event.get(type, float("-inf")) < EVENT_COOLDOWN:
                return None

            event_id = str(uuid.uuid4())
            photo = None

            # A foto vai para o disco ANTES da linha no banco: o banco
            # nunca aponta para um arquivo que não existe.
            if jpeg is not None:
                photo = event_id + ".jpg"
                self._write_photo(photo, jpeg)

            with self._db:
                self._db.execute(
                    "INSERT INTO events (id, type, ts, score, data, photo)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, type, ts, score, json.dumps(self._latest), photo)
                )

            self._last_event[type] = ts
            return event_id

    def _write_photo(self, name, jpeg):
        path = os.path.join(self.photo_dir, name)
        tmp = path + ".tmp"

        with open(tmp, "wb") as f:
            f.write(jpeg)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp, path)

        fd = os.open(self.photo_dir, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)

    def window(self, event_id, seconds=WINDOW_SECONDS):
        """
        Telemetria de `seconds` antes até `seconds` depois do evento.
        """
        return self._query(
            "SELECT t.id, t.ts, t.data FROM telemetry t, events e"
            " WHERE e.id = ? AND t.ts BETWEEN e.ts - ? AND e.ts + ?"
            " ORDER BY t.ts",
            (event_id, seconds, seconds)
        )

    # --------------------------------------------------------
    # FILA DE SINCRONIZAÇÃO
    # --------------------------------------------------------

    # A fila É o banco: pendente = linha com synced = 0. Sobrevive a
    # reinicializações sem nenhum passo de recuperação (RNF25).
    # Chamar ack_* somente após a confirmação do backend (RF22).

    def pending_telemetry(self, limit=500):
        return self._query(
            "SELECT id, ts, data FROM telemetry WHERE synced = 0"
            " ORDER BY id LIMIT ?",
            (limit,)
        )

    def pending_events(self, limit=100):
        """
        Metadados pendentes. Podem ir por 4G ou Wi-Fi (RF20).
        """
        return self._query(
            "SELECT id, type, ts, score, data FROM events"
            " WHERE meta_synced = 0 ORDER BY ts LIMIT ?",
            (limit,)
        )

    def pending_photos(self, limit=100):
        """
        Lista de (id do evento, caminho do JPEG). Somente Wi-Fi (RF21).
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, photo FROM events"
                " WHERE photo IS NOT NULL AND photo_synced = 0"
                " ORDER BY ts LIMIT ?",
                (limit,)
            ).fetchall()

        return [
            (r["id"], os.path.join(self.photo_dir, r["photo"]))
            for r in rows
        ]

    def ack_telemetry(self, ids):
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE telemetry SET synced = 1 WHERE id = ?",
                [(i,) for i in ids]
            )

    def ack_event(self, event_id):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET meta_synced = 1 WHERE id = ?",
                (event_id,)
            )

    def ack_photo(self, event_id):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET photo_synced = 1 WHERE id = ?",
                (event_id,)
            )

    def pending_counts(self):
        """
        Quantidade de registros pendentes, para a saúde do dispositivo (RF32).
        """
        with self._lock:
            row = self._db.execute(
                "SELECT"
                " (SELECT COUNT(*) FROM telemetry WHERE synced = 0),"
                " (SELECT COUNT(*) FROM events WHERE meta_synced = 0),"
                " (SELECT COUNT(*) FROM events"
                "   WHERE photo IS NOT NULL AND photo_synced = 0)"
            ).fetchone()

        return {"telemetry": row[0], "events": row[1], "photos": row[2]}

    # --------------------------------------------------------

    def _query(self, sql, params):
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()

        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in rows
        ]

    def close(self):
        self._db.close()
