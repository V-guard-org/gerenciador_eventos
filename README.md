# V-GUARD — Gerenciador de Eventos

Gerenciador de eventos local-first do projeto V-GUARD: grava no cartão SD a telemetria consolidada, os eventos e as fotografias, mantém a fila do que ainda não foi confirmado pelo backend e a envia por Wi-Fi ou 4G.

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

# Sincronização em segundo plano, no mesmo processo
from sync import Sync
Sync(manager).start()
```

`update` e `record_event` aceitam `ts` (epoch, segundos). A Raspberry Pi não tem relógio de tempo real: sem rede o relógio do sistema pode estar errado, então prefira passar o horário do GPS.

## O que fica no SD

```
dados/
├── vguard.db          # SQLite: tabelas telemetry, events e event_window
└── photos/<id>.jpg    # fotografia do evento, nomeada pelo id
```

Cada evento tem dois estados independentes, `meta_synced` e `photo_synced`. Um evento enviado pelo 4G fica com os metadados confirmados e a foto pendente até o próximo Wi-Fi.

## Sincronização

`sync.py` roda um ciclo a cada `SYNC_INTERVAL`:

| Enlace | O que envia |
|---|---|
| Wi-Fi autorizado | saúde, telemetria, eventos (com janela) e fotografias |
| 4G | saúde, telemetria e eventos (com janela), sem fotografias |
| nenhum | nada; tudo continua pendente |

Estados de cada item:

| Estado | Significado |
|---|---|
| `PENDING` (0) | ainda não confirmado; é reenviado até dar certo |
| `CONFIRMED` (1) | o backend respondeu 2xx |
| `FAILED` (2) | o backend recusou o dado (400, 413 ou 422); fica no SD, sai da fila |

"Enviando" não é gravado: se a energia ou a rede caírem no meio, o item continua `PENDING` e é reenviado inteiro. Erro de rede, 5xx, 401 ou 404 interrompem o ciclo e o intervalo dobra a cada tentativa, até `RETRY_MAX`.

O enlace é o que o kernel usa de fato para chegar ao backend (`ip route get`). Para o Wi-Fi ter precedência sobre o 4G, a métrica de rota do Wi-Fi precisa ser menor:

```bash
nmcli connection modify "<conexão 4G>" ipv4.route-metric 700
nmcli connection modify "<conexão Wi-Fi>" ipv4.route-metric 100
```

Configuração no topo de `sync.py`: `AUTHORIZED_WIFI` (nomes das conexões Wi-Fi autorizadas), `CELLULAR_DEVICES`, `SYNC_INTERVAL` e `RETRY_MAX`. Endereço e credencial vêm do ambiente: `VGUARD_BACKEND`, `VGUARD_DEVICE` e `VGUARD_TOKEN`.

O contrato com o backend é **provisório** e está descrito no topo de `sync.py`.

## Janela temporal do evento

As amostras de `update` passam por um buffer circular em memória com os últimos `WINDOW_SECONDS`, na frequência original dos sensores. Ao registrar um evento:

1. a parte anterior da janela é gravada na mesma transação do evento;
2. a parte posterior é gravada quando chega a primeira amostra depois de `WINDOW_SECONDS`;
3. só então o evento aparece em `pending_events()`.

`manager.window(event_id)` devolve as amostras gravadas (`ts`, `source`, `data`).

Se a energia cair no meio, o evento fica só com a parte anterior. Se os sensores pararem, a janela fecha sozinha após `2 × WINDOW_SECONDS`.

## Armazenamento e recuperação

- **Corte de energia:** o SQLite roda em WAL com `synchronous=FULL` e cada foto é gravada em arquivo temporário, sincronizada e só então renomeada. Um corte perde no máximo a gravação em andamento.
- **Recuperação:** ao abrir, o gerenciador apaga de `photos/` o que não tem evento no banco (fotos pela metade, fotos de evento que não chegou a ser gravado) e fecha as janelas interrompidas. A fila volta como estava.
- **Ocupação:** `storage_status()` devolve a fração usada do cartão e o nível `ok`, `warning` ou `critical`.
- **Limpeza:** `cleanup()` não faz nada abaixo do alerta. A partir dele, apaga a telemetria e os eventos (com foto e janela) que o backend já confirmou por inteiro. O que está pendente nunca é apagado.

`cleanup()` não roda sozinho: chame periodicamente, por exemplo a cada minuto.

## Configuração

Constantes no topo de `event_manager.py`, com valores iniciais a calibrar:

| Constante | Padrão | Função |
|---|---|---|
| `TELEMETRY_INTERVAL` | 1 s | intervalo de consolidação da telemetria gravada |
| `EVENT_COOLDOWN` | 5 s | tempo mínimo entre dois eventos do mesmo tipo |
| `WINDOW_SECONDS` | 10 s | telemetria preservada antes e depois de cada evento |
| `BUFFER_MAX_SAMPLES` | 10000 | teto do buffer circular (folga para ~100 amostras/s) |
| `STORAGE_WARN` | 0.80 | ocupação do SD que dispara o alerta e libera a limpeza |
| `STORAGE_CRITICAL` | 0.95 | ocupação do SD considerada crítica |

## Teste

```bash
cd edge/vguard/events
python3 test_event_manager.py
python3 test_sync.py              # contra um backend falso local
```
Por enquanto, o test_sync dará o resultado "WARNING:root:backend recusou /events/...: 422"
O teste test_event_manager também mata um processo gravador com `SIGKILL` dez vezes seguidas e confere banco, fotos e fila a cada reabertura. Isso não é um corte de energia. Para o ensaio real na Raspberry:

```bash
python3 test_event_manager.py writer /caminho/dados   # deixe rodando e puxe a fonte
python3 test_event_manager.py check /caminho/dados    # depois do boot
```

## Ainda não faz

- Envio retomável de fotos: uma foto interrompida é reenviada inteira.
- Rotação de logs (RNF23): o gerenciador não gera logs.
- Com o SD cheio só de dados pendentes, `record_event` falha com erro do SQLite; não há tratamento além do nível `critical`.
