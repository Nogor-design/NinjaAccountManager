// ============================================================
// NinjaAccountManager.cs
// NinjaTrader 8 NinjaScript Indicator
//
// INSTALLATION:
//   1. Copy this file into your NinjaTrader 8 NinjaScript editor
//      (Tools → Edit NinjaScript → Indicators → New)
//   2. Compile it (no extra DLL references required).
//   3. Apply the indicator to any chart (it is non-visual).
//   4. Make sure the Python server is running before loading the indicator.
//
// PROTOCOL:
//   This indicator connects to a Python WebSocket server and streams
//   account, position, order, market-data, and bar JSON messages.
//   It also accepts commands from Python to submit/cancel orders.
//
// KEY DESIGN NOTES:
//   • Orders are queued with ConcurrentQueue and submitted inside OnBarUpdate,
//     which runs on NT's own processing thread.  This is the only reliable way
//     to call Account.CreateOrder / Account.Submit from a non-Strategy script.
//   • Calculate = OnEachTick so bars stream in real-time intrabar.
//   • Bar history is flushed to Python on each new connection.
//   • Instrument.FullName is used everywhere for consistent naming.
// ============================================================

#region Using declarations
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Net.WebSockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Core;
using NinjaTrader.Data;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Indicators;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class NinjaAccountManager : Indicator
    {
        // ── Typed bar record for the ring-buffer ──────────────────────────────
        private class BarRec
        {
            public string instrument;
            public double timestamp, open, high, low, close;
            public int    volume;
        }

        // ── Order / cancel request types (WebSocket thread → NT thread) ───────
        private class OrderReq
        {
            public string Account, Instrument, Action, Type;
            public int    Qty;
            public double Price, StopPrice;
        }

        // ── Configuration properties ──────────────────────────────────────────
        [NinjaScriptProperty]
        [Display(Name = "Server URL", Order = 1, GroupName = "Connection")]
        public string ServerUrl { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Reconnect Delay (s)", Order = 2, GroupName = "Connection")]
        public int ReconnectDelaySeconds { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Bar History Depth", Order = 3, GroupName = "Chart")]
        public int BarHistoryDepth { get; set; }

        // ── WebSocket ─────────────────────────────────────────────────────────
        private ClientWebSocket          _ws;
        private CancellationTokenSource  _cts;
        private readonly object          _sendLock  = new object();
        private volatile bool            _isConnected;

        // ── JSON serializer (built-in .NET 4.x – no extra DLL needed) ─────────
        private readonly JavaScriptSerializer _json = new JavaScriptSerializer();

        // ── Bar ring-buffer (written on NT calculation thread) ────────────────
        private readonly object       _barLock            = new object();
        private readonly List<BarRec> _barCache           = new List<BarRec>(200);
        private volatile bool         _flushHistoryOnTick = false;

        // ── Thread-safe order/cancel queues (WebSocket → NT calculation thread) ─
        private readonly ConcurrentQueue<OrderReq> _pendingOrders  = new ConcurrentQueue<OrderReq>();
        private readonly ConcurrentQueue<string>   _pendingCancels = new ConcurrentQueue<string>();

        // ── Lifecycle ─────────────────────────────────────────────────────────
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description              = "NinjaAccountManager WebSocket connector for the Python desktop app.";
                Name                     = "NinjaAccountManager";
                IsOverlay                = false;
                IsSuspendedWhileInactive = false;
                Calculate                = Calculate.OnEachTick;
                ServerUrl                = "ws://127.0.0.1:8765/ws";
                ReconnectDelaySeconds    = 5;
                BarHistoryDepth          = 200;
            }
            else if (State == State.DataLoaded)
            {
                _cts = new CancellationTokenSource();
                Task.Run(() => ConnectLoopAsync(_cts.Token));

                // AccountStatusUpdate is a static event – subscribe on the type
                Account.AccountStatusUpdate += OnAccountStatusUpdate;
                // PositionUpdate and OrderUpdate are instance events – subscribe per account
                foreach (Account account in Account.All)
                {
                    account.PositionUpdate += OnPositionUpdate;
                    account.OrderUpdate    += OnOrderUpdate;
                }
            }
            else if (State == State.Terminated)
            {
                Account.AccountStatusUpdate -= OnAccountStatusUpdate;
                foreach (Account account in Account.All)
                {
                    account.PositionUpdate -= OnPositionUpdate;
                    account.OrderUpdate    -= OnOrderUpdate;
                }
                _cts?.Cancel();
                _ws?.Abort();
            }
        }

        // ── OnBarUpdate – NT's processing thread ──────────────────────────────
        // Account.CreateOrder / Account.Submit MUST be called here (on the NT
        // thread). ConcurrentQueue hands work from the WebSocket thread safely.
        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || CurrentBar < 1) return;

            // 1. Process any pending order submissions
            while (_pendingOrders.TryDequeue(out var req))
                SubmitOrderOnNTThread(req);

            // 2. Process any pending cancels
            while (_pendingCancels.TryDequeue(out var orderId))
                CancelOrderOnNTThread(orderId);

            // 3. Build / update bar cache
            var rec = new BarRec
            {
                instrument = Instrument.FullName,
                timestamp  = (double)new DateTimeOffset(Time[0]).ToUnixTimeSeconds(),
                open       = (double)Open[0],
                high       = (double)High[0],
                low        = (double)Low[0],
                close      = (double)Close[0],
                volume     = (int)Volume[0],
            };

            bool sendHistory = false;
            lock (_barLock)
            {
                int last = _barCache.Count - 1;
                if (last >= 0 && _barCache[last].timestamp == rec.timestamp)
                    _barCache[last] = rec;          // intrabar update
                else
                {
                    if (_barCache.Count >= BarHistoryDepth) _barCache.RemoveAt(0);
                    _barCache.Add(rec);
                }

                if (_flushHistoryOnTick && _isConnected)
                {
                    _flushHistoryOnTick = false;
                    sendHistory = true;
                }
            }

            if (!_isConnected) return;

            // 4. Stream bar data
            if (sendHistory)
            {
                List<BarRec> snap;
                lock (_barLock) snap = new List<BarRec>(_barCache);
                foreach (var b in snap) SendBar(b);
            }
            else
            {
                SendBar(rec);
            }
        }

        private void SubmitOrderOnNTThread(OrderReq req)
        {
            try
            {
                Account acc = Account.All
                    .FirstOrDefault(a => string.Equals(
                        a.Name, req.Account, StringComparison.OrdinalIgnoreCase))
                    ?? Account.All.FirstOrDefault();

                Instrument instr = Instrument.GetInstrument(req.Instrument);

                if (acc   == null) { Print($"[NAM] Account not found: {req.Account}");       return; }
                if (instr == null) { Print($"[NAM] Instrument not found: {req.Instrument}"); return; }

                OrderAction oa = req.Action == "Sell" ? OrderAction.Sell : OrderAction.Buy;
                OrderType   ot = Enum.TryParse<OrderType>(req.Type, out var parsedOt)
                                 ? parsedOt : OrderType.Market;

                // CreateOrder allocates the order object; Submit sends it to the exchange.
                Order order = acc.CreateOrder(
                    instr, oa, ot,
                    TimeInForce.Day,
                    req.Qty,
                    req.Price,
                    req.StopPrice,
                    string.Empty,           // OCO id
                    "NinjaAccountManager",  // signal name visible in Activity log
                    null);                  // ATM strategy (none)

                if (order != null)
                    acc.Submit(new[] { order });
                else
                    Print("[NAM] CreateOrder returned null — order not submitted.");
            }
            catch (Exception ex) { Print($"[NAM] SubmitOrder error: {ex.Message}"); }
        }

        private void CancelOrderOnNTThread(string orderId)
        {
            try
            {
                foreach (Account acc in Account.All)
                foreach (Order   o   in acc.Orders)
                {
                    if (o.OrderId == orderId && o.OrderState == OrderState.Working)
                    {
                        acc.Cancel(new[] { o });
                        return;
                    }
                }
                Print($"[NAM] Cancel: working order {orderId} not found.");
            }
            catch (Exception ex) { Print($"[NAM] CancelOrder error: {ex.Message}"); }
        }

        // ── Market data (bid / ask / last) ────────────────────────────────────
        protected override void OnMarketData(MarketDataEventArgs e)
        {
            if (!_isConnected) return;
            SendJson(new
            {
                type = "MARKET_DATA",
                data = new
                {
                    instrument = e.Instrument.FullName,
                    bid        = e.Bid,
                    ask        = e.Ask,
                    last       = e.Last,
                    volume     = (int)e.Volume,
                }
            });
        }

        // ── Account status (balance, margin, P&L, …) ─────────────────────────
        private void OnAccountStatusUpdate(object sender, AccountStatusEventArgs e)
        {
            if (!_isConnected) return;
            Account acc = e.Account;
            SendJson(new
            {
                type = "ACCOUNT",
                data = new
                {
                    name               = acc.Name,
                    balance            = acc.Get(AccountItem.CashValue,             Currency.UsDollar),
                    cash_value         = acc.Get(AccountItem.CashValue,             Currency.UsDollar),
                    realized_pnl       = acc.Get(AccountItem.RealizedProfitLoss,   Currency.UsDollar),
                    unrealized_pnl     = acc.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar),
                    initial_margin     = acc.Get(AccountItem.InitialMargin,         Currency.UsDollar),
                    maintenance_margin = acc.Get(AccountItem.MaintenanceMargin,     Currency.UsDollar),
                    buying_power       = acc.Get(AccountItem.BuyingPower,           Currency.UsDollar),
                    excess             = acc.Get(AccountItem.ExcessIntradayMargin,  Currency.UsDollar),
                }
            });
        }

        // ── Position update ───────────────────────────────────────────────────
        private void OnPositionUpdate(object sender, PositionEventArgs e)
        {
            if (!_isConnected) return;
            Position pos = e.Position;
            int qty = pos.MarketPosition == MarketPosition.Long  ?  pos.Quantity
                    : pos.MarketPosition == MarketPosition.Short ? -pos.Quantity
                    : 0;
            SendJson(new
            {
                type = "POSITION",
                data = new
                {
                    account        = pos.Account.Name,
                    instrument     = pos.Instrument.FullName,
                    quantity       = qty,
                    avg_price      = pos.AveragePrice,
                    unrealized_pnl = pos.GetUnrealizedProfitLoss(PerformanceUnit.Currency, 0),
                    market_value   = pos.AveragePrice * Math.Abs(pos.Quantity),
                }
            });
        }

        // ── Order update ──────────────────────────────────────────────────────
        private void OnOrderUpdate(object sender, OrderEventArgs e)
        {
            if (!_isConnected) return;
            Order o = e.Order;
            SendJson(new
            {
                type = "ORDER",
                data = new
                {
                    order_id        = o.OrderId,
                    account         = o.Account.Name,
                    instrument      = o.Instrument.FullName,
                    action          = o.OrderAction.ToString(),
                    order_type      = o.OrderType.ToString(),
                    quantity        = o.Quantity,
                    filled_quantity = o.Filled,
                    price           = o.LimitPrice,
                    stop_price      = o.StopPrice,
                    status          = o.OrderState.ToString(),
                    timestamp       = o.Time.ToString("o"),
                }
            });
        }

        // ── Incoming commands from Python ─────────────────────────────────────
        private void HandleCommand(string jsonStr)
        {
            try
            {
                var    cmd    = _json.Deserialize<Dictionary<string, object>>(jsonStr);
                string action = cmd.ContainsKey("action") ? cmd["action"] as string : null;

                if (action == "SUBMIT_ORDER")
                {
                    // Queue for OnBarUpdate (NT thread) — do NOT call Account
                    // methods from here (WebSocket / Task thread).
                    var req = new OrderReq
                    {
                        Account    = cmd.ContainsKey("account")     ? cmd["account"]     as string : null,
                        Instrument = cmd.ContainsKey("instrument")  ? cmd["instrument"]  as string : null,
                        Action     = cmd.ContainsKey("orderAction") ? cmd["orderAction"] as string : "Buy",
                        Type       = cmd.ContainsKey("orderType")   ? cmd["orderType"]   as string : "Market",
                        Qty        = cmd.ContainsKey("quantity")    ? Convert.ToInt32(cmd["quantity"])   : 1,
                        Price      = cmd.ContainsKey("price")       ? Convert.ToDouble(cmd["price"])     : 0.0,
                        StopPrice  = cmd.ContainsKey("stopPrice")   ? Convert.ToDouble(cmd["stopPrice"]) : 0.0,
                    };
                    _pendingOrders.Enqueue(req);
                    Print($"[NAM] Order queued: {req.Action} {req.Type} {req.Instrument} x{req.Qty}");
                }
                else if (action == "CANCEL_ORDER")
                {
                    string orderId = cmd.ContainsKey("orderId") ? cmd["orderId"] as string : null;
                    if (orderId != null) _pendingCancels.Enqueue(orderId);
                }
                else if (action == "SUBSCRIBE_MD")
                {
                    // NOTE: In NinjaScript indicators, dynamic runtime market data subscription
                    // for additional instruments is not supported. AddDataSeries() must be called
                    // in State.Configure before the indicator loads. The primary chart instrument
                    // is already streaming via OnMarketData(). For additional instruments,
                    // restart the indicator with those instruments pre-configured via AddDataSeries.
                    string instrName = cmd.ContainsKey("instrument") ? cmd["instrument"] as string : null;
                    Print($"[NAM] SUBSCRIBE_MD requested for {instrName}. " +
                          "Runtime subscription of additional instruments is not supported in NinjaScript. " +
                          "Add the instrument via AddDataSeries() in State.Configure instead.");
                }
            }
            catch (Exception ex) { Print($"[NAM] HandleCommand error: {ex.Message}"); }
        }

        // ── WebSocket connect / reconnect loop ────────────────────────────────
        private async Task ConnectLoopAsync(CancellationToken ct)
        {
            while (!ct.IsCancellationRequested)
            {
                _ws = new ClientWebSocket();
                try
                {
                    Print($"[NAM] Connecting to {ServerUrl}…");
                    await _ws.ConnectAsync(new Uri(ServerUrl), ct);
                    _isConnected        = true;
                    _flushHistoryOnTick = true;   // OnBarUpdate will flush bar history on next tick
                    Print("[NAM] Connected to Python server.");

                    SendInitialSnapshot();
                    await ListenAsync(ct);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception ex)
                {
                    Print($"[NAM] Connection error: {ex.Message}. Retrying in {ReconnectDelaySeconds}s…");
                }
                finally
                {
                    _isConnected        = false;
                    _flushHistoryOnTick = false;
                    _ws?.Dispose();
                }

                if (!ct.IsCancellationRequested)
                    await Task.Delay(TimeSpan.FromSeconds(ReconnectDelaySeconds), ct)
                              .ContinueWith(_ => { });
            }
        }

        private async Task ListenAsync(CancellationToken ct)
        {
            var buffer = new byte[16384];
            var sb     = new StringBuilder();
            while (_ws.State == WebSocketState.Open && !ct.IsCancellationRequested)
            {
                var result = await _ws.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                if (result.MessageType == WebSocketMessageType.Close) break;
                sb.Append(Encoding.UTF8.GetString(buffer, 0, result.Count));
                if (result.EndOfMessage)
                {
                    HandleCommand(sb.ToString());
                    sb.Clear();
                }
            }
        }

        // ── Initial snapshot (accounts / positions / orders) ──────────────────
        // Bar history is sent separately via _flushHistoryOnTick in OnBarUpdate,
        // because Bars data can only be safely accessed on the NT thread.
        private void SendInitialSnapshot()
        {
            foreach (Account acc in Account.All)
            {
                // Account
                SendJson(new
                {
                    type = "ACCOUNT",
                    data = new
                    {
                        name               = acc.Name,
                        balance            = acc.Get(AccountItem.CashValue,             Currency.UsDollar),
                        cash_value         = acc.Get(AccountItem.CashValue,             Currency.UsDollar),
                        realized_pnl       = acc.Get(AccountItem.RealizedProfitLoss,   Currency.UsDollar),
                        unrealized_pnl     = acc.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar),
                        initial_margin     = acc.Get(AccountItem.InitialMargin,         Currency.UsDollar),
                        maintenance_margin = acc.Get(AccountItem.MaintenanceMargin,     Currency.UsDollar),
                        buying_power       = acc.Get(AccountItem.BuyingPower,           Currency.UsDollar),
                        excess             = acc.Get(AccountItem.ExcessIntradayMargin,  Currency.UsDollar),
                    }
                });

                // Positions
                foreach (Position pos in acc.Positions)
                {
                    int qty = pos.MarketPosition == MarketPosition.Long  ?  pos.Quantity
                            : pos.MarketPosition == MarketPosition.Short ? -pos.Quantity
                            : 0;
                    SendJson(new
                    {
                        type = "POSITION",
                        data = new
                        {
                            account        = pos.Account.Name,
                            instrument     = pos.Instrument.FullName,
                            quantity       = qty,
                            avg_price      = pos.AveragePrice,
                            unrealized_pnl = pos.GetUnrealizedProfitLoss(PerformanceUnit.Currency, 0),
                            market_value   = pos.AveragePrice * Math.Abs(pos.Quantity),
                        }
                    });
                }

                // Orders
                foreach (Order order in acc.Orders)
                {
                    SendJson(new
                    {
                        type = "ORDER",
                        data = new
                        {
                            order_id        = order.OrderId,
                            account         = order.Account.Name,
                            instrument      = order.Instrument.FullName,
                            action          = order.OrderAction.ToString(),
                            order_type      = order.OrderType.ToString(),
                            quantity        = order.Quantity,
                            filled_quantity = order.Filled,
                            price           = order.LimitPrice,
                            stop_price      = order.StopPrice,
                            status          = order.OrderState.ToString(),
                            timestamp       = order.Time.ToString("o"),
                        }
                    });
                }
            }
        }

        // ── Helpers ───────────────────────────────────────────────────────────
        private void SendBar(BarRec b)
        {
            SendJson(new
            {
                type = "BAR",
                data = new
                {
                    instrument = b.instrument,
                    timestamp  = b.timestamp,
                    open       = b.open,
                    high       = b.high,
                    low        = b.low,
                    close      = b.close,
                    volume     = b.volume,
                }
            });
        }

        private void SendJson(object payload)
        {
            if (_ws == null || _ws.State != WebSocketState.Open) return;
            try
            {
                byte[] bytes = Encoding.UTF8.GetBytes(_json.Serialize(payload));
                lock (_sendLock)
                {
                    _ws.SendAsync(
                        new ArraySegment<byte>(bytes),
                        WebSocketMessageType.Text,
                        endOfMessage: true,
                        CancellationToken.None).GetAwaiter().GetResult();
                }
            }
            catch (Exception ex) { Print($"[NAM] SendJson error: {ex.Message}"); }
        }

        #region Required NinjaScript placeholder
        [Browsable(false)]
        [XmlIgnore]
        public Series<double> PlaceholderSeries { get; set; }
        #endregion
    }
}
