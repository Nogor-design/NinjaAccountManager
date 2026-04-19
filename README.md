# NinjaAccountManager

A real-time NinjaTrader 8 desktop account monitor built with **Python 3.11+**, **DearPyGUI**, and **WebSockets**.

---

## Architecture overview

```
NinjaTrader 8  ──── WebSocket (ws://127.0.0.1:8765/ws) ────▶  Python app
  (client)                                                       (server)
```

Python runs a **WebSocket server**. NinjaTrader 8, with the bundled NinjaScript
indicator loaded, connects as a **client** and streams live trading data as JSON.
The Python app displays everything in a DearPyGUI desktop window and can send
order commands back to NinjaTrader.

---

## Project layout

```
NinjaAccountManager/
├── main.py                         Entry point
├── requirements.txt
├── core/
│   ├── config.py                   AppConfig dataclass (host, port, …)
│   ├── data_models.py              Pure dataclasses (AccountData, Position, …)
│   ├── state.py                    Thread-safe AppState store
│   ├── event_bus.py                Pub/sub EventBus + Events constants
│   └── nt_client.py                WebSocket server + JSON protocol handler
├── gui/
│   ├── app.py                      NinjaApp: DearPyGUI lifecycle + render loop
│   ├── dashboard.py                Summary cards + connection status
│   ├── accounts.py                 Per-account detail table
│   ├── positions.py                Open positions table
│   ├── orders.py                   Orders table + order entry form
│   ├── charts.py                   Candlestick chart with SMA / EMA
│   └── logs.py                     Scrollable filterable log viewer
└── ninjascript/
    └── NinjaAccountManager.cs      NinjaTrader 8 indicator (install in NT8)
```

---

## Setup

### 1 – Prerequisites

| Tool | Version |
|------|---------|
| Python | 3.11 or newer |
| NinjaTrader 8 | Latest build |
| pip | current |

### 2 – Clone / download

```bash
git clone <repo-url> NinjaAccountManager
cd NinjaAccountManager
```

### 3 – Create virtual environment & install dependencies

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

`requirements.txt` contains:
```
dearpygui>=1.11.0
websockets>=12.0
```

### 4 – Install the NinjaScript indicator

1. Open **NinjaTrader 8 → Tools → Edit NinjaScript → Indicators → New**
2. Paste the contents of `ninjascript/NinjaAccountManager.cs`.
3. Press **Compile** (F5). Fix any namespace errors if your NT8 version differs.
4. Apply the indicator to any chart (it is non-visual).  
   Set the **Server URL** property to `ws://127.0.0.1:8765/ws`  
   (must match the Python server's `host:port` in `core/config.py`).

### 5 – Run the Python app

```bash
python main.py
```

The app starts a WebSocket server on `ws://127.0.0.1:8765`.  
NinjaTrader will connect automatically once the indicator is loaded on a chart.

---

## Configuration

Edit `core/config.py` to change defaults:

| Field | Default | Description |
|-------|---------|-------------|
| `host` | `127.0.0.1` | WebSocket server bind address |
| `port` | `8765` | WebSocket server port |
| `chart_max_candles` | `200` | Max candles kept in memory per instrument |
| `sma_period` | `20` | SMA period for chart overlay |
| `ema_period` | `9` | EMA period for chart overlay |
| `window_width` | `1440` | Initial window width |
| `window_height` | `900` | Initial window height |

---

## Message protocol

### NinjaTrader → Python (streaming data)

```json
// Account snapshot / update
{"type": "ACCOUNT", "data": {"name": "Sim101", "balance": 100000.0,
  "cash_value": 100000.0, "realized_pnl": 0.0, "unrealized_pnl": -50.0,
  "initial_margin": 500.0, "maintenance_margin": 400.0,
  "buying_power": 400000.0, "excess": 99500.0}}

// Position update (quantity=0 means closed)
{"type": "POSITION", "data": {"account": "Sim101", "instrument": "ES 03-25",
  "quantity": 2, "avg_price": 5000.25, "unrealized_pnl": 150.0,
  "market_value": 10000.50}}

// Order update
{"type": "ORDER", "data": {"order_id": "abc123", "account": "Sim101",
  "instrument": "ES 03-25", "action": "Buy", "order_type": "Limit",
  "quantity": 1, "filled_quantity": 0, "price": 4990.0, "stop_price": 0.0,
  "status": "Working", "timestamp": "2024-03-15T09:30:00"}}

// Market data tick
{"type": "MARKET_DATA", "data": {"instrument": "ES 03-25",
  "bid": 5000.0, "ask": 5000.25, "last": 5000.0, "volume": 12345}}

// Bar (OHLCV)
{"type": "BAR", "data": {"instrument": "ES 03-25",
  "timestamp": 1710496200.0, "open": 5000.0, "high": 5001.5,
  "low": 4999.0, "close": 5001.0, "volume": 850}}
```

### Python → NinjaTrader (commands)

```json
{"action": "SUBMIT_ORDER", "account": "Sim101", "instrument": "ES 03-25",
 "orderAction": "Buy", "orderType": "Limit", "quantity": 1,
 "price": 4990.0, "stopPrice": 0.0}

{"action": "CANCEL_ORDER", "orderId": "abc123"}

{"action": "SUBSCRIBE_MD", "instrument": "NQ 03-25"}

{"action": "SUBSCRIBE_BARS", "instrument": "NQ 03-25",
 "barType": "Minute", "period": 5}
```

---

## NinjaTrader connection checklist

- [ ] Python app is running (you see "Listening for NinjaTrader" in the Logs tab)
- [ ] Firewall allows `127.0.0.1:8765` TCP traffic
- [ ] NinjaScript compiled without errors
- [ ] Indicator applied to a live chart (connected data feed)
- [ ] **Server URL** property matches `ws://<host>:<port>/ws`
- [ ] Status dot in the app header turns green

---

## Architectural decisions

### WebSocket server (Python) / client (NinjaTrader)
ninja-socket demonstrates that NinjaTrader is the WebSocket *client*.  Running
Python as the server makes reconnection trivial: NT simply re-connects if the
Python process restarts, without any changes to the indicator.

### Manual DearPyGUI render loop
Using `while dpg.is_dearpygui_running(): dpg.render_dearpygui_frame()` instead
of `dpg.start_dearpygui()` gives us a hook to drive panel refreshes each frame,
keeping background-thread data flowing into the GUI without extra timers or
callbacks.

### AppState as a shared-memory store
Rather than queuing GUI-update callables from background threads (which
DearPyGUI doesn't natively support), we store all trading data in a thread-safe
`AppState` object and let the main render thread pull fresh data each frame.
Dirty flags limit unnecessary DearPyGUI calls to frames where data actually
changed.

### EventBus decouples transport from GUI
The WebSocket layer publishes typed events; the GUI and state layers subscribe.
Adding a new data source (REST poller, fix engine, …) only requires publishing
the same event types — no GUI changes needed.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Status dot stays red | Check firewall; verify NT indicator is applied to a live chart |
| `ModuleNotFoundError: dearpygui` | Activate the virtual environment and run `pip install dearpygui` |
| `ModuleNotFoundError: websockets` | Run `pip install websockets` |
| Chart is empty | Click **Subscribe** on the Charts tab while NT is connected |
| Orders not submitting | Ensure account name matches exactly (check Accounts tab) |
| NinjaScript compile error | Verify the `using` namespace matches your NT8 version |
