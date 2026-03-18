# FlightScanner

Monitor automático de preços de passagens aéreas via Google Flights.
Roda em background, salva histórico, compara preços e envia alertas pelo Telegram.

---

## O que faz

- Consulta o Google Flights automaticamente em intervalos configuráveis
- Suporta voos **só de ida** e **ida e volta**
- Salva todos os preços em banco SQLite com histórico completo
- Exibe tabela no console com preço atual e variação em relação à rodada anterior
- Envia resumo pelo **Telegram** a cada rodada
- Dispara alerta especial quando o preço cai acima do threshold configurado

---

## Estrutura do projeto

```
flightscanner/
├── config.yaml          # Toda a configuração (buscas, agenda, scraper, notificações)
├── main.py              # CLI: run | search | history | install
├── requirements.txt
└── src/
    ├── config.py        # Dataclasses + loader YAML
    ├── scraper.py       # Playwright + Google Flights (extração de preços)
    ├── storage.py       # SQLite — histórico de preços
    ├── notifier.py      # Console (Rich) + Telegram
    └── scheduler.py     # APScheduler — agendamento das rodadas
```

---

## Instalação

```bash
# 1. Instalar dependências Python
pip install -r requirements.txt

# 2. Instalar o browser Chromium (necessário para o Playwright)
python main.py install
```

---

## Comandos

| Comando | O que faz |
|---|---|
| `python main.py run` | Inicia o monitor contínuo com agendamento |
| `python main.py search` | Executa uma rodada única e sai |
| `python main.py search --id vdc-gru-mai26` | Executa só uma busca específica |
| `python main.py history` | Exibe histórico de preços do banco |
| `python main.py history --id vdc-gru-mai26` | Histórico de uma busca específica |
| `python main.py install` | Instala o Chromium via Playwright |

Todas as opções aceitam `--verbose` para logs detalhados (DEBUG).

---

## Configuração (`config.yaml`)

### Buscas

```yaml
searches:
  - id: "vdc-gru-mai26"                          # identificador único
    description: "Conquista ↔ Guarulhos — 09→10/Mai/2026"
    origin: "VDC"                                 # código IATA origem
    destination: "GRU"                            # código IATA destino
    outbound_date: "2026-05-09"                   # data de ida (YYYY-MM-DD)
    return_date: "2026-05-10"                     # data de volta (omitir = só de ida)
    adults: 1
    cabin: "economy"
```

- Se `return_date` estiver presente → busca **ida e volta**
- Se `return_date` for `null` ou omitido → busca **só de ida**

### Agendamento

```yaml
schedule:
  interval_minutes: 60        # intervalo entre rodadas (padrão: 60 min)
  start_immediately: true     # roda imediatamente ao iniciar
  run_at_times: []            # horários fixos do dia, ex: ["08:00", "20:00"]
                              # se preenchido, interval_minutes é ignorado
```

### Notificações

```yaml
notifications:
  console: true                      # exibe tabela no terminal
  price_drop_threshold_pct: 5.0      # % mínima de queda para alerta especial (0 = sempre alerta)
  webhook_url: ""                    # Discord ou Slack webhook (opcional)
  telegram:
    enabled: true
    bot_token: "SEU_TOKEN"           # obtido via @BotFather
    chat_id: "SEU_CHAT_ID"          # obtido via @userinfobot
```

### Scraper

```yaml
scraper:
  headless: true              # false = abre janela do Chrome (debug)
  timeout_ms: 35000           # tempo máximo de espera por resultados
  screenshot_on_error: true   # salva screenshot em data/screenshots/ em caso de erro
  retry_attempts: 2           # tentativas por busca antes de desistir
  delay_between_searches_sec: 8   # pausa entre buscas consecutivas
  proxy: ""                   # ex: "http://host:port" ou "socks5://host:port"
```

---

## Configurar Telegram

**1. Criar o bot**
1. Abra o Telegram e procure `@BotFather`
2. Envie `/newbot` e siga as instruções
3. Copie o **token** gerado (ex: `1234567890:AAF...xyz`)

**2. Obter seu chat_id**
1. Procure `@userinfobot` no Telegram
2. Envie qualquer mensagem
3. Copie o **Id** retornado (ex: `987654321`)

**3. Atualizar o `config.yaml`**
```yaml
telegram:
  enabled: true
  bot_token: "1234567890:AAF...xyz"
  chat_id: "987654321"
```

**Formato da mensagem enviada a cada rodada:**
```
✈️ Conquista ↔ Guarulhos — 09→10/Mai/2026
🕐 17/03/2026 às 22:15
🟢 Caiu 2.1% (era R$ 631,00)

🥇 R$ 618,00  |  Gol  |  06:00 - 08:05 2h5min  |  Direto
🔹 R$ 649,00  |  Latam Airlines  |  07:30 - 09:40  |  Direto
🔹 R$ 720,00  |  Azul  |  11:00 - 13:10  |  1 escala(s)

📊 Minimo historico: R$ 596,00
```

---

## Como funciona internamente

### Scraper
1. Constrói URL do Google Flights com parâmetro `tfs` (protobuf codificado em base64url) contendo origem, destino, datas e tipo de viagem
2. Abre o Chrome headless via Playwright e navega para a URL
3. Para buscas de ida e volta, detecta a página de exploração e clica em "Pesquisar" para obter resultados
4. Extrai preços, horários e companhias via JavaScript (`innerText` dos cards de resultado)
5. Detecta a companhia pelo texto do card: LATAM, Gol, Azul, Avianca, Passaredo

### Armazenamento
- Banco SQLite em `./data/flights.db`
- Cada rodada salva todos os voos encontrados com timestamp
- Histórico purgado automaticamente após `keep_history_days` dias
- Screenshots salvas em `./data/screenshots/` quando ocorre erro

### Variação de preço
- A coluna **Variação** no console compara o mais barato desta rodada com o mínimo da rodada anterior
- O **Mínimo histórico** é o menor preço já registrado desde o início do monitoramento
- Alerta especial é disparado quando a queda supera `price_drop_threshold_pct`

---

## Dados gerados

```
data/
├── flights.db           # banco SQLite com todo o histórico
├── flightscanner.log    # logs detalhados (rotação em 10 MB)
└── screenshots/         # capturas de tela em caso de erro
```

---

## Dependências

| Pacote | Uso |
|---|---|
| `playwright` | Automação do Chrome / extração do Google Flights |
| `apscheduler` | Agendamento das rodadas |
| `pyyaml` | Leitura do config.yaml |
| `rich` | Tabelas coloridas no terminal |
| `loguru` | Logs estruturados com rotação |
| `typer` | Interface de linha de comando |
| `httpx` | Envio de notificações Telegram / webhook |
