"""Base de datos SQLite del simulador (un archivo: datos/trader.db)."""
import os, sqlite3, threading, json

RUTA = os.environ.get('DMN_TRADER_DB') or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'datos', 'trader.db')
LOCK = threading.RLock()

ESQUEMA = """
CREATE TABLE IF NOT EXISTS cuentas (
  id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, tipo TEXT NOT NULL DEFAULT 'simulada', moneda TEXT NOT NULL DEFAULT 'USD',
  capital_inicial REAL NOT NULL, efectivo REAL NOT NULL, creada INTEGER NOT NULL, archivada INTEGER NOT NULL DEFAULT 0,
  config TEXT NOT NULL DEFAULT '{}', inicio_dia TEXT, equity_inicio_dia REAL, bloqueada_hasta TEXT,
  replay_simbolo TEXT, replay_intervalo TEXT, replay_idx INTEGER, replay_velas TEXT);
CREATE TABLE IF NOT EXISTS ordenes (
  id INTEGER PRIMARY KEY, cuenta_id INTEGER NOT NULL, simbolo TEXT NOT NULL, lado TEXT NOT NULL, tipo TEXT NOT NULL,
  cantidad REAL NOT NULL, limite REAL, stop REAL, trail_pct REAL, trail_ref REAL, activada INTEGER NOT NULL DEFAULT 0,
  estado TEXT NOT NULL, creada INTEGER NOT NULL, ejecutada INTEGER, precio_ejec REAL, comision REAL,
  padre_id INTEGER, oco TEXT, reduce INTEGER NOT NULL DEFAULT 0, motivo TEXT, nota TEXT, setup TEXT, origen TEXT, sl REAL, tp REAL);
CREATE INDEX IF NOT EXISTS ix_ord ON ordenes(cuenta_id, estado);
CREATE TABLE IF NOT EXISTS posiciones (
  cuenta_id INTEGER NOT NULL, simbolo TEXT NOT NULL, cantidad REAL NOT NULL, precio_medio REAL NOT NULL, moneda TEXT,
  abierta INTEGER, trade_id INTEGER, PRIMARY KEY (cuenta_id, simbolo));
CREATE TABLE IF NOT EXISTS ejecuciones (
  id INTEGER PRIMARY KEY, cuenta_id INTEGER, orden_id INTEGER, simbolo TEXT, lado TEXT, cantidad REAL, precio REAL,
  fx REAL, comision REAL, pnl REAL, ts INTEGER);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, cuenta_id INTEGER, simbolo TEXT, direccion TEXT, estado TEXT, abierto INTEGER, cerrado INTEGER,
  cantidad REAL, entrada REAL, salida_valor REAL DEFAULT 0, salida_cant REAL DEFAULT 0, salida REAL, pnl REAL DEFAULT 0,
  comisiones REAL DEFAULT 0, stop_inicial REAL, objetivo REAL, riesgo REAL, r REAL, mfe REAL, mae REAL,
  setup TEXT, nota TEXT, emocion TEXT, etiquetas TEXT, leccion TEXT, nota_cierre TEXT, respeto_plan INTEGER);
CREATE TABLE IF NOT EXISTS equity (cuenta_id INTEGER, ts INTEGER, valor REAL, efectivo REAL);
CREATE INDEX IF NOT EXISTS ix_eq ON equity(cuenta_id, ts);
CREATE TABLE IF NOT EXISTS vigilancia (simbolo TEXT PRIMARY KEY, nota TEXT, anadido INTEGER);
CREATE TABLE IF NOT EXISTS alertas (
  id INTEGER PRIMARY KEY, simbolo TEXT, condicion TEXT, valor REAL, nota TEXT, activa INTEGER DEFAULT 1,
  creada INTEGER, disparada INTEGER, precio_disparo REAL, vista INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS ajustes (clave TEXT PRIMARY KEY, valor TEXT);
"""

_con = None


def con():
    global _con
    if _con is None:
        os.makedirs(os.path.dirname(RUTA), exist_ok=True)
        _con = sqlite3.connect(RUTA, check_same_thread=False, isolation_level=None)
        _con.row_factory = sqlite3.Row
        _con.execute('PRAGMA journal_mode=WAL')
        _con.executescript(ESQUEMA)
    return _con


def q(sql, args=()):
    with LOCK:
        return [dict(r) for r in con().execute(sql, args).fetchall()]


def uno(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def ex(sql, args=()):
    with LOCK:
        return con().execute(sql, args).lastrowid


def ajuste(clave, defecto=None):
    r = uno('SELECT valor FROM ajustes WHERE clave=?', (clave,))
    return json.loads(r['valor']) if r else defecto


def poner_ajuste(clave, valor):
    ex('INSERT INTO ajustes(clave,valor) VALUES(?,?) ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor', (clave, json.dumps(valor)))
