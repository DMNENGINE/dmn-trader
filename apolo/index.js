// Paquete de APOLO para DMN Trader: APOLO arranca, vigila y para el motor (Python) en este equipo, da al agente las
// herramientas de la mesa (dinero simulado) y aporta la pestaña Trading del panel. El piloto con dinero real NO se
// toca desde aquí: lo activa/pausa el núcleo de APOLO (/v1/trading/real) con permiso explícito y auditoría.
const { definirPlugin } = require('@apolo/sdk');
const { spawn, spawnSync } = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const os = require('os');
const path = require('path');

const RAIZ = path.join(__dirname, '..');
const LECTURA = new Set(['estado', 'cuentas', 'precio', 'analisis', 'buscar', 'ordenes', 'tamano', 'diario', 'stats', 'examen', 'equity', 'estrategias',
  'backtest', 'optimizar', 'escaner', 'alertas', 'avisos', 'reglas', 'enjambre', 'agente', 'updown', 'polymarket']);
const ESCRITURA = new Set(['usar', 'nueva', 'reiniciar', 'comprar', 'vender', 'compra', 'venta', 'cerrar', 'stop', 'cancelar', 'anotar', 'vigilar', 'olvidar',
  'alerta', 'practica', 'avanzar', 'reglas', 'updown', 'polymarket']);
const SUB_LECTURA = { enjambre: ['', 'informe', 'noticias', 'lecciones', 'agente'], agente: ['', 'informe', 'noticias', 'lecciones', 'agente'], updown: ['', 'mercados'], polymarket: ['', 'mercados'] };
const SUB_ESCRITURA = { updown: ['iniciar', 'parar'], polymarket: ['iniciar', 'parar'] };

let apolo, proc = null, parando = false, reintentos = 0, temporizador = null, vigia = null, python = null, token = '', estado = { estado: 'parado' };
// Por defecto se usa el motor que ya corre en este equipo (servidor-pc.bat, puerto 8777: piloto real, datos y central).
// Solo con externo = '' el paquete arranca un motor propio.
const EXTERNO = 'http://127.0.0.1:8777';
const logs = [];
const anotar = (tipo, txt) => {
  for (const l of String(txt).split(/\r?\n/)) if (l.trim()) logs.push(`${new Date().toISOString().slice(11, 19)} ${tipo} ${token ? l.split(token).join('•••') : l}`.slice(0, 400));
  logs.splice(0, Math.max(0, logs.length - 200));
};
const ajustes = async () => ({ lan: false, puerto: 0, externo: EXTERNO, ...(apolo.almacen.leer('ajustes') || {}) });
async function sondear(url) {
  try { const r = await fetch(url + '/api/salud', { signal: AbortSignal.timeout(3000), redirect: 'error' }); return r.ok && (await r.json()).ok === true; } catch { return false; }
}
// motor externo: se comprueba cada 15 s; el destino (lo que usa el núcleo de APOLO) solo existe mientras responde
async function vigilarExterno(url) {
  const vivo = await sondear(url);
  if (vivo && estado.estado !== 'activo') {
    estado = { estado: 'activo', externo: true, url, puerto: +new URL(url).port, desde: Date.now() };
    try { apolo.almacen.guardar('destino', { url, externo: true, desde: estado.desde }); } catch (e) { anotar('!', e.message); }
    apolo.log(`usando el motor de este equipo en ${url}`);
  } else if (!vivo && estado.estado !== 'esperando') {
    estado = { estado: 'esperando', externo: true, url, error: `Tu motor de DMN Trader (${url}) no responde. Ábrelo con servidor-pc.bat; esta pestaña se conecta sola en cuanto arranque.` };
    try { apolo.almacen.borrar('destino'); } catch { }
  }
}
const rutaDatos = () => apolo.config.datos || path.join(apolo.almacen.ruta, 'datos', 'trader.db');

