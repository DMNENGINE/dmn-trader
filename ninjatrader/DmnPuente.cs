// DMN Trader ↔ NinjaTrader 8: puente HTTP local (solo localhost) para el enjambre y el piloto de DMN Trader.
// Instalar: copiar a Documents\NinjaTrader 8\bin\Custom\AddOns\ y compilar (New → NinjaScript Editor → F5).
// Datos en Documents\NinjaTrader 8\dmn\: token.txt (lo crea solo; cabecera x-dmn-token) y puente.json:
//   { "cuentas_orden": ["Sim101"], "max_contratos": 1 }  ← SOLO las cuentas listadas pueden recibir órdenes.
// API (http://localhost:36990):
//   GET  /estado                          conexiones, cuentas (saldo, P/L), posiciones y órdenes vivas
//   GET  /barras?inst=MES 12-26&min=5&dias=5   velas (t, o, h, l, c, v)
//   POST /orden  {cuenta, inst, lado: buy|sell, qty, sl_ticks, tp_ticks, id}  entrada a mercado + stop y objetivo OCO al llenarse
//   POST /cerrar {cuenta, inst}           cierra la posición y anula sus órdenes
//   GET  /cerradas                        operaciones DMN cerradas (entrada, salida, $ netos con comisión) de esta sesión
#region Using declarations
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
	public class DmnPuente : AddOnBase
	{
		private const string Prefijo = "http://localhost:36990/";
		private HttpListener http;
		private Thread hilo;
		private string token;
		private readonly JavaScriptSerializer json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 };
		// entradas a la espera de llenarse: nombre de la orden → (sl_ticks, tp_ticks)
		private readonly Dictionary<string, int[]> brackets = new Dictionary<string, int[]>();
		private readonly HashSet<Account> escuchando = new HashSet<Account>();
		// posición DMN abierta por cuenta|instrumento y libro de operaciones cerradas
		private class Abierta { public string Nombre; public double Entrada, Pnl, Comision; public int Qty, Restan; public bool Largo; public DateTime Desde; }
		private readonly Dictionary<string, Abierta> abiertas = new Dictionary<string, Abierta>();
		private readonly List<object> cerradas = new List<object>();

		private static string Dir { get { return Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "dmn"); } }

		protected override void OnStateChange()
		{
			if (State == State.SetDefaults)
			{
				Name = "DmnPuente";
				Description = "Puente HTTP local para DMN Trader (enjambre y piloto).";
			}
			else if (State == State.Active)
				Arrancar();
			else if (State == State.Terminated)
				Parar();
		}

		private void Log(string s) { NinjaTrader.Code.Output.Process("[DMN] " + s, PrintTo.OutputTab1); }

		private void Arrancar()
		{
			try
			{
				Directory.CreateDirectory(Dir);
				string ft = Path.Combine(Dir, "token.txt");
				if (!File.Exists(ft))
					File.WriteAllText(ft, Guid.NewGuid().ToString("N") + Guid.NewGuid().ToString("N"));
				token = File.ReadAllText(ft).Trim();
				string fc = Path.Combine(Dir, "puente.json");
				if (!File.Exists(fc))
					File.WriteAllText(fc, "{ \"cuentas_orden\": [\"Sim101\"], \"max_contratos\": 1 }");
				http = new HttpListener();
				http.Prefixes.Add(Prefijo);
				http.Start();
				hilo = new Thread(Bucle) { IsBackground = true, Name = "DmnPuente" };
				hilo.Start();
				Log("puente escuchando en " + Prefijo);
			}
			catch (Exception ex) { Log("no pude arrancar: " + ex.Message); }
		}

		private void Parar()
		{
			try { if (http != null) http.Close(); } catch { }
			lock (escuchando)
				foreach (Account a in escuchando)
				{
					a.OrderUpdate -= AlActualizarOrden;
					a.ExecutionUpdate -= AlEjecutar;
				}
		}

		private Dictionary<string, object> Config()
		{
			try { return json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(Dir, "puente.json"))); }
			catch { return new Dictionary<string, object>(); }
		}

		private void Bucle()
		{
			while (http != null && http.IsListening)
			{
				HttpListenerContext c;
				try { c = http.GetContext(); } catch { return; }
				ThreadPool.QueueUserWorkItem(_ => Atender(c));
			}
		}

		private void Atender(HttpListenerContext c)
		{
			int codigo = 200;
			object r;
			try
			{
				if (c.Request.Headers["x-dmn-token"] != token)
				{
					codigo = 401;
					r = new { error = "token" };
				}
				else
				{
					string ruta = c.Request.Url.AbsolutePath.TrimEnd('/');
					string metodo = c.Request.HttpMethod;
					Dictionary<string, object> b = null;
					if (metodo == "POST")
						using (var sr = new StreamReader(c.Request.InputStream, Encoding.UTF8))
							b = json.Deserialize<Dictionary<string, object>>(sr.ReadToEnd());
					if (ruta == "/estado") r = Estado();
					else if (ruta == "/barras") r = Barras(c.Request.QueryString["inst"], Ent(c.Request.QueryString["min"], 5), Ent(c.Request.QueryString["dias"], 5));
					else if (ruta == "/orden" && metodo == "POST") r = Orden(b);
					else if (ruta == "/cerrar" && metodo == "POST") r = Cerrar(b);
					else if (ruta == "/cerradas") { lock (cerradas) r = new { cerradas = cerradas.ToList() }; }
					else { codigo = 404; r = new { error = "ruta" }; }
				}
			}
			catch (Exception ex) { codigo = 400; r = new { error = ex.Message }; }
			try
			{
				byte[] bytes = Encoding.UTF8.GetBytes(json.Serialize(r));
				c.Response.StatusCode = codigo;
				c.Response.ContentType = "application/json; charset=utf-8";
				c.Response.OutputStream.Write(bytes, 0, bytes.Length);
				c.Response.Close();
			}
			catch { }
		}

		private static int Ent(string s, int d) { int x; return int.TryParse(s, out x) ? x : d; }
		private static string Txt(Dictionary<string, object> b, string k) { object v; return b != null && b.TryGetValue(k, out v) && v != null ? v.ToString() : null; }

		private object Estado()
		{
			var cuentas = new List<object>();
			lock (Account.All)
				foreach (Account a in Account.All)
				{
					cuentas.Add(new
					{
						nombre = a.Name,
						conexion = a.Connection != null ? a.Connection.Options.Name : null,
						conectada = a.Connection != null && a.Connection.Status == ConnectionStatus.Connected,
						simulada = a.Provider == Provider.Simulator || a.Name.StartsWith("Sim") || a.Name == "Playback101" || a.Name == "Backtest",
						saldo = a.Get(AccountItem.CashValue, Currency.UsDollar),
						poder_compra = a.Get(AccountItem.BuyingPower, Currency.UsDollar),
						realizado = a.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar),
						no_realizado = a.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar),
						posiciones = a.Positions.Where(p => p.MarketPosition != MarketPosition.Flat).Select(p => new
						{
							inst = p.Instrument.FullName,
							lado = p.MarketPosition.ToString(),
							qty = p.Quantity,
							precio = p.AveragePrice
						}).ToList(),
						ordenes = a.Orders.Where(o => o.OrderState == OrderState.Working || o.OrderState == OrderState.Accepted || o.OrderState == OrderState.Submitted).Select(o => new
						{
							nombre = o.Name,
							inst = o.Instrument.FullName,
							accion = o.OrderAction.ToString(),
							tipo = o.OrderType.ToString(),
							qty = o.Quantity,
							limite = o.LimitPrice,
							stop = o.StopPrice,
							estado = o.OrderState.ToString()
						}).ToList()
					});
				}
			var conexiones = new List<object>();
			lock (Connection.Connections)
				foreach (Connection x in Connection.Connections)
					conexiones.Add(new { nombre = x.Options.Name, estado = x.Status.ToString(), precios = x.PriceStatus.ToString() });
			return new { ok = true, hora = DateTime.UtcNow, conexiones, cuentas, permitidas = Config().ContainsKey("cuentas_orden") ? Config()["cuentas_orden"] : null };
		}

		private object Barras(string inst, int minutos, int dias)
		{
			Instrument ins = Instrument.GetInstrument(inst);
			if (ins == null) throw new Exception("instrumento desconocido: " + inst);
			var listo = new ManualResetEvent(false);
			List<object> velas = null;
			string error = null;
			var req = new BarsRequest(ins, DateTime.Now.AddDays(-Math.Max(1, Math.Min(dias, 60))), DateTime.Now)
			{
				BarsPeriod = new BarsPeriod { BarsPeriodType = BarsPeriodType.Minute, Value = Math.Max(1, minutos) },
				TradingHours = ins.MasterInstrument.TradingHours
			};
			req.Request((bars, codigoError, mensaje) =>
			{
				try
				{
					if (codigoError != ErrorCode.NoError) error = codigoError + ": " + mensaje;
					else
					{
						velas = new List<object>();
						var b = bars.Bars;
						for (int i = 0; i < b.Count; i++)
							velas.Add(new object[] { (long)(b.GetTime(i).ToUniversalTime() - new DateTime(1970, 1, 1)).TotalSeconds,
								b.GetOpen(i), b.GetHigh(i), b.GetLow(i), b.GetClose(i), b.GetVolume(i) });
					}
				}
				finally { listo.Set(); }
			});
			bool ok = listo.WaitOne(60000);
			req.Dispose();
			if (!ok) throw new Exception("tiempo agotado pidiendo velas");
			if (error != null) throw new Exception(error);
			return new { inst = ins.FullName, tick = ins.MasterInstrument.TickSize, valor_punto = ins.MasterInstrument.PointValue, velas };
		}

		private Account CuentaPermitida(string nombre)
		{
			var cfg = Config();
			object l;
			var permitidas = cfg.TryGetValue("cuentas_orden", out l) && l is System.Collections.ArrayList
				? ((System.Collections.ArrayList)l).Cast<object>().Select(x => x.ToString()).ToList() : new List<string>();
			if (!permitidas.Contains(nombre)) throw new Exception("la cuenta " + nombre + " no está en cuentas_orden de puente.json");
			Account a;
			lock (Account.All) a = Account.All.FirstOrDefault(x => x.Name == nombre);
			if (a == null) throw new Exception("cuenta no encontrada: " + nombre);
			if (a.Connection == null || a.Connection.Status != ConnectionStatus.Connected) throw new Exception("cuenta sin conexión: " + nombre);
			lock (escuchando)
				if (escuchando.Add(a))
				{
					a.OrderUpdate += AlActualizarOrden;
					a.ExecutionUpdate += AlEjecutar;
				}
			return a;
		}

		private object Orden(Dictionary<string, object> b)
		{
			Account a = CuentaPermitida(Txt(b, "cuenta"));
			Instrument ins = Instrument.GetInstrument(Txt(b, "inst"));
			if (ins == null) throw new Exception("instrumento desconocido");
			int qty = Ent(Txt(b, "qty"), 1), maxq = Ent(Config().ContainsKey("max_contratos") ? Config()["max_contratos"].ToString() : "1", 1);
			if (qty < 1 || qty > maxq) throw new Exception("cantidad fuera del límite (" + maxq + ")");
			int sl = Ent(Txt(b, "sl_ticks"), 0), tp = Ent(Txt(b, "tp_ticks"), 0);
			if (sl < 1 || tp < 1) throw new Exception("toda entrada lleva stop y objetivo (sl_ticks y tp_ticks)");
			if (a.Positions.Any(p => p.Instrument.FullName == ins.FullName && p.MarketPosition != MarketPosition.Flat))
				throw new Exception("ya hay posición en " + ins.FullName);
			bool compra = Txt(b, "lado") == "buy";
			string nombre = "DMN-E-" + (Txt(b, "id") ?? DateTime.UtcNow.Ticks.ToString());
			lock (brackets) brackets[nombre] = new[] { sl, tp };
			Order o = a.CreateOrder(ins, compra ? OrderAction.Buy : OrderAction.SellShort, OrderType.Market, OrderEntry.Automated, TimeInForce.Day,
				qty, 0, 0, string.Empty, nombre, Core.Globals.MaxDate, null);
			a.Submit(new[] { o });
			Log("orden " + nombre + " " + (compra ? "compra" : "venta") + " " + qty + " " + ins.FullName + " en " + a.Name + " (SL " + sl + " / TP " + tp + " ticks)");
			return new { ok = true, nombre, estado = o.OrderState.ToString() };
		}

		// al llenarse una entrada DMN pone su stop y su objetivo (OCO): nunca hay posición sin stop
		private void AlActualizarOrden(object sender, OrderEventArgs e)
		{
			if (e.OrderState != OrderState.Filled || e.Order == null || !e.Order.Name.StartsWith("DMN-E-")) return;
			int[] st;
			lock (brackets)
			{
				if (!brackets.TryGetValue(e.Order.Name, out st)) return;
				brackets.Remove(e.Order.Name);
			}
			try
			{
				Account a = e.Order.Account;
				Instrument ins = e.Order.Instrument;
				double t = ins.MasterInstrument.TickSize, p = e.AverageFillPrice;
				bool largo = e.Order.OrderAction == OrderAction.Buy;
				string oco = "DMN-OCO-" + e.Order.Name.Substring(6);
				OrderAction salida = largo ? OrderAction.Sell : OrderAction.BuyToCover;
				double stop = ins.MasterInstrument.RoundToTickSize(largo ? p - st[0] * t : p + st[0] * t);
				double obj = ins.MasterInstrument.RoundToTickSize(largo ? p + st[1] * t : p - st[1] * t);
				Order s = a.CreateOrder(ins, salida, OrderType.StopMarket, OrderEntry.Automated, TimeInForce.Gtc, e.Filled, 0, stop, oco, "DMN-SL-" + e.Order.Name.Substring(6), Core.Globals.MaxDate, null);
				Order g = a.CreateOrder(ins, salida, OrderType.Limit, OrderEntry.Automated, TimeInForce.Gtc, e.Filled, obj, 0, oco, "DMN-TP-" + e.Order.Name.Substring(6), Core.Globals.MaxDate, null);
				a.Submit(new[] { s, g });
				Log("bracket de " + e.Order.Name + ": stop " + stop + " / objetivo " + obj);
			}
			catch (Exception ex)
			{
				Log("¡no pude poner el stop de " + e.Order.Name + "! cierro la posición: " + ex.Message);
				try { e.Order.Account.Flatten(new[] { e.Order.Instrument }); } catch { }
			}
		}

		// libro: la entrada DMN abre; cualquier ejecución en contra (stop, objetivo, cierre manual o por tiempo) va cerrando
		private void AlEjecutar(object sender, ExecutionEventArgs e)
		{
			try
			{
				Execution x = e.Execution;
				if (x == null || x.Order == null) return;
				string clave = x.Account.Name + "|" + x.Instrument.FullName;
				double pv = x.Instrument.MasterInstrument.PointValue;
				lock (abiertas)
				{
					Abierta a;
					abiertas.TryGetValue(clave, out a);
					bool compra = x.Order.OrderAction == OrderAction.Buy || x.Order.OrderAction == OrderAction.BuyToCover;
					if (x.Order.Name.StartsWith("DMN-E-"))
					{
						if (a == null || a.Nombre != x.Order.Name)
							abiertas[clave] = a = new Abierta { Nombre = x.Order.Name, Largo = compra, Desde = x.Time };
						a.Entrada = (a.Entrada * a.Qty + x.Price * x.Quantity) / (a.Qty + x.Quantity);
						a.Qty += x.Quantity;
						a.Restan += x.Quantity;
						a.Comision += x.Commission;
						return;
					}
					if (a == null || compra == a.Largo) return;
					int q = Math.Min(x.Quantity, a.Restan);
					a.Pnl += (x.Price - a.Entrada) * q * pv * (a.Largo ? 1 : -1);
					a.Comision += x.Commission;
					a.Restan -= q;
					if (a.Restan > 0) return;
					abiertas.Remove(clave);
					lock (cerradas)
					{
						cerradas.Add(new { nombre = a.Nombre, cuenta = x.Account.Name, inst = x.Instrument.FullName, largo = a.Largo, qty = a.Qty,
							entrada = a.Entrada, salida = x.Price, por = x.Order.Name, pnl = Math.Round(a.Pnl - a.Comision, 2), comision = a.Comision,
							desde = a.Desde, hasta = x.Time });
						if (cerradas.Count > 500) cerradas.RemoveAt(0);
					}
					Log("cerrada " + a.Nombre + ": " + Math.Round(a.Pnl - a.Comision, 2) + " $");
				}
			}
			catch (Exception ex) { Log("libro: " + ex.Message); }
		}

		private object Cerrar(Dictionary<string, object> b)
		{
			Account a = CuentaPermitida(Txt(b, "cuenta"));
			Instrument ins = Instrument.GetInstrument(Txt(b, "inst"));
			if (ins == null) throw new Exception("instrumento desconocido");
			a.Flatten(new[] { ins });
			Log("cerrada " + ins.FullName + " en " + a.Name);
			return new { ok = true };
		}
	}
}
