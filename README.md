# V-GUARD — Gerenciador de Eventos

Gerenciador de eventos local-first do projeto V-GUARD: grava no cartão SD a telemetria consolidada, os eventos e as fotografias, e mantém a fila do que ainda não foi confirmado pelo backend.

Só usa a biblioteca padrão do Python (3.10+). Não há dependências a instalar.

## Uso

```python
from event_manager import EventManager

manager = EventManager("dados")

# Leitores de GPS e do ESP32 (UART), na frequência do sensor
manager.update("gps", {"lat": -25.43, "lon": -49.27, "speed": 62.0})
manager.update("imu", {"ax": 0.1, "ay": 0.0, "az": 9.8})
manager.update("obd", {"rpm": 2100, "speed": 60})

# Visão, quando a condição de fadiga/distração atende aos limiares
ok, buffer = cv2.imencode(".jpg", frame)
event_id = manager.record_event("fadiga", score, buffer.tobytes())

# Serviço de sincronização
for event in manager.pending_events():          # 4G ou Wi-Fi
    ...                                         # POST; se o backend confirmar:
    manager.ack_event(event["id"])

for event_id, path in manager.pending_photos(): # somente Wi-Fi
    ...
    manager.ack_photo(event_id)
```

`update` e `record_event` aceitam `ts` (epoch, segundos). A Raspberry Pi não tem relógio de tempo real: sem rede o relógio do sistema pode estar errado, então prefira passar o horário do GPS.

## O que fica no SD

```
dados/
├── vguard.db          # SQLite: tabelas telemetry, events e event_window
└── photos/<id>.jpg    # fotografia do evento, nomeada pelo id
```

Cada evento tem dois estados independentes, `meta_synced` e `photo_synced`. Um evento enviado pelo 4G fica com os metadados confirmados e a foto pendente até o próximo Wi-Fi.

## Janela temporal do evento

As amostras de `update` passam por um buffer circular em memória com os últimos `WINDOW_SECONDS`, na frequência original dos sensores. Ao registrar um evento:

1. a parte anterior da janela é gravada na mesma transação do evento;
2. a parte posterior é gravada quando chega a primeira amostra depois de `WINDOW_SECONDS`;
3. só então o evento aparece em `pending_events()`.

`manager.window(event_id)` devolve as amostras gravadas (`ts`, `source`, `data`).

Se a energia cair no meio, o evento fica só com a parte anterior. Se os sensores pararem, a janela fecha sozinha após `2 × WINDOW_SECONDS`.

## Configuração

Constantes no topo de `event_manager.py`, com valores iniciais a calibrar:

| Constante | Padrão | Função |
|---|---|---|
| `TELEMETRY_INTERVAL` | 1 s | intervalo de consolidação da telemetria gravada |
| `EVENT_COOLDOWN` | 5 s | tempo mínimo entre dois eventos do mesmo tipo |
| `WINDOW_SECONDS` | 10 s | telemetria preservada antes e depois de cada evento |
| `BUFFER_MAX_SAMPLES` | 10000 | teto do buffer circular (folga para ~100 amostras/s) |

## Teste

```bash
cd edge/vguard/events
python3 test_event_manager.py
```

## Ainda não faz

- Envio ao backend e seleção de enlace (serviço de sincronização).
- Limpeza de dados já sincronizados (RNF23): hoje nada é apagado do SD.