// ---------- Python ----------
function buscarPython() {
  const candidatos = [apolo.config.python ? [apolo.config.python] : null, ...(process.platform === 'win32' ? [['py', '-3'], ['python'], ['python3']] : [['python3'], ['python']])].filter(Boolean);
  let sinNumpy = null;
  for (const [cmd, ...pre] of candidatos) {
    const r = spawnSync(cmd, [...pre, '-c', 'import sys;print("%d.%d" % sys.version_info[:2]);import numpy'], { encoding: 'utf8', windowsHide: true, timeout: 20000 });
    const v = (r.stdout || '').trim().split(/\s+/)[0];
    if (!/^\d+\.\d+$/.test(v)) continue;
    const [ma, mi] = v.split('.').map(Number);
    if (ma < 3 || (ma === 3 && mi < 10)) continue;
    if (r.status === 0) return { cmd, pre, version: v, numpy: true };
    sinNumpy = sinNumpy || { cmd, pre, version: v, numpy: false };
  }
  return sinNumpy;
}

// ---------- motor ----------
async function arrancar() {
  if (proc) return;
  clearTimeout(temporizador); clearInterval(vigia); parando = false;
  const a0 = await ajustes();
  if (a0.externo) {
    token = (await apolo.secretos.leer('dmn-trader:token')) || '';
    if (!token) { token = crypto.randomBytes(24).toString('hex'); await apolo.secretos.guardar('dmn-trader:token', token); }   // el núcleo exige que exista
    estado = { estado: 'parado' };
    await vigilarExterno(a0.externo);
    vigia = setInterval(() => vigilarExterno(a0.externo).catch(e => anotar('!', e.message)), 15_000);
    return;
  }
  python = buscarPython();
  if (!python) { estado = { estado: 'sin_python', error: 'No encuentro Python 3.10 o superior. Instálalo desde python.org y reinicia el motor.' }; return; }
  if (!python.numpy) { estado = { estado: 'sin_numpy', error: `Python ${python.version} no tiene numpy.`, python: python.version }; return; }
  token = await apolo.secretos.leer('dmn-trader:token');
  if (!token) { token = crypto.randomBytes(24).toString('hex'); await apolo.secretos.guardar('dmn-trader:token', token); }
  const gmgn = await apolo.secretos.leer('dmn-trader:gmgn').catch(() => '');
  const a = await ajustes();
  try { fs.mkdirSync(path.dirname(rutaDatos()), { recursive: true }); } catch { }   // fuera del almacén la crea el propio motor
  const env = { ...process.env, DMN_TRADER_DB: rutaDatos(), DMN_TRADER_TOKEN: token, DMN_TRADER_VIGILAR_STDIN: '1',
    PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8', ...(gmgn ? { GMGN_API_KEY: gmgn } : {}) };
  delete env.APOLO_PLUGIN;
  const args = [...python.pre, '-m', 'trader.servidor', '--host', a.lan ? '0.0.0.0' : '127.0.0.1', '--puerto', String(a.puerto || 0)];
  estado = { estado: 'arrancando', python: python.version, datos: rutaDatos(), lan: !!a.lan };
  // stdin abierto = APOLO vivo: si el proceso del paquete muere, el motor lo nota (EOF) y se cierra solo
  const p = spawn(python.cmd, args, { cwd: RAIZ, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
  proc = p;
  let buffer = '';
  p.stdout.on('data', d => {
    buffer += d; let i;
    while ((i = buffer.indexOf('\n')) >= 0) {
      const l = buffer.slice(0, i).trim(); buffer = buffer.slice(i + 1);
      const m = l.match(/^DMN_TRADER_PUERTO=(\d+)$/);
      if (m) {
        estado = { ...estado, estado: 'activo', puerto: +m[1], url: `http://127.0.0.1:${m[1]}`, pid: p.pid, desde: Date.now() }; reintentos = 0;
        try { apolo.almacen.guardar('destino', { url: estado.url, pid: p.pid, desde: estado.desde }); } catch (e) { anotar('!', e.message); }
        apolo.log(`motor en ${estado.url}`);
      } else anotar('·', l);
    }
  });
  p.stderr.on('data', d => anotar('!', d));
  p.on('error', e => { anotar('!', e.message); estado = { estado: 'caido', error: e.message }; });
  p.on('exit', code => {
    if (proc === p) proc = null;
    apolo.almacen.borrar('destino');
    if (parando) { estado = { estado: 'parado' }; return; }
    const espera = Math.min(60_000, 2000 * 2 ** reintentos++);
    estado = { estado: 'caido', codigo: code, error: logs.filter(l => l.includes(' ! ')).slice(-3).join(' | ') || `salió con código ${code}`, reintentoEn: espera };
    temporizador = setTimeout(() => arrancar().catch(e => anotar('!', e.message)), espera);
  });
}
function parar() {
  parando = true; clearTimeout(temporizador); clearInterval(vigia);
  if (estado.externo) { try { apolo.almacen.borrar('destino'); } catch { } estado = { estado: 'parado' }; return Promise.resolve(); }   // el motor externo no es nuestro: no se toca
  const p = proc; if (!p) { estado = { estado: 'parado' }; return Promise.resolve(); }
  return new Promise(ok => { const t = setTimeout(() => { try { p.kill('SIGKILL'); } catch { } ok(); }, 5000); p.once('exit', () => { clearTimeout(t); ok(); }); try { p.stdin.end(); p.kill(); } catch { ok(); } });
}
const reiniciar = async () => { await parar(); reintentos = 0; await arrancar(); return publico(); };
const publico = () => ({ ...estado, logs: logs.slice(-40), plataforma: process.platform });

// ---------- herramientas ----------
function ejecutarCliente(orden) {
  if (!estado.url) throw new Error('el motor de DMN Trader no está en marcha (mira Trading → Motor en el panel)');
  if (!python) python = buscarPython();
  if (!python) throw new Error('No encuentro Python 3.10 o superior para el cliente de DMN Trader.');
  return new Promise((ok, mal) => {
    const p = spawn(python.cmd, [...python.pre, path.join(__dirname, 'trader.py'), ...orden], { cwd: RAIZ, windowsHide: true,
      env: { ...process.env, DMN_TRADER_URL: estado.url, DMN_TRADER_TOKEN: token, PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } });
    let out = '', err = '';
    const t = setTimeout(() => { p.kill(); mal(new Error('DMN Trader tardó más de 150 s')); }, 150_000);
    p.stdout.on('data', d => { out += d; }); p.stderr.on('data', d => { err += d; });
    p.on('exit', code => { clearTimeout(t); const txt = (out + (code ? '\n' + err : '')).split(token).join('•••').trim(); code ? mal(new Error(txt.slice(-1500) || `código ${code}`)) : ok(txt.slice(0, 12000) || 'hecho'); });
  });
}
function validarOrden(orden, escritura) {
  if (!Array.isArray(orden) || !orden.length || orden.length > 30 || orden.some(x => typeof x !== 'string' && typeof x !== 'number')) throw new Error('orden: lista de textos, p. ej. ["precio","AAPL"]');
  const o = orden.map(String), c = o[0].toLowerCase(), sub = (o[1] || '').toLowerCase();
  if (['enjambre', 'agente'].includes(c) && ['iniciar', 'parar'].includes(sub)) throw new Error('el enjambre se arranca y para con trading_autopiloto (con duración y pérdida máxima)');
  if (!escritura) {
    if (!LECTURA.has(c)) throw new Error(`"${c}" no es una consulta: usa trader_operar`);
    if (c === 'reglas' && o.length > 1) throw new Error('cambiar reglas es trader_operar');
    if (SUB_LECTURA[c] && !SUB_LECTURA[c].includes(sub) && !/^\d+$/.test(sub)) throw new Error(`"${c} ${sub}" no es una consulta`);
  } else {
    if (!ESCRITURA.has(c)) throw new Error(`"${c}" no es una orden de operar: usa trader_consultar`);
    if (c === 'reglas' && o.length < 2) throw new Error('para ver las reglas usa trader_consultar');
    if (SUB_ESCRITURA[c] && !SUB_ESCRITURA[c].includes(sub)) throw new Error(`"${c} ${sub}" no es una orden de operar`);
  }
  return o;
}
const ORDEN = { type: 'object', properties: { orden: { type: 'array', items: { type: 'string' }, description: 'p. ej. ["precio","BTC-USD"] o ["comprar","AAPL","10","sl=180","tp=210"]' } }, required: ['orden'] };

module.exports = definirPlugin({
  async activar(a) {
    apolo = a;
    await apolo.registrarHerramienta({
      nombre: 'trader_guia', descripcion: 'Guía de DMN Trader y referencias',
      parametros: { type: 'object', properties: { tema: { type: 'string', enum: ['guia', 'estrategias', 'riesgo', 'glosario', 'plan-aprendizaje'] } } },
      ejecutar: async ({ tema = 'guia' }) => fs.readFileSync(tema === 'guia' ? path.join(__dirname, 'SKILL.md') : path.join(__dirname, 'references', `${tema.replace(/[^\w-]/g, '')}.md`), 'utf8'),
    });
    await apolo.registrarHerramienta({ nombre: 'trader_consultar', descripcion: 'DMN Trader (lectura)', parametros: ORDEN, ejecutar: ({ orden }) => ejecutarCliente(validarOrden(orden, false)) });
    await apolo.registrarHerramienta({ nombre: 'trader_operar', descripcion: 'DMN Trader (operar, simulado)', parametros: ORDEN, ejecutar: ({ orden }) => ejecutarCliente(validarOrden(orden, true)) });
    await apolo.registrarVista({
      nombre: 'trading', titulo: 'Trading', icono: 'grafica',
      acciones: {
        estado: async () => ({ ...publico(), gmgn: !!(await apolo.secretos.leer('dmn-trader:gmgn').catch(() => '')), ajustes: await ajustes() }),
        reiniciar,
        arrancar: async () => { await arrancar(); return publico(); },
        parar: async () => { await parar(); return publico(); },
        // la mesa completa (velas, órdenes, diario, backtest, práctica) es el panel web del propio motor
        mesa: async () => { if (!estado.url) throw new Error('el motor no está en marcha'); return { url: estado.externo ? estado.url + '/' : `${estado.url}/#token=${token}` }; },
        instalarNumpy: async () => {
          const py = buscarPython(); if (!py) throw new Error('primero instala Python 3.10+');
          const r = spawnSync(py.cmd, [...py.pre, '-m', 'pip', 'install', '--user', 'numpy'], { encoding: 'utf8', windowsHide: true, timeout: 600_000 });
          anotar('pip', (r.stdout || '').slice(-800) + (r.stderr || '').slice(-800));
          if (r.status !== 0) throw new Error('pip no pudo instalar numpy: ' + (r.stderr || '').trim().split('\n').pop());
          return reiniciar();
        },
        guardarGmgn: async ({ clave }) => {
          if (typeof clave !== 'string' || !/^[\w.-]{16,200}$/.test(clave.trim())) throw new Error('clave de GMGN no válida');
          await apolo.secretos.guardar('dmn-trader:gmgn', clave.trim()); return reiniciar();
        },
        borrarGmgn: async () => { await apolo.secretos.guardar('dmn-trader:gmgn', ''); return reiniciar(); },
        ajustes: async ({ lan, puerto, externo }) => {
          const x = await ajustes();
          if (externo !== undefined) { if (externo !== '' && !/^http:\/\/127\.0\.0\.1:\d{1,5}$/.test(externo)) throw new Error('motor externo: http://127.0.0.1:<puerto> o vacío para arrancar uno propio'); x.externo = externo; }
          if (typeof lan === 'boolean') x.lan = lan;
          if (puerto !== undefined) { if (!Number.isInteger(puerto) || (puerto !== 0 && (puerto < 1024 || puerto > 65535))) throw new Error('puerto 0 (automático) o 1024-65535'); x.puerto = puerto; }
          apolo.almacen.guardar('ajustes', x); return reiniciar();
        },
        // datos para que otro equipo (p. ej. una Raspberry Pi) se una como nodo; solo si se permite la red local
        nodo: async () => {
          const a = await ajustes(); if (!a.lan || !estado.puerto) throw new Error('activa "Permitir que otros equipos se unan" primero');
          const ips = Object.values(os.networkInterfaces()).flat().filter(i => i && i.family === 'IPv4' && !i.internal).map(i => i.address);
          return { centrales: ips.map(ip => `http://${ip}:${estado.puerto}`), token };
        },
      },
    });
    await arrancar();
  },
  async desactivar() { await parar(); },
});
