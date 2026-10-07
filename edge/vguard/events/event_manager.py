"""
V-GUARD — Gerenciador de eventos local-first.

Tudo é gravado no cartão SD antes de qualquer tentativa de envio (RF14):

    dados/
    ├── vguard.db        SQLite: telemetria consolidada, eventos e estados de sincronização
    └── photos/<id>.jpg  fotografia de cada evento (RNF13)

Quem escreve: visão (record_event) e leitores de GPS / ESP32 (update).
Quem lê: o serviço de sincronização (pending_* / ack_*).
"""

import collections
import json
import math
import os
import shutil
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

# Janela de telemetria antes/depois do evento (RF17), na frequência
# original dos sensores.
WINDOW_SECONDS = 10.0

# Teto do buffer circular, caso o relógio salte para trás e a poda
# por tempo pare de funcionar. Folga para ~100 amostras/s.
BUFFER_MAX_SAMPLES = 10000

# Ocupação do cartão SD (fração de 0 a 1) que dispara o alerta e o
# estado crítico (RNF22). A limpeza só atua a partir do alerta.
STORAGE_WARN = 0.80
STORAGE_CRITICAL = 0.95

# Estados de sincronização gravados nas colunas *synced. "Enviando" não
# é gravado: se a energia cair no meio de um envio, o item simplesmente
# continua PENDING.
PENDING = 0
CONFIRMED = 1   # o backend confirmou o armazenamento
FAILED = 2      # o backend recusou em definitivo; fica no SD, sai da fila


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
    photo_synced INTEGER NOT NULL DEFAULT 0,
    window_closed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_window (
    event_id TEXT NOT NULL REFERENCES events (id),
    ts       REAL NOT NULL,
    source   TEXT NOT NULL,
    data     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS event_window_event ON event_window (event_id, ts);
"""


class EventManager:

    def __init__(self, data_dir="dados"):
        self.data_dir = data_dir
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

        # Janelas interrompidas por queda de energia não serão mais
        # completadas: ficam só com a parte anterior ao evento.
        with self._db:
            self._db.execute(
                "UPDATE events SET window_closed = 1 WHERE window_closed = 0"
            )

        self._remove_orphans()

        # Buffer circular: (ts, fonte, amostra) dos últimos WINDOW_SECONDS.
        self._buffer = collections.deque(maxlen=BUFFER_MAX_SAMPLES)
        self._open = {}         # id -> (ts do evento, prazo monotônico)

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

            self._buffer.append((ts, source, data))
            self._close_windows(ts)
            while self._buffer[0][0] < ts - WINDOW_SECONDS:
                self._buffer.popleft()

            if ts - self._last_flush < TELEMETRY_INTERVAL:
                return

            # consolida pela última amostra do intervalo; trocar por
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

            # A parte anterior da janela entra na mesma transação do
            # evento: sobrevive mesmo se a energia cair logo em seguida.
            with self._db:
                self._db.execute(
                    "INSERT INTO events (id, type, ts, score, data, photo)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (event_id, type, ts, score, json.dumps(self._latest), photo)
                )
                self._insert_window(event_id, ts - WINDOW_SECONDS, ts)

            # Se os sensores pararem, a janela fecha pelo prazo.
            self._open[event_id] = (ts, time.monotonic() + 2 * WINDOW_SECONDS)
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

    # --------------------------------------------------------
    # JANELA TEMPORAL
    # --------------------------------------------------------

    def _insert_window(self, event_id, start, end):
        """
        Copia do buffer para o SD as amostras com start <= ts <= end.
        """
        self._db.executemany(
            "INSERT INTO event_window VALUES (?, ?, ?, ?)",
            [
                (event_id, ts, source, json.dumps(data))
                for ts, source, data in self._buffer
                if start <= ts <= end
            ]
        )

    def _close_windows(self, ts):
        """
        Grava a parte posterior das janelas que já terminaram em `ts`.
        """
        now = time.monotonic()

        for event_id, (event_ts, deadline) in list(self._open.items()):
            end = event_ts + WINDOW_SECONDS

            if ts <= end and now < deadline:
                continue

            with self._db:
                # nextafter: a amostra do instante do evento já foi gravada.
                self._insert_window(
                    event_id, math.nextafter(event_ts, math.inf), end
                )
                self._db.execute(
                    "UPDATE events SET window_closed = 1 WHERE id = ?",
                    (event_id,)
                )
            del self._open[event_id]

    def window(self, event_id):
        """
        Amostras de WINDOW_SECONDS antes até WINDOW_SECONDS depois do evento.
        """
        return self._query(
            "SELECT ts, source, data FROM event_window"
            " WHERE event_id = ? ORDER BY ts",
            (event_id,)
        )

    # --------------------------------------------------------
    # FILA DE SINCRONIZAÇÃO
    # --------------------------------------------------------

    # A fila É o banco: pendente = linha com synced = 0. Sobrevive a
    # reinicializações sem nenhum passo de recuperação (RNF25).
    # Chamar ack_* somente após a resposta do backend (RF22).

    def pending_telemetry(self, limit=500):
        return self._query(
            "SELECT id, ts, data FROM telemetry WHERE synced = 0"
            " ORDER BY id LIMIT ?",
            (limit,)
        )

    def pending_events(self, limit=100):
        """
        Metadados pendentes. Podem ir por 4G ou Wi-Fi (RF20).

        Um evento só entra na fila depois que sua janela fecha.
        """
        with self._lock:
            self._close_windows(float("-inf"))

        return self._query(
            "SELECT id, type, ts, score, data FROM events"
            " WHERE meta_synced = 0 AND window_closed = 1 ORDER BY ts LIMIT ?",
            (limit,)
        )

    def pending_photos(self, limit=100):
        """
        Lista de (id do evento, caminho do JPEG). Somente Wi-Fi (RF21).

        Só entram fotos cujos metadados o backend já confirmou.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT id, photo FROM events"
                " WHERE photo IS NOT NULL AND photo_synced = 0"
                " AND meta_synced = 1 ORDER BY ts LIMIT ?",
                (limit,)
            ).fetchall()

        return [
            (r["id"], os.path.join(self.photo_dir, r["photo"]))
            for r in rows
        ]

    def ack_telemetry(self, ids, state=CONFIRMED):
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE telemetry SET synced = ? WHERE id = ?",
                [(state, i) for i in ids]
            )

    def ack_event(self, event_id, state=CONFIRMED):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET meta_synced = ? WHERE id = ?",
                (state, event_id)
            )

    def ack_photo(self, event_id, state=CONFIRMED):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE events SET photo_synced = ? WHERE id = ?",
                (state, event_id)
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
                " (SELECT COUNT(*) FROM events WHERE photo IS NOT NULL"
                "   AND photo_synced = 0 AND meta_synced != 2)"
            ).fetchone()

        return {"telemetry": row[0], "events": row[1], "photos": row[2]}

    # --------------------------------------------------------
    # ARMAZENAMENTO
    # --------------------------------------------------------

    def storage_status(self):
        """
        Ocupação do cartão SD, para a saúde do dispositivo (RF32).
        """
        usage = shutil.disk_usage(self.data_dir)
        used = usage.used / usage.total

        if used >= STORAGE_CRITICAL:
            level = "critical"
        elif used >= STORAGE_WARN:
            level = "warning"
        else:
            level = "ok"

        return {"used": used, "free_bytes": usage.free, "level": level}

    def cleanup(self):
        """
        Abaixo de STORAGE_WARN não faz nada. A partir dele, apaga o que o
        backend já confirmou; o que está pendente nunca é apagado (RNF23).

        Chamar periodicamente (ex.: a cada minuto).
        """
        if self.storage_status()["level"] == "ok":
            return

        # apaga tudo que já foi sincronizado de uma vez; trocar por
        # lotes do mais antigo ao mais novo se o histórico local for útil.
        done = "meta_synced = 1 AND (photo IS NULL OR photo_synced = 1)"

        with self._lock:
            with self._db:
                self._db.execute("DELETE FROM telemetry WHERE synced = 1")
                self._db.execute(
                    "DELETE FROM event_window WHERE event_id IN"
                    " (SELECT id FROM events WHERE " + done + ")"
                )
                self._db.execute("DELETE FROM events WHERE " + done)

            # As fotos desses eventos ficaram sem linha no banco.
            self._remove_orphans()

    def _remove_orphans(self):
        """
        Apaga arquivos em photos/ sem evento no banco: gravações
        interrompidas (.tmp), fotos cujo evento não chegou a ser gravado
        e fotos de eventos removidos pela limpeza.
        """
        known = {
            row[0] for row in
            self._db.execute("SELECT photo FROM events WHERE photo IS NOT NULL")
        }

        for name in os.listdir(self.photo_dir):
            if name not in known:
                os.remove(os.path.join(self.photo_dir, name))

    # --------------------------------------------------------

    def _query(self, sql, params):
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()

        return [
            {**dict(r), "data": json.loads(r["data"])}
            for r in rows
        ]

    def close(self):
        with self._lock:
            self._close_windows(float("inf"))
        self._db.close()
