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
using System.Security.Cryptography;
using System.Globalization;
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
		
		private readonly object ordenLock = new object();
		private readonly Dictionary<string, string> ejecucionesVistas = new Dictionary<string, string>();
		private bool libroSano = true;
		private bool riesgoBloqueado;
		private string motivoRiesgo;
		private readonly Dictionary<string, string> proteccionesEntrada = new Dictionary<string, string>();
		private readonly HashSet<string> emergenciasSolicitadas = new HashSet<string>();
		private readonly string instanciaPuente = Guid.NewGuid().ToString("N");
		private readonly DateTime arranquePuente = DateTime.UtcNow;
		private readonly DateTime inicioCotizaciones = Core.Globals.Now;
		private readonly Dictionary<Connection, DateTime> conexionesDesde = new Dictionary<Connection, DateTime>();
		private readonly HashSet<Account> escuchando = new HashSet<Account>();
		// posición DMN abierta por cuenta|instrumento y libro de operaciones cerradas
		private class Abierta { public string Nombre; public double Entrada, Pnl, Comision, SalidaValor; public int Qty, Restan, SalidaQty; public bool Largo; public DateTime Desde; }
		private readonly Dictionary<string, Abierta> abiertas = new Dictionary<string, Abierta>();
		private readonly List<object> cerradas = new List<object>();

		private class LibroPersistido
		{
			public Dictionary<string, Abierta> Abiertas;
			public List<object> Cerradas;
			public Dictionary<string, string> Ejecuciones;
			public Dictionary<string, string> Protecciones;
		}

		private void BloquearLibro()
		{
			libroSano = false;
			try { File.WriteAllText(Path.Combine(Dir, "libro-bloqueado.txt"), DateTime.UtcNow.ToString("O")); } catch { }
		}

		private void BloquearRiesgo(string motivo)
		{
			if (riesgoBloqueado && motivoRiesgo == motivo) return;
			riesgoBloqueado = true;
			motivoRiesgo = motivo;
			try { File.WriteAllText(Path.Combine(Dir, "riesgo-bloqueado.txt"), DateTime.UtcNow.ToString("O") + "\n" + motivo); }
			catch { BloquearLibro(); }
		}

		private void CargarLibro()
		{
			string bloqueo = Path.Combine(Dir, "riesgo-bloqueado.txt");
			if (File.Exists(bloqueo)) { riesgoBloqueado = true; motivoRiesgo = File.ReadAllText(bloqueo); }
			if (File.Exists(Path.Combine(Dir, "libro-bloqueado.txt"))) libroSano = false;
			string ruta = Path.Combine(Dir, "libro.json");
			if (!File.Exists(ruta)) return;
			var estado = json.Deserialize<LibroPersistido>(File.ReadAllText(ruta));
			if (estado == null || estado.Abiertas == null || estado.Cerradas == null || estado.Ejecuciones == null) throw new Exception("libro incompleto; conciliar antes de iniciar");
			foreach (var par in estado.Abiertas) abiertas.Add(par.Key, par.Value);
			cerradas.AddRange(estado.Cerradas);
			foreach (var par in estado.Ejecuciones) ejecucionesVistas.Add(par.Key, par.Value);
			if (estado.Protecciones != null) foreach (var par in estado.Protecciones) proteccionesEntrada.Add(par.Key, par.Value);
		}

		private void GuardarLibro()
		{
			try
			{
				string ruta = Path.Combine(Dir, "libro.json"), temporal = ruta + ".tmp";
				var estado = new LibroPersistido { Abiertas = abiertas, Cerradas = cerradas, Ejecuciones = ejecucionesVistas, Protecciones = proteccionesEntrada };
				using (var flujo = new FileStream(temporal, FileMode.Create, FileAccess.Write, FileShare.None))
				using (var escritor = new StreamWriter(flujo)) { escritor.Write(json.Serialize(estado)); escritor.Flush(); flujo.Flush(true); }
				if (File.Exists(ruta)) File.Replace(temporal, ruta, null); else File.Move(temporal, ruta);
			}
			catch { BloquearLibro(); throw; }
		}

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
				CargarLibro();
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
				Connection.ConnectionStatusUpdate += AlCambiarConexion;
				hilo = new Thread(Bucle) { IsBackground = true, Name = "DmnPuente" };
				hilo.Start();
				Log("puente escuchando en " + Prefijo);
			}
			catch (Exception ex) { Log("no pude arrancar: " + ex.Message); }
		}

		private void Parar()
		{
			Connection.ConnectionStatusUpdate -= AlCambiarConexion;
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
					else if (ruta == "/orden/estado" && metodo == "GET") r = EstadoOrden(c.Request.QueryString["cuenta"], c.Request.QueryString["nombre"]);
					else if (ruta == "/orden" && metodo == "POST") r = Orden(b);
					else if (ruta == "/cerrar" && metodo == "POST") r = Cerrar(b);
					else if (ruta == "/cerradas") { lock (cerradas) r = new { cerradas = cerradas.ToList() }; }
					else { codigo = 404; r = new { error = "ruta" }; }
				}
			}
			catch (CierreSinDatos ex) { codigo = 409; r = new { error = ex.Message, codigo = "CIERRE_PENDIENTE_DATOS" }; }
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
					// Retomar eventos de las cuentas permitidas despues de reinicio/reconexion.
					try { CuentaPermitida(a.Name); } catch { }
					cuentas.Add(new
					{
						nombre = a.Name,
						conexion = a.Connection != null ? a.Connection.Options.Name : null,
						conectada = a.Connection != null && a.Connection.Status == ConnectionStatus.Connected,
						simulada = a.Provider == Provider.Simulator,
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
						ordenes = a.Orders.Where(o => o.OrderState == OrderState.Working || o.OrderState == OrderState.Accepted || o.OrderState == OrderState.Submitted || o.OrderState == OrderState.PartFilled || o.OrderState == OrderState.CancelPending).Select(o => new
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
			return new { ok = true, contrato = "dmn-puente/2", revision = "2026-10-24-rechazos-4", capacidades = new[] { "dmn-orden-estado/1" }, instancia = instanciaPuente, arranque_utc = arranquePuente, libro_sano = libroSano, riesgo_bloqueado = riesgoBloqueado, motivo_riesgo = motivoRiesgo, hora = DateTime.UtcNow, conexiones, cuentas, permitidas = Config().ContainsKey("cuentas_orden") ? Config()["cuentas_orden"] : null };
		}

		private object EstadoOrden(string cuenta, string nombre)
		{
			Account a = CuentaPermitida(cuenta);
			if (string.IsNullOrEmpty(nombre) || !nombre.StartsWith("DMN-E-") || nombre.Length > 64) throw new Exception("identificador de entrada invalido");
			if (!libroSano) return new { cuenta, nombre, estado = "desconocida" };
			lock (cerradas)
				foreach (object x in cerradas)
				{
					var c = json.Deserialize<Dictionary<string, object>>(json.Serialize(x));
					if (Txt(c, "cuenta") == cuenta && Txt(c, "nombre") == nombre)
						return new { cuenta, nombre, inst = Txt(c, "inst"), estado = "cerrada", cantidad = 0, cierre = c };
				}
			Order entrada;
			lock (a.Orders) entrada = a.Orders.LastOrDefault(o => o.Name == nombre);
			Abierta propia = null;
			string instrumento = null;
			lock (abiertas)
				foreach (var x in abiertas)
					if (x.Key.StartsWith(cuenta + "|") && x.Value.Nombre == nombre)
					{
						var v = x.Value;
						propia = new Abierta { Nombre = v.Nombre, Qty = v.Qty, Restan = v.Restan, Largo = v.Largo };
						instrumento = x.Key.Substring(cuenta.Length + 1);
						break;
					}
			if (propia != null && propia.Restan > 0)
			{
				int cantidadBroker = 0;
				bool ladoBroker = false;
				lock (a.Positions)
				{
					var p = a.Positions.FirstOrDefault(x => x.Instrument.FullName == instrumento && x.MarketPosition != MarketPosition.Flat);
					if (p != null) { cantidadBroker = p.Quantity; ladoBroker = p.MarketPosition == MarketPosition.Long; }
				}
				if (cantidadBroker != propia.Restan || ladoBroker != propia.Largo)
					return new { cuenta, nombre, inst = instrumento, estado = "desconocida" };
				int stops = 0, objetivos = 0;
				OrderAction salida = propia.Largo ? OrderAction.Sell : OrderAction.BuyToCover;
				lock (a.Orders)
					foreach (var o in a.Orders.Where(o => o.Instrument.FullName == instrumento && o.OrderAction == salida &&
						(o.OrderState == OrderState.Working || o.OrderState == OrderState.Accepted || o.OrderState == OrderState.PartFilled)))
					{
						if (o.Name.StartsWith("DMN-SL-") && o.OrderType == OrderType.StopMarket) stops += Math.Max(0, o.Quantity - o.Filled);
						if (o.Name.StartsWith("DMN-TP-") && o.OrderType == OrderType.Limit) objetivos += Math.Max(0, o.Quantity - o.Filled);
					}
				bool protegida = stops == propia.Restan && objetivos == propia.Restan;
				return new { cuenta, nombre, inst = instrumento, estado = protegida ? "abierta_protegida" : "abierta_sin_proteccion",
					cantidad = propia.Restan, lado = propia.Largo ? "buy" : "sell" };
			}
			if (entrada != null)
			{
				bool plana;
				lock (a.Positions) plana = !a.Positions.Any(x => x.Instrument.FullName == entrada.Instrument.FullName && x.MarketPosition != MarketPosition.Flat);
				bool terminalSinFill = (entrada.OrderState == OrderState.Cancelled || entrada.OrderState == OrderState.Rejected) && entrada.Filled == 0 && plana && (Core.Globals.Now - entrada.Time).TotalSeconds >= 2;
				return new { cuenta, nombre, inst = entrada.Instrument.FullName, cantidad = entrada.Filled,
					estado = terminalSinFill ? "sin_ejecucion" : "pendiente" };
			}
			return new { cuenta, nombre, estado = "desconocida" };
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
			lock (ordenLock) return OrdenUnica(b);
		}

		private static string Huella(string valor)
		{
			using (var sha = SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(valor))).Replace("-", "");
		}

		private object OrdenUnica(Dictionary<string, object> b)
		{
			if (!libroSano || riesgoBloqueado) throw new Exception("riesgo o libro pendiente de conciliacion; nuevas entradas bloqueadas");
			Account a = CuentaPermitida(Txt(b, "cuenta"));
			Instrument ins = Instrument.GetInstrument(Txt(b, "inst"));
			if (ins == null) throw new Exception("instrumento desconocido");
			int qty = EnteroOrden(b, "qty"), maxq = Ent(Config().ContainsKey("max_contratos") ? Config()["max_contratos"].ToString() : "1", 1);
			if (qty < 1 || qty > maxq) throw new Exception("cantidad fuera del límite (" + maxq + ")");
			int sl = EnteroOrden(b, "sl_ticks"), tp = EnteroOrden(b, "tp_ticks");
			if (sl < 1 || tp < 1) throw new Exception("toda entrada lleva stop y objetivo (sl_ticks y tp_ticks)");
			if (a.Positions.Any(p => p.Instrument.FullName == ins.FullName && p.MarketPosition != MarketPosition.Flat))
				throw new Exception("ya hay posición en " + ins.FullName);
			string lado = Txt(b, "lado"), id = Txt(b, "id");
			if (lado != "buy" && lado != "sell") throw new Exception("lado invalido: buy o sell");
			if (string.IsNullOrWhiteSpace(id) || id.Length > 200) throw new Exception("id estable obligatorio");
			bool compra = lado == "buy";
			string nombre = "DMN-E-" + Huella(a.Name + "|" + ins.FullName + "|" + id).Substring(0, 32);
			string intenciones = Path.Combine(Dir, "intenciones");
			Directory.CreateDirectory(intenciones);
			string archivo = Path.Combine(intenciones, nombre + ".txt");
			if (File.Exists(archivo)) throw new Exception("solicitud ya registrada; conciliar " + nombre + " antes de reintentar");
			lock (a.Orders)
				if (a.Orders.Any(x => x.Instrument.FullName == ins.FullName && x.OrderState != OrderState.Filled && x.OrderState != OrderState.Cancelled && x.OrderState != OrderState.Rejected))
					throw new Exception("hay ordenes pendientes en " + ins.FullName);
			using (var f = new FileStream(archivo, FileMode.CreateNew, FileAccess.Write, FileShare.None))
			using (var w = new StreamWriter(f)) { w.WriteLine(sl); w.WriteLine(tp); w.Flush(); f.Flush(true); }
			Order o = a.CreateOrder(ins, compra ? OrderAction.Buy : OrderAction.SellShort, OrderType.Market, OrderEntry.Automated, TimeInForce.Day,
				qty, 0, 0, string.Empty, nombre, Core.Globals.MaxDate, null);
			a.Submit(new[] { o });
			Log("orden " + nombre + " " + (compra ? "compra" : "venta") + " " + qty + " " + ins.FullName + " en " + a.Name + " (SL " + sl + " / TP " + tp + " ticks)");
			return new { ok = true, nombre, estado = o.OrderState.ToString() };
		}

		private static int EnteroOrden(Dictionary<string, object> b, string campo)
		{
			int valor;
			if (!int.TryParse(Txt(b, campo), out valor) || valor < 1) throw new Exception(campo + " debe ser entero positivo");
			return valor;
		}

		private void SolicitarCierreEmergencia(Account cuenta, Instrument instrumento, string entrada)
		{
			Abierta propia;
			lock (abiertas)
			{
				if (!abiertas.TryGetValue(cuenta.Name + "|" + instrumento.FullName, out propia) || propia.Nombre != entrada || propia.Restan <= 0)
				{ Log("cierre no enviado: falta confirmar posicion propia"); return; }
			}
			lock (cuenta.Positions)
			{
				var p = cuenta.Positions.FirstOrDefault(x => x.Instrument.FullName == instrumento.FullName && x.MarketPosition != MarketPosition.Flat);
				if (p == null || p.Quantity != propia.Restan || (p.MarketPosition == MarketPosition.Long) != propia.Largo)
				{ Log("cierre no enviado: posicion distinta del libro; conciliacion necesaria"); return; }
			}
			try { ValidarDatosCierre(cuenta, instrumento); }
			catch (Exception ex) { Log("CIERRE PENDIENTE: " + ex.Message); return; }
			lock (cuenta.Orders)
				if (cuenta.Orders.Any(o => o.Instrument.FullName == instrumento.FullName && o.Name == "Close" && o.OrderState != OrderState.Filled && o.OrderState != OrderState.Cancelled && o.OrderState != OrderState.Rejected)) return;
			lock (ordenLock)
			{
				string clave = cuenta.Name + "|" + instrumento.FullName + "|" + entrada;
				if (!emergenciasSolicitadas.Add(clave)) return;
			}
			try { cuenta.Flatten(new[] { instrumento }); }
			catch (Exception ex) { Log("CIERRE NO CONFIRMADO: " + ex.Message); }
		}

		private void AlActualizarOrden(object sender, OrderEventArgs e)
		{
			if (e == null || e.Order == null || string.IsNullOrEmpty(e.Order.Name) || e.OrderState != OrderState.Rejected) return;
			string nombre = e.Order.Name;
			if (nombre == "Close")
			{
				bool viva;
				lock (e.Order.Account.Positions) viva = e.Order.Account.Positions.Any(p => p.Instrument.FullName == e.Order.Instrument.FullName && p.MarketPosition != MarketPosition.Flat);
				if (viva) { BloquearRiesgo("cierre_rechazado: verificar posicion antes de reanudar"); Log("CIERRE RECHAZADO: no se considera plana la cuenta"); }
				return;
			}
			bool stop = nombre.StartsWith("DMN-SL-");
			if (!stop && !nombre.StartsWith("DMN-TP-")) return;
			string entrada;
			lock (ordenLock)
				if (!proteccionesEntrada.TryGetValue(e.Order.Account.Name + "|" + nombre, out entrada))
				{ BloquearRiesgo("proteccion_rechazada_sin_ambito: requiere conciliacion"); return; }
			bool vigente;
			lock (abiertas)
			{
				Abierta propia;
				vigente = abiertas.TryGetValue(e.Order.Account.Name + "|" + e.Order.Instrument.FullName, out propia) && propia.Nombre == entrada && propia.Restan > 0;
			}
			if (!vigente)
			{
				bool cerrada;
				lock (cerradas) cerrada = cerradas.Any(x => { var c = json.Deserialize<Dictionary<string,object>>(json.Serialize(x)); return Txt(c,"cuenta") == e.Order.Account.Name && Txt(c,"nombre") == entrada; });
				if (!cerrada) BloquearRiesgo("rechazo_sin_posicion_conciliada");
				return;
			}
			BloquearRiesgo(stop ? "stop_rechazado: cierre de emergencia pendiente de confirmar" : "objetivo_rechazado: verificar stop y conciliar");
			if (stop) SolicitarCierreEmergencia(e.Order.Account, e.Order.Instrument, entrada);
		}

		// Cada ejecucion parcial recibe proteccion para su propia cantidad.
		private void Proteger(Execution x)
		{
			lock (ordenLock)
			{
				string archivo = Path.Combine(Dir, "intenciones", x.Order.Name + ".txt");
				string marca = Path.Combine(Dir, "intenciones", "exec-" + Huella(x.Account.Name + "|" + x.ExecutionId) + ".txt");
				if (File.Exists(marca))
				{
					BloquearLibro();
					Log("proteccion con resultado desconocido: conciliar antes de nuevas entradas " + x.ExecutionId);
					return;
				}
				try
				{
					var st = File.ReadAllLines(archivo);
					int sl = int.Parse(st[0]), tp = int.Parse(st[1]);
					Account a = x.Account; Instrument ins = x.Instrument;
					double t = ins.MasterInstrument.TickSize, p = x.Price;
					bool largo = x.Order.OrderAction == OrderAction.Buy;
					string sufijo = Huella(a.Name + "|" + x.ExecutionId).Substring(0, 24);
					string oco = "DMN-OCO-" + sufijo;
					OrderAction salida = largo ? OrderAction.Sell : OrderAction.BuyToCover;
					double stop = ins.MasterInstrument.RoundToTickSize(largo ? p - sl * t : p + sl * t);
					double obj = ins.MasterInstrument.RoundToTickSize(largo ? p + tp * t : p - tp * t);
					// La intencion de proteccion tambien precede al envio; una ambiguedad exige conciliacion.
					File.WriteAllText(marca, "proteccion solicitada");
					Order s = a.CreateOrder(ins, salida, OrderType.StopMarket, OrderEntry.Automated, TimeInForce.Gtc, x.Quantity, 0, stop, oco, "DMN-SL-" + sufijo, Core.Globals.MaxDate, null);
					Order g = a.CreateOrder(ins, salida, OrderType.Limit, OrderEntry.Automated, TimeInForce.Gtc, x.Quantity, obj, 0, oco, "DMN-TP-" + sufijo, Core.Globals.MaxDate, null);
					proteccionesEntrada[a.Name + "|" + s.Name] = x.Order.Name;
					proteccionesEntrada[a.Name + "|" + g.Name] = x.Order.Name;
					GuardarLibro();
					a.Submit(new[] { s, g });
				}
				catch (Exception ex)
				{
					BloquearRiesgo("proteccion_no_confirmada: requiere conciliacion");
					Log("PROTECCION NO CONFIRMADA: " + ex.Message);
					SolicitarCierreEmergencia(x.Account, x.Instrument, x.Order.Name);
				}
			}
		}

		// libro: la entrada DMN abre; cualquier ejecución en contra (stop, objetivo, cierre manual o por tiempo) va cerrando
		private void AlEjecutar(object sender, ExecutionEventArgs e)
		{
			try
			{
				Execution x = e.Execution;
				if (x == null || x.Order == null || string.IsNullOrEmpty(x.Order.Name)) return;
				if (string.IsNullOrEmpty(x.ExecutionId) || x.Quantity <= 0 || double.IsNaN(x.Price) || double.IsInfinity(x.Price)) { BloquearLibro(); return; }
				string clave = x.Account.Name + "|" + x.Instrument.FullName;
				double pv = x.Instrument.MasterInstrument.PointValue;
				lock (abiertas)
				{
					string id = x.Account.Name + "|" + x.ExecutionId;
					string huella = Huella(x.Instrument.FullName + "|" + x.Order.Name + "|" + x.Quantity + "|" + x.Price.ToString("R", CultureInfo.InvariantCulture) + "|" + x.Commission.ToString("R", CultureInfo.InvariantCulture));
					string anterior;
					if (ejecucionesVistas.TryGetValue(id, out anterior))
					{
						if (anterior != huella) { BloquearLibro(); Log("ejecucion corregida por el broker; conciliar " + x.ExecutionId); }
						return;
					}
					ejecucionesVistas.Add(id, huella);
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
						GuardarLibro();
						Proteger(x);
						return;
					}
					if (a == null || compra == a.Largo) { GuardarLibro(); return; }
					int q = Math.Min(x.Quantity, a.Restan);
					a.Pnl += (x.Price - a.Entrada) * q * pv * (a.Largo ? 1 : -1);
						a.Comision += x.Commission * q / x.Quantity;
						a.SalidaValor += x.Price * q;
						a.SalidaQty += q;
						a.Restan -= q;
					if (a.Restan > 0) { GuardarLibro(); return; }
					abiertas.Remove(clave);
					lock (cerradas)
					{
						cerradas.Add(new { nombre = a.Nombre, cuenta = x.Account.Name, inst = x.Instrument.FullName, largo = a.Largo, qty = a.Qty,
							entrada = a.Entrada, salida = a.SalidaValor / a.SalidaQty, por = x.Order.Name, pnl = Math.Round(a.Pnl - a.Comision, 2), comision = a.Comision,
							desde = a.Desde, hasta = x.Time });
						GuardarLibro();
					}
					Log("cerrada " + a.Nombre + ": " + Math.Round(a.Pnl - a.Comision, 2) + " $");
				}
			}
			catch (Exception ex) { BloquearLibro(); Log("libro no conciliado: " + ex.Message); }
		}

		private class CierreSinDatos : Exception
		{
			public CierreSinDatos() : base("CIERRE_PENDIENTE_DATOS: sin cotizaciones recientes y validas; se conservan las protecciones") { }
		}

		private void AlCambiarConexion(object sender, ConnectionStatusEventArgs e)
		{
			if (e.Connection == null) return;
			if (e.PriceStatus != e.PreviousPriceStatus || e.Status != e.PreviousStatus)
				lock (conexionesDesde) conexionesDesde[e.Connection] = Core.Globals.Now;
		}

		private static bool DatosCierreValidos(bool conectado, double bid, double ask, DateTime ultimo, DateTime desde, DateTime ahora)
		{
			return conectado && !double.IsNaN(bid) && !double.IsInfinity(bid) && !double.IsNaN(ask) && !double.IsInfinity(ask)
				&& bid > 0 && ask > 0 && bid <= ask && ultimo >= desde
				&& (ahora - ultimo).TotalSeconds >= -5 && (ahora - ultimo).TotalSeconds <= 30;
		}

		private void ValidarDatosCierre(Account a, Instrument ins)
		{
			if (a.Connection == null) throw new CierreSinDatos();
			DateTime desde;
			lock (conexionesDesde)
				if (!conexionesDesde.TryGetValue(a.Connection, out desde)) desde = inicioCotizaciones;
			double bid = 0, ask = 0;
			DateTime ultimo = DateTime.MinValue;
			lock (ins.SyncMarketData)
			{
				var md = ins.MarketData;
				if (md != null)
				{
					if (md.Bid != null) { bid = md.Bid.Price; if (md.Bid.Time > ultimo) ultimo = md.Bid.Time; }
					if (md.Ask != null) { ask = md.Ask.Price; if (md.Ask.Time > ultimo) ultimo = md.Ask.Time; }
					if (md.Last != null && md.Last.Time > ultimo) ultimo = md.Last.Time;
				}
			}
			if (!DatosCierreValidos(a.Connection != null && a.Connection.Status == ConnectionStatus.Connected && a.Connection.PriceStatus == ConnectionStatus.Connected,
				bid, ask, ultimo, desde, Core.Globals.Now)) throw new CierreSinDatos();
		}

		private object Cerrar(Dictionary<string, object> b)
		{
			lock (ordenLock)
			{
				Account a = CuentaPermitida(Txt(b, "cuenta"));
				Instrument ins = Instrument.GetInstrument(Txt(b, "inst"));
				if (ins == null) throw new Exception("instrumento desconocido");
				lock (a.Orders)
				{
					var pendiente = a.Orders.FirstOrDefault(o => o.Instrument.FullName == ins.FullName && o.Name == "Close"
						&& o.OrderState != OrderState.Filled && o.OrderState != OrderState.Cancelled && o.OrderState != OrderState.Rejected);
					if (pendiente != null) return new { ok = true, estado = "pendiente", nombre = pendiente.Name };
				}
				bool abierta;
				lock (a.Positions) abierta = a.Positions.Any(p => p.Instrument.FullName == ins.FullName && p.MarketPosition != MarketPosition.Flat);
				// Flatten cancela protecciones: no llamarlo si el precio no puede verificarse.
				if (abierta) ValidarDatosCierre(a, ins);
				a.Flatten(new[] { ins });
				Log("cierre solicitado de " + ins.FullName + " en " + a.Name + "; confirmar ejecucion");
				return new { ok = true, estado = "solicitado" };
			}
		}
	}
}
