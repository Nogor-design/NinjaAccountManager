# NinjaTrader 8 NinjaScript – Coding Rules

## DO NOT change these – they are intentional fixes for NT8 compiler errors

### JSON serialization
- Use `System.Web.Script.Serialization.JavaScriptSerializer` — NOT Newtonsoft.Json
- Newtonsoft is not auto-referenced in NinjaScript and requires manual DLL setup
- Deserialize to `Dictionary<string, object>` and use `.ContainsKey()` — NOT dynamic

### Event subscriptions
- `Account.AccountStatusUpdate` is STATIC — subscribe on the TYPE: `Account.AccountStatusUpdate += ...`
- `account.PositionUpdate` is an INSTANCE event — subscribe per account in a foreach loop
- `account.OrderUpdate` is an INSTANCE event — subscribe per account in a foreach loop

### Required using directives
- `[Display]` attribute requires `using System.ComponentModel.DataAnnotations;`
- `[Browsable]` requires `using System.ComponentModel;`
- `[XmlIgnore]` requires `using System.Xml.Serialization;`
- Dispatcher requires `using NinjaTrader.Core;` (for `Globals.RandomDispatcher`)

### Order submission
- `NTWindow.ActiveWindowDispatcher` does NOT exist — use `Globals.RandomDispatcher`
- Orders MUST be submitted on the NT thread via OnBarUpdate + ConcurrentQueue
- Always call `acc.Submit(new[] { order })` after `acc.CreateOrder(...)` — CreateOrder alone does nothing
- Do NOT call Account methods from a Task/WebSocket/background thread

### Market data
- `MarketData.Subscribe()` does NOT exist in NinjaScript indicators
- Additional instruments require `AddDataSeries()` called in `State.Configure`

### EventArgs constructors
- `AccountStatusEventArgs`, `PositionEventArgs`, `OrderEventArgs` cannot be manually constructed
- For snapshots, build the JSON payload directly from the live Account/Position/Order objects

### Instrument naming
- Use `Instrument.FullName` — not `MasterInstrument.Name`