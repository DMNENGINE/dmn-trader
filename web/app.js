// DMN Trader — panel web. Sin frameworks: helpers pequeños + Lightweight Charts.
'use strict';
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const E = { cuenta: null, cuentas: [], simbolo: localStorage.getItem('dmn.sim') || 'SPY', intervalo: localStorage.getItem('dmn.int') || '1d',
  lado: 'buy', capas: new Set(JSON.parse(localStorage.getItem('dmn.capas') || '["sma"]')), velas: [], analisis: null, sub: 'pos', auto: null, estr: {} };
const INTERVALOS = ['5m', '15m', '60m', '1d', '1wk'];

// token (cuando el servidor está en la Pi): se pasa una vez como http://pi:8777/#token=XXXX y se recuerda
try { const m = location.hash.match(/token=([^&]+)/); if (m) { localStorage.setItem('dmn.token', m[1]); history.replaceState(null, '', location.pathname); } } catch { }
const TOKEN = (() => { try { return localStorage.getItem('dmn.token') || ''; } catch { return ''; } })();
async function api(ruta, metodo = 'GET', cuerpo) {
  const h = { ...(cuerpo ? { 'content-type': 'application/json' } : {}), ...(TOKEN ? { 'x-token': TOKEN } : {}) };
  const r = await fetch('/api' + ruta, { method: metodo, headers: h, body: cuerpo ? JSON.stringify(cuerpo) : undefined });
  const j = await r.json().catch(() => ({ error: 'respuesta inválida' }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const n = (x, d = 2) => x == null || isNaN(x) ? '—' : Number(x).toLocaleString('es-ES', { minimumFractionDigits: d, maximumFractionDigits: d });
const pd = x => x == null ? 2 : Math.abs(x) >= 1000 ? 2 : Math.abs(x) >= 1 ? 2 : 5;
const np = x => n(x, pd(x));
const col = x => x > 0 ? 'verde' : x < 0 ? 'rojo' : '';
const sg = (x, d = 2) => x == null ? '—' : `<span class="${col(x)}">${x > 0 ? '+' : ''}${n(x, d)}</span>`;
const fecha = t => t ? new Date(t * 1000).toLocaleString('es-ES', { day: '2-digit', month: '2-digit', year: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
function msg(el, texto, bien) { const m = $(el); m.className = 'msg ' + (bien ? 'bien' : 'mal'); m.textContent = texto; }
function toast(t) { const d = document.createElement('div'); d.className = 'toast'; d.textContent = t; document.body.appendChild(d); setTimeout(() => d.remove(), 4500); }
function tabla(cols, filas, vacio = 'Nada todavía.') {
  if (!filas.length) return `<div class="tenue">${vacio}</div>`;
  return `<table><thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join('')}</tr></thead><tbody>${filas.map(f => `<tr>${cols.map(c => `<td>${c[1](f)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
const esReplay = () => E.cuenta && E.cuenta.tipo === 'replay';

// ---------- gráficos ----------
const LW = LightweightCharts;
const temaG = { layout: { background: { color: 'transparent' }, textColor: '#8592a6', fontFamily: 'Inter, Segoe UI, sans-serif' },
  grid: { vertLines: { color: '#162032' }, horzLines: { color: '#162032' } }, rightPriceScale: { borderColor: '#1f2a3c' },
  timeScale: { borderColor: '#1f2a3c', timeVisible: true }, crosshair: { mode: 0 } };
const G = {};
function crearGraficos() {
  G.c = LW.createChart($('#chart'), { ...temaG, autoSize: true });
  G.velas = G.c.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
  G.vol = G.c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', color: '#334155' });
  G.c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  const linea = (color, w = 1) => G.c.addLineSeries({ color, lineWidth: w, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  G.s20 = linea('#f59e0b'); G.s50 = linea('#3b82f6'); G.s200 = linea('#a855f7', 2);
  G.bbS = linea('#64748b'); G.bbM = linea('#475569'); G.bbI = linea('#64748b'); G.vwap = linea('#06b6d4');
  G.r = LW.createChart($('#chartRsi'), { ...temaG, autoSize: true, timeScale: { visible: false } });
  G.rsi = G.r.addLineSeries({ color: '#a78bfa', lineWidth: 1, priceLineVisible: false });
  [70, 30].forEach(v => G.rsi.createPriceLine({ price: v, color: '#334155', lineStyle: 2, axisLabelVisible: false }));
  G.c.timeScale().subscribeVisibleLogicalRangeChange(r => r && G.r.timeScale().setVisibleLogicalRange(r));
  G.lineasPrecio = [];
  G.c.subscribeClick(p => { if (p.point && G.velas) { const px = G.velas.coordinateToPrice(p.point.y); if (px && $('#tTipo').value !== 'mercado') { ($('#tTipo').value === 'stop' ? $('#tStop') : $('#tLim')).value = (+px).toFixed(pd(px)); } } });
}
// cálculos locales de indicadores para dibujar (el servidor hace los suyos)
const sma = (x, k) => x.map((_, i) => i < k - 1 ? null : x.slice(i - k + 1, i + 1).reduce((a, b) => a + b, 0) / k);
function rsiL(c, k = 14) { const o = Array(c.length).fill(null); if (c.length <= k) return o; let g = 0, p = 0;
  for (let i = 1; i <= k; i++) { const d = c[i] - c[i - 1]; g += Math.max(d, 0); p += Math.max(-d, 0); } g /= k; p /= k; o[k] = p ? 100 - 100 / (1 + g / p) : 100;
  for (let i = k + 1; i < c.length; i++) { const d = c[i] - c[i - 1]; g = (g * (k - 1) + Math.max(d, 0)) / k; p = (p * (k - 1) + Math.max(-d, 0)) / k; o[i] = p ? 100 - 100 / (1 + g / p) : 100; } return o; }
function serie(v, vals) { return v.map((b, i) => vals[i] == null ? null : { time: b.t, value: vals[i] }).filter(Boolean); }
function dibujar(v) {
  E.velas = v;
  G.velas.setData(v.map(b => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
  G.vol.setData(v.map(b => ({ time: b.t, value: b.v, color: b.c >= b.o ? '#14532d' : '#7f1d1d' })));
  const c = v.map(b => b.c), on = E.capas;
  G.s20.setData(on.has('sma') ? serie(v, sma(c, 20)) : []); G.s50.setData(on.has('sma') ? serie(v, sma(c, 50)) : []); G.s200.setData(on.has('sma') ? serie(v, sma(c, 200)) : []);
  if (on.has('bb')) { const m = sma(c, 20), sd = c.map((_, i) => i < 19 ? null : Math.sqrt(c.slice(i - 19, i + 1).reduce((a, x) => a + (x - m[i]) ** 2, 0) / 20));
    G.bbM.setData(serie(v, m)); G.bbS.setData(serie(v, m.map((x, i) => x == null ? null : x + 2 * sd[i]))); G.bbI.setData(serie(v, m.map((x, i) => x == null ? null : x - 2 * sd[i]))); }
  else [G.bbM, G.bbS, G.bbI].forEach(s => s.setData([]));
  if (on.has('vwap')) { let pv = 0, vv = 0, dia = null; G.vwap.setData(v.map(b => { const d = new Date(b.t * 1000).toISOString().slice(0, 10); if (d !== dia) { pv = vv = 0; dia = d; } const tp = (b.h + b.l + b.c) / 3; pv += tp * (b.v || 0); vv += b.v || 0; return { time: b.t, value: vv ? pv / vv : tp }; })); }
  else G.vwap.setData([]);
  G.rsi.setData(serie(v, rsiL(c)));
  marcas();
}
function marcas() {
  G.lineasPrecio.forEach(l => G.velas.removePriceLine(l)); G.lineasPrecio = [];
  if (!E.cuenta) return;
  const p = (E.cuenta.posiciones || []).find(x => x.simbolo === E.simbolo);
  const add = (price, color, title) => price && G.lineasPrecio.push(G.velas.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, title }));
  if (p) { add(p.precio_medio, '#3b82f6', `${p.lado} ${n(Math.abs(p.cantidad), 4)}`); add(p.stop, '#ef4444', 'SL'); add(p.objetivo, '#22c55e', 'TP'); }
  (E.cuenta.ordenes || []).filter(o => o.simbolo === E.simbolo && !o.reduce).forEach(o => add(o.limite || o.stop, '#f59e0b', `${o.lado === 'buy' ? 'C' : 'V'} ${o.tipo}`));
}
async function cargarGrafico(ajustar = true) {
  $('#gSimbolo').textContent = E.simbolo;
  try {
    let v;
    if (esReplay()) { const r = await api(`/cuentas/${E.cuenta.id}/replay`); v = r.velas; E.simbolo = r.simbolo; $('#gSimbolo').textContent = r.simbolo;
      $('#rInfo').textContent = `${r.simbolo} · ${r.intervalo} · vela ${r.idx + 1}/${r.total} · ${fecha(v[v.length - 1].t)}${r.fin ? ' · FIN' : ''}`; }
    else v = await api(`/velas?s=${encodeURIComponent(E.simbolo)}&i=${E.intervalo}`);
    dibujar(v);
    if (ajustar) G.c.timeScale().setVisibleLogicalRange({ from: v.length - 150, to: v.length + 3 });
    const u = v[v.length - 1];
    if (esReplay()) { $('#gPrecio').textContent = np(u.c); $('#gNombre').textContent = '(pasado)'; $('#gCambio').innerHTML = ''; }
  } catch (e) { toast('Gráfico: ' + e.message); }
}
async function cargarCotizacion() {
  if (esReplay()) return;
  try { const q = await api(`/cotizacion?s=${encodeURIComponent(E.simbolo)}`); E.q = q;
    $('#gNombre').textContent = q.nombre + (q.abierto ? '' : ' · cerrado'); $('#gPrecio').textContent = np(q.precio); $('#gCambio').innerHTML = sg(q.cambio_pct) + '%';
    const u = E.velas[E.velas.length - 1];
    if (u && E.intervalo !== '1wk') { const b = { time: u.t, open: u.o, high: Math.max(u.h, q.precio), low: Math.min(u.l, q.precio), close: q.precio }; G.velas.update(b); }
  } catch { }
}
async function cargarAnalisis() {
  const el = $('#analisis');
  if (esReplay()) { el.textContent = 'En práctica no hay lectura automática: decide tú.'; return; }
  el.textContent = 'analizando…';
  try { const a = await api(`/analisis?s=${encodeURIComponent(E.simbolo)}`); E.analisis = a;
    el.innerHTML = `<b>Sesgo: ${a.sesgo_tecnico}</b><br>RSI ${n(a.rsi14, 0)} · ATR ${n(a.atr_pct)}% · 52s ${a.rango_52s.posicion_pct ?? '—'}%<br>
      1m ${sg(a.rendimiento['1m'])}% · 3m ${sg(a.rendimiento['3m'])}% · 1a ${sg(a.rendimiento['1a'])}%<br>
      Soportes: ${a.soportes.map(np).join(' · ') || '—'}<br>Resistencias: ${a.resistencias.map(np).join(' · ') || '—'}<br>
      ${a.senales.map(s => '• ' + esc(s)).join('<br>')}<br><span class="chico">${a.aviso}</span>`;
  } catch (e) { el.textContent = e.message; }
}

// ---------- cuenta ----------
async function cargarCuentas() {
  E.cuentas = await api('/cuentas');
  const def = +(localStorage.getItem('dmn.cuenta') || 0);
  const sel = $('#selCuenta');
  sel.innerHTML = E.cuentas.map(c => `<option value="${c.id}">${c.tipo === 'replay' ? '🎮 ' : ''}${esc(c.nombre)}</option>`).join('');
  sel.value = E.cuentas.some(c => c.id === def) ? def : E.cuentas[0]?.id;
}
async function cargarCuenta() {
  const id = +$('#selCuenta').value; if (!id) return;
  const antes = E.cuenta?.tipo;
  E.cuenta = await api(`/cuentas/${id}`);
  const c = E.cuenta;
  $('#kpis').innerHTML = [['Equity', `<b>${n(c.equity)} ${c.moneda}</b>`], ['P&L hoy', `<b>${sg(c.pnl_dia)} (${sg(c.pnl_dia_pct)}%)</b>`],
    ['P&L total', `<b>${sg(c.pnl_total)} (${sg(c.pnl_total_pct)}%)</b>`], ['Efectivo', `<b>${n(c.efectivo)}</b>`], ['Poder de compra', `<b>${n(c.poder_compra)}</b>`],
    ['Posiciones', `<b>${c.posiciones_abiertas}</b>`], ...(c.bloqueada ? [['Estado', '<b class="rojo">BLOQUEADA HOY</b>']] : [])]
    .map(([a, b]) => `<div class="kpi"><small>${a}</small>${b}</div>`).join('');
  $('#controlReplay').classList.toggle('oculto', !esReplay());
  $('#intervalos').classList.toggle('oculto', esReplay());
  $('.vig').classList.toggle('oculto', esReplay());
  if (antes !== c.tipo || esReplay()) { if (esReplay()) E.simbolo = c.replay.simbolo; }
  pintarAbajo(); marcas();
  return antes !== c.tipo;
}
function pintarAbajo() {
  const c = E.cuenta; if (!c) return; let h = '';
  if (E.sub === 'pos') h = tabla([['Símbolo', p => `<a href="#" data-ver="${p.simbolo}">${p.simbolo}</a>`], ['Lado', p => p.lado], ['Cantidad', p => n(Math.abs(p.cantidad), 4)],
    ['Precio medio', p => np(p.precio_medio)], ['Actual', p => np(p.precio)], ['Valor', p => n(p.valor)], ['P&L', p => sg(p.pnl)], ['%', p => sg(p.pnl_pct)],
    ['SL', p => p.stop ? np(p.stop) : '<span class="ambar">sin stop</span>'], ['TP', p => p.objetivo ? np(p.objetivo) : '—'],
    ['', p => `<button data-be="${p.simbolo}">SL a BE</button> <button data-mover="${p.simbolo}">SL/TP</button> <button data-medio="${p.simbolo}">½</button> <button data-cerrar="${p.simbolo}">Cerrar</button>`]], c.posiciones, 'Sin posiciones abiertas.');
  else if (E.sub === 'ord') h = tabla([['#', o => o.id], ['Símbolo', o => o.simbolo], ['Lado', o => o.lado === 'buy' ? '<span class="verde">compra</span>' : '<span class="rojo">venta</span>'],
    ['Tipo', o => o.tipo + (o.reduce ? ' (salida)' : '')], ['Cantidad', o => n(o.cantidad, 4)], ['Límite', o => np(o.limite)], ['Stop', o => np(o.stop)], ['Estado', o => o.estado], ['Creada', o => fecha(o.creada)],
    ['', o => `<button data-cancelar="${o.id}">Cancelar</button>`]], c.ordenes, 'Sin órdenes pendientes.');
  else { api(`/cuentas/${c.id}/ejecuciones`).then(ej => { $('#tablaAbajo').innerHTML = tabla([['Hora', x => fecha(x.ts)], ['Símbolo', x => x.simbolo], ['Lado', x => x.lado === 'buy' ? 'compra' : 'venta'],
    ['Cantidad', x => n(x.cantidad, 4)], ['Precio', x => np(x.precio)], ['Comisión', x => n(x.comision)], ['P&L', x => x.pnl ? sg(x.pnl) : '—']], ej); }); return; }
  $('#tablaAbajo').innerHTML = h;
}

// ---------- vigilancia ----------
async function cargarVig() {
  try { const l = await api('/vigilancia');
    $('#listaVig').innerHTML = l.map(x => `<div class="fv ${x.simbolo === E.simbolo ? 'on' : ''}" data-ver="${x.simbolo}"><div><div class="s">${x.simbolo}</div><div class="n">${esc(x.nombre || x.error || '')}</div></div>
      <div class="p">${np(x.precio)}<br>${x.cambio_pct != null ? sg(x.cambio_pct) + '%' : ''}</div><button class="x" data-quitar="${x.simbolo}" title="Quitar">✕</button></div>`).join('');
  } catch { }
}
let tBuscar;
$('#buscar').addEventListener('input', e => { clearTimeout(tBuscar); const t = e.target.value.trim(); if (!t) { $('#resBuscar').innerHTML = ''; return; }
  tBuscar = setTimeout(async () => { try { const r = await api('/buscar?q=' + encodeURIComponent(t));
    $('#resBuscar').innerHTML = r.resultados.slice(0, 7).map(x => `<div data-anadir="${x.simbolo}"><b>${x.simbolo}</b> <span class="tenue chico">${esc(x.nombre || '')} · ${x.tipo || ''} ${x.bolsa || ''}</span></div>`).join(''); } catch { } }, 300); });
$('#buscar').addEventListener('keydown', e => { if (e.key === 'Enter') { const d = $('#resBuscar div'); if (d) d.click(); } });

function verSimbolo(s) { E.simbolo = s; localStorage.setItem('dmn.sim', s); $$('.fv').forEach(f => f.classList.toggle('on', f.dataset.ver === s));
  limpiarTicket(); cargarGrafico(); cargarCotizacion(); cargarAnalisis(); }

// ---------- ticket ----------
function limpiarTicket() { ['#tCant', '#tImporte', '#tLim', '#tStop', '#tSL', '#tTP'].forEach(i => $(i).value = ''); $('#tRiesgoInfo').textContent = ''; $('#tMsg').textContent = ''; }
function ponerLado(l) { E.lado = l; $$('.lado button').forEach(b => b.classList.toggle('on', b.dataset.lado === l));
  const b = $('#btnEnviar'); b.className = 'enviar ' + (l === 'buy' ? 'compra' : 'venta'); b.textContent = l === 'buy' ? 'COMPRAR' : 'VENDER'; }
function ponerTipo() { const t = $('#tTipo').value; $('#lLim').classList.toggle('oculto', !['limite', 'stop_limite'].includes(t));
  $('#lStop').classList.toggle('oculto', !['stop', 'stop_limite'].includes(t)); $('#lTrail').classList.toggle('oculto', t !== 'trailing'); }
const precioRef = () => +($('#tLim').value || $('#tStop').value || (esReplay() ? E.velas[E.velas.length - 1]?.c : E.q?.precio) || 0);
function atrLocal() { const v = E.velas; if (v.length < 15) return null; let a = 0; for (let i = v.length - 14; i < v.length; i++) a += Math.max(v[i].h - v[i].l, Math.abs(v[i].h - v[i - 1].c), Math.abs(v[i].l - v[i - 1].c)); return a / 14; }
$('#btnAtr').onclick = () => { const p = precioRef(), a = atrLocal(); if (!p || !a) return; const s = E.lado === 'buy' ? 1 : -1;
  $('#tSL').value = (p - s * 2 * a).toFixed(pd(p)); $('#tTP').value = (p + s * 4 * a).toFixed(pd(p)); $('#tRiesgoInfo').textContent = `ATR(14) = ${np(a)} · stop a 2 ATR, objetivo a 2R`; };
$('#btnRiesgo').onclick = async () => { const sl = +$('#tSL').value; if (!sl) return msg('#tMsg', 'Pon primero el stop-loss (o pulsa SL/TP por ATR).');
  try { const r = await api(`/cuentas/${E.cuenta.id}/tamano?s=${encodeURIComponent(E.simbolo)}&entrada=${precioRef()}&stop=${sl}`);
    $('#tCant').value = r.cantidad; $('#tImporte').value = '';
    $('#tRiesgoInfo').innerHTML = `Arriesgas <b>${n(r.riesgo)}</b> (${r.riesgo_pct}%) · stop a ${r.distancia_stop_pct}% · posición ${n(r.valor_posicion)}${r.limitado_por_poder_compra ? ' · <span class="ambar">limitada por poder de compra</span>' : ''}`;
  } catch (e) { msg('#tMsg', e.message); } };
$('#btnEnviar').onclick = async () => {
  const t = $('#tTipo').value, cuerpo = { simbolo: E.simbolo, lado: E.lado, tipo: t, cantidad: $('#tCant').value || null, importe: $('#tImporte').value || null,
    limite: $('#tLim').value || null, stop: $('#tStop').value || null, trail_pct: t === 'trailing' ? $('#tTrail').value : null,
    sl: $('#tSL').value || null, tp: $('#tTP').value || null, setup: $('#tSetup').value || null, nota: $('#tNota').value || null };
  try { const o = await api(`/cuentas/${E.cuenta.id}/ordenes`, 'POST', cuerpo);
    msg('#tMsg', `Orden #${o.id} ${o.estado}${o.precio_ejec ? ' a ' + np(o.precio_ejec) : ''}${o.avisos.length ? ' · ⚠ ' + o.avisos.join(' · ') : ''}`, true);
    limpiarTicket(); $('#tNota').value = ''; await cargarCuenta();
  } catch (e) { msg('#tMsg', e.message); } };

// ---------- acciones de tabla (delegación) ----------
document.addEventListener('click', async e => {
  const d = e.target.closest('[data-ver],[data-quitar],[data-anadir],[data-cerrar],[data-medio],[data-be],[data-mover],[data-cancelar],[data-anotar],[data-abrir-practica],[data-borrar-alerta]');
  if (!d) return; const ds = d.dataset; const cid = E.cuenta?.id;
  try {
    if (ds.quitar) { e.stopPropagation(); await api('/vigilancia/' + encodeURIComponent(ds.quitar), 'DELETE'); return cargarVig(); }
    if (ds.anadir) { await api('/vigilancia', 'POST', { simbolo: ds.anadir }); $('#buscar').value = ''; $('#resBuscar').innerHTML = ''; await cargarVig(); irA('mesa'); return verSimbolo(ds.anadir); }
    if (ds.ver) { e.preventDefault(); if (!esReplay()) { irA('mesa'); verSimbolo(ds.ver); } return; }
    if (ds.cerrar) await api(`/cuentas/${cid}/cerrar`, 'POST', { simbolo: ds.cerrar });
    if (ds.medio) await api(`/cuentas/${cid}/cerrar`, 'POST', { simbolo: ds.medio, porcentaje: 50 });
    if (ds.be) { const p = E.cuenta.posiciones.find(x => x.simbolo === ds.be); await api(`/cuentas/${cid}/stop`, 'POST', { simbolo: ds.be, stop: p.precio_medio }); toast('Stop movido a breakeven: este trade ya no puede perder (salvo hueco).'); }
    if (ds.mover) return modalStop(ds.mover);
    if (ds.cancelar) await api(`/cuentas/${cid}/ordenes/${ds.cancelar}`, 'DELETE');
    if (ds.anotar) return modalDiario(+ds.anotar);
    if (ds.abrirPractica) { $('#selCuenta').value = ds.abrirPractica; await cambiarCuenta(); return irA('mesa'); }
    if (ds.borrarAlerta) { await api('/alertas/' + ds.borrarAlerta, 'DELETE'); return cargarAlertas(); }
    await cargarCuenta();
  } catch (err) { toast(err.message); }
});
function modal(html) { $('#modalCuerpo').innerHTML = html; $('#modal').classList.remove('oculto'); }
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal' || e.target.dataset.cerrarModal != null) $('#modal').classList.add('oculto'); });
function modalStop(s) { const p = E.cuenta.posiciones.find(x => x.simbolo === s);
  modal(`<div class="cab">${s}: stop y objetivo</div><div class="form"><div class="fila2"><label>Stop-loss <input id="mS" type="number" step="any" value="${p.stop ?? ''}"></label><label>Take-profit <input id="mT" type="number" step="any" value="${p.objetivo ?? ''}"></label></div>
  <label>o trailing stop % <input id="mTr" type="number" step="any" placeholder="p. ej. 3"></label><button id="mOk" class="enviar">Guardar</button><button data-cerrar-modal class="sec ancho">Cancelar</button><div id="mMsg" class="msg"></div></div>`);
  $('#mOk').onclick = async () => { try { await api(`/cuentas/${E.cuenta.id}/stop`, 'POST', { simbolo: s, stop: $('#mTr').value ? null : ($('#mS').value || null), objetivo: $('#mT').value || 0, trail_pct: $('#mTr').value || null });
    $('#modal').classList.add('oculto'); cargarCuenta(); } catch (e) { msg('#mMsg', e.message); } }; }

// ---------- diario ----------
async function cargarDiario() {
  const d = await api(`/cuentas/${E.cuenta.id}/diario`); E.diario = d;
  $('#tablaDiario').innerHTML = tabla([['#', t => t.id], ['Símbolo', t => t.simbolo], ['Dir.', t => t.direccion], ['Estado', t => t.estado], ['Entrada', t => np(t.entrada)], ['Salida', t => np(t.salida)],
    ['P&L', t => t.estado === 'cerrado' ? sg(t.pnl) : '—'], ['R', t => t.r != null ? sg(t.r) : '—'], ['MFE/MAE', t => `${np(t.mfe)} / ${np(t.mae)}`], ['Abierto', t => fecha(t.abierto)], ['Duración', t => t.cerrado ? durac(t.cerrado - t.abierto) : '—'],
    ['Setup', t => esc(t.setup || '—')], ['Emoción', t => esc(t.emocion || '')], ['Plan', t => t.respeto_plan == null ? '' : t.respeto_plan ? '✔' : '✘'],
    ['', t => `<button data-anotar="${t.id}">${t.nota || t.leccion ? 'Editar' : 'Anotar'}</button>`]], d, 'Aún no hay operaciones. Ve a la Mesa y haz la primera.');
}
const durac = s => s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${(s / 3600).toFixed(1)} h` : `${(s / 86400).toFixed(1)} d`;
function modalDiario(id) { const t = E.diario.find(x => x.id === id);
  modal(`<div class="cab">Trade #${t.id} · ${t.simbolo} ${t.direccion} · ${t.estado === 'cerrado' ? sg(t.pnl) : 'abierto'}</div><div class="form">
  <label>Setup <input id="dSetup" list="setups" value="${esc(t.setup || '')}"></label><label>Por qué entré <input id="dNota" value="${esc(t.nota || '')}"></label>
  <label>Emoción <select id="dEmo">${['', 'tranquilo', 'confiado', 'ansioso', 'miedo', 'codicia', 'FOMO', 'venganza', 'aburrido'].map(x => `<option ${x === t.emocion ? 'selected' : ''}>${x}</option>`).join('')}</select></label>
  <label>¿Respeté el plan? <select id="dPlan"><option value="">—</option><option value="1" ${t.respeto_plan === 1 ? 'selected' : ''}>Sí</option><option value="0" ${t.respeto_plan === 0 ? 'selected' : ''}>No</option></select></label>
  <label>Lección <input id="dLec" value="${esc(t.leccion || '')}" placeholder="¿qué haría distinto?"></label><label>Etiquetas (coma) <input id="dEtq" value="${esc(t.etiquetas || '')}"></label>
  <button id="dOk" class="enviar">Guardar</button><button data-cerrar-modal class="sec ancho">Cerrar</button><div id="dMsg" class="msg"></div></div>`);
  $('#dOk').onclick = async () => { try { await api(`/cuentas/${E.cuenta.id}/diario/${id}`, 'PATCH', { setup: $('#dSetup').value, nota: $('#dNota').value, emocion: $('#dEmo').value,
    respeto_plan: $('#dPlan').value === '' ? null : +$('#dPlan').value, leccion: $('#dLec').value, etiquetas: $('#dEtq').value }); $('#modal').classList.add('oculto'); cargarDiario(); } catch (e) { msg('#dMsg', e.message); } }; }

// ---------- estadísticas ----------
let gEq;
async function cargarStats() {
  const id = E.cuenta.id; const [s, eq, ex] = await Promise.all([api(`/cuentas/${id}/estadisticas`), api(`/cuentas/${id}/equity`), api(`/cuentas/${id}/examen`)]);
  const t = (a, b) => `<div class="tile"><small>${a}</small><b>${b}</b></div>`;
  $('#statsKpis').innerHTML = [t('Trades', s.trades), t('Acierto', s.trades ? n(s.tasa_acierto, 1) + '%' : '—'), t('Factor beneficio', n(s.factor_beneficio)), t('Esperanza/trade', sg(s.esperanza)),
    t('R medio', s.r_medio != null ? sg(s.r_medio) : '—'), t('Beneficio neto', sg(s.beneficio_neto)), t('Drawdown máx.', n(s.max_drawdown_pct) + '%'),
    t('Ganancia/pérdida media', `${n(s.ganancia_media)} / ${n(s.perdida_media)}`), t('Rachas', `${s.racha_ganadora ?? 0}G / ${s.racha_perdedora ?? 0}P`), t('Duración media', s.duracion_media_h != null ? s.duracion_media_h + ' h' : '—'),
    t('Con stop', s.con_stop_pct != null ? s.con_stop_pct + '%' : '—'), t('Diario anotado', s.anotados_pct != null ? s.anotados_pct + '%' : '—')].join('');
  if (!gEq) { gEq = LW.createChart($('#chartEquity'), { ...temaG, autoSize: true }); gEq.s = gEq.addAreaSeries({ lineColor: '#3b82f6', topColor: '#3b82f633', bottomColor: '#3b82f600', lineWidth: 2 }); }
  const vistos = new Set(); gEq.s.setData(eq.filter(e => !vistos.has(e.ts) && vistos.add(e.ts)).map(e => ({ time: e.ts, value: e.valor }))); gEq.timeScale().fitContent();
  $('#examen').innerHTML = `<div class="grande ${ex.aprobado ? 'verde' : 'ambar'}">${ex.cumplidos}/${ex.total} ${ex.aprobado ? '✔ listo' : 'todavía no'}</div>` +
    ex.criterios.map(c => `<div class="crit"><span>${c.ok ? '✅' : '⬜'}</span><span>${c.criterio}</span><span>${esc(c.valor)}</span></div>`).join('') + `<p class="tenue chico">${ex.nota}</p>`;
  const grupo = o => tabla([['', x => esc(x[0])], ['Trades', x => x[1].trades], ['Acierto', x => n(x[1].ganadores / x[1].trades * 100, 0) + '%'], ['P&L', x => sg(x[1].pnl)]], Object.entries(o || {}).sort((a, b) => b[1].pnl - a[1].pnl));
  $('#porSetup').innerHTML = grupo(s.por_setup); $('#porSimbolo').innerHTML = grupo(s.por_simbolo); $('#porDia').innerHTML = grupo(s.por_dia);
}

// ---------- backtest ----------
let gBT;
async function prepararBT() {
  E.estr = await api('/estrategias');
  $('#bEst').innerHTML = Object.entries(E.estr).map(([k, v]) => `<option value="${k}">${v.descripcion}</option>`).join('');
  $('#bEst').onchange = paramsBT; paramsBT();
}
function paramsBT() { const e = E.estr[$('#bEst').value]; $('#bDesc').textContent = '';
  $('#bParams').innerHTML = Object.entries(e.params).map(([k, v]) => `<label>${k} <input data-param="${k}" value="${v}"></label>`).join('');
  $('#bRangos').innerHTML = Object.entries(e.params).filter(([, v]) => typeof v === 'number').map(([k, v]) => `<label>${k} (valores separados por coma) <input data-rango="${k}" value="${[v * 0.5, v, v * 1.5, v * 2].map(x => Number.isInteger(v) ? Math.round(x) : +x.toFixed(2)).join(',')}"></label>`).join('') || '<span class="tenue">Sin parámetros.</span>'; }
function cuerpoBT() { const params = {}; $$('[data-param]').forEach(i => params[i.dataset.param] = isNaN(+i.value) ? i.value : +i.value);
  return { simbolo: $('#bSim').value.trim(), intervalo: $('#bInt').value, rango: $('#bRango').value, estrategia: $('#bEst').value, params, capital: +$('#bCap').value,
    comision_pct: +$('#bCom').value, deslizamiento_pct: +$('#bSlip').value, tamano: $('#bTam').value, fraccion: +$('#bFrac').value, riesgo_pct: +$('#bRiesgo').value,
    stop_atr: +$('#bStop').value, objetivo_atr: +$('#bObj').value, cortos: $('#bCortos').checked }; }
$('#btnBT').onclick = async () => { msg('#bMsg', 'calculando…', true);
  try { const r = await api('/backtest', 'POST', cuerpoBT()); msg('#bMsg', `${r.velas} velas · ${fecha(r.desde)} → ${fecha(r.hasta)}`, true);
    const t = (a, b) => `<div class="tile"><small>${a}</small><b>${b}</b></div>`;
    $('#btKpis').innerHTML = [t('Retorno', sg(r.retorno_pct) + '%'), t('Comprar y mantener', sg(r.comprar_mantener_pct) + '%'), t('CAGR', sg(r.cagr_pct) + '%'), t('Drawdown máx.', n(r.max_drawdown_pct) + '%'),
      t('Sharpe', n(r.sharpe)), t('Sortino', n(r.sortino)), t('Calmar', n(r.calmar)), t('Trades', r.trades), t('Acierto', r.trades ? n(r.tasa_acierto, 1) + '%' : '—'),
      t('Factor beneficio', n(r.factor_beneficio)), t('Esperanza', sg(r.esperanza)), t('En mercado', n(r.tiempo_en_mercado_pct, 0) + '%')].join('');
    if (!gBT) { gBT = LW.createChart($('#chartBT'), { ...temaG, autoSize: true }); gBT.a = gBT.addLineSeries({ color: '#3b82f6', lineWidth: 2 }); gBT.b = gBT.addLineSeries({ color: '#64748b', lineWidth: 1 }); }
    const dedup = arr => { const s = new Set(); return arr.filter(p => !s.has(p.t) && s.add(p.t)).map(p => ({ time: p.t, value: p.v })); };
    gBT.a.setData(dedup(r.curva)); gBT.b.setData(r.referencia ? dedup(r.referencia.curva) : []); gBT.timeScale().fitContent();
    const mc = r.montecarlo; $('#btMC').innerHTML = mc ? `Drawdown mediano <b>${mc.dd_mediano}%</b> · en el 5 % peor de los casos <b>${mc.dd_95}%</b> · peor <b>${mc.dd_peor}%</b><br>
      Permutación del orden de los mismos trades: mide drawdown; no estima la probabilidad de beneficio futuro.
      ${r.bootstrap ? `<br>Remuestreo por bloques: capital final percentiles 5–95 <b>${n(r.bootstrap.final_5)}–${n(r.bootstrap.final_95)}</b>. Condicional al historial, sin garantía de rentabilidad.` : ''}` : '<span class="tenue">Pocos trades para Monte Carlo.</span>';
    $('#btTrades').innerHTML = tabla([['Entrada', x => fecha(x.entrada_t)], ['Salida', x => fecha(x.salida_t)], ['Lado', x => x.lado], ['Precio ent.', x => np(x.entrada)], ['Precio sal.', x => np(x.salida)], ['P&L', x => sg(x.pnl)], ['R', x => x.r != null ? sg(x.r) : '—'], ['Motivo', x => x.motivo]], r.lista_trades.slice().reverse());
  } catch (e) { msg('#bMsg', e.message); } };
$('#btnOpt').onclick = async () => { const rangos = {}; $$('[data-rango]').forEach(i => rangos[i.dataset.rango] = i.value.split(',').map(Number).filter(x => !isNaN(x)));
  $('#btOpt').textContent = 'probando combinaciones…';
  try { const r = await api('/optimizar', 'POST', { ...cuerpoBT(), rangos });
    $('#btOpt').innerHTML = `${r.probadas} combinaciones · corte ${fecha(r.corte_t)}${r.aviso ? `<br><b class="ambar">⚠ ${r.aviso}</b>` : ''}` + tabla([['Parámetros', x => esc(JSON.stringify(x.params))], ['Entreno ret/Sharpe', x => `${sg(x.entreno.retorno_pct)}% / ${n(x.entreno.sharpe)}`], ['Prueba ret/Sharpe', x => `${sg(x.prueba.retorno_pct)}% / ${n(x.prueba.sharpe)}`], ['', x => `<button data-usar='${esc(JSON.stringify(x.params))}'>Usar</button>`]], r.top);
    $$('[data-usar]').forEach(b => b.onclick = () => { const p = JSON.parse(b.dataset.usar); Object.entries(p).forEach(([k, v]) => { const i = $(`[data-param="${k}"]`); if (i) i.value = v; }); $('#btnBT').click(); });
  } catch (e) { $('#btOpt').textContent = e.message; } };

// ---------- práctica ----------
async function cargarPracticas() { const l = E.cuentas.filter(c => c.tipo === 'replay');
  $('#listaPracticas').innerHTML = tabla([['Nombre', c => esc(c.nombre)], ['Símbolo', c => c.replay.simbolo + ' · ' + c.replay.intervalo], ['Progreso', c => `${c.replay.idx + 1}/${c.replay.total}`], ['Equity', c => n(c.equity)], ['P&L', c => sg(c.pnl_total_pct) + '%'], ['', c => `<button data-abrir-practica="${c.id}">Abrir</button>`]], l, 'Aún no hay sesiones de práctica.'); }
$('#btnPractica').onclick = async () => { msg('#pMsg', 'cargando el pasado…', true);
  try { const s = $('#pSim').value.trim().toUpperCase(); const c = await api('/cuentas', 'POST', { nombre: `Práctica ${s} ${new Date().toLocaleDateString('es-ES')}`, capital: +$('#pCap').value,
      replay: { simbolo: s, intervalo: $('#pInt').value, rango: $('#pRango').value, calentamiento: +$('#pCal').value, azar: $('#pAzar').checked } });
    await cargarCuentas(); $('#selCuenta').value = c.id; await cambiarCuenta(); irA('mesa'); } catch (e) { msg('#pMsg', e.message); } };
async function avanzar(p) { try { const r = await api(`/cuentas/${E.cuenta.id}/replay/avanzar`, 'POST', { pasos: p }); dibujar(r.velas);
  $('#rInfo').textContent = `${r.simbolo} · ${r.intervalo} · vela ${r.idx + 1}/${r.total} · ${fecha(r.velas[r.velas.length - 1].t)}${r.fin ? ' · FIN' : ''}`; $('#gPrecio').textContent = np(r.velas[r.velas.length - 1].c);
  if (r.ejecuciones) toast(`${r.ejecuciones} orden(es) ejecutada(s)`); await cargarCuenta(); if (r.fin) pararAuto(); } catch (e) { toast(e.message); pararAuto(); } }
$$('[data-paso]').forEach(b => b.onclick = () => avanzar(+b.dataset.paso));
function pararAuto() { clearInterval(E.auto); E.auto = null; $('#rAuto').classList.remove('on'); }
$('#rAuto').onclick = () => { if (E.auto) return pararAuto(); $('#rAuto').classList.add('on'); E.auto = setInterval(() => avanzar(1), +$('#rVel').value); };

// ---------- escáner y alertas ----------
$('#btnEscanear').onclick = async () => { $('#tablaEscaner').textContent = 'escaneando…';
  try { const f = await api('/escaner'); $('#tablaEscaner').innerHTML = tabla([['Símbolo', x => `<a href="#" data-ver="${x.simbolo}">${x.simbolo}</a>`], ['Precio', x => np(x.precio)], ['Día', x => x.cambio_pct != null ? sg(x.cambio_pct) + '%' : '—'],
    ['1 mes', x => x.rend_1m != null ? sg(x.rend_1m) + '%' : '—'], ['RSI', x => n(x.rsi14, 0)], ['ATR%', x => n(x.atr_pct)], ['Rango 52s', x => x.rango_52s_pct != null ? x.rango_52s_pct + '%' : '—'],
    ['Sesgo', x => `<span class="${x.sesgo_tecnico === 'alcista' ? 'verde' : x.sesgo_tecnico === 'bajista' ? 'rojo' : ''}">${x.sesgo_tecnico || esc(x.error)}</span>`], ['Señales', x => `<span class="chico">${(x.senales || []).map(esc).join(' · ')}</span>`]], f); } catch (e) { $('#tablaEscaner').textContent = e.message; } };
async function cargarAlertas() { const a = await api('/alertas'); $('#tablaAlertas').innerHTML = tabla([['Símbolo', x => x.simbolo], ['Condición', x => ({ mayor: '≥', menor: '≤', cambio_pct: '|Δ día| ≥ %' })[x.condicion] + ' ' + np(x.valor)],
  ['Nota', x => esc(x.nota || '')], ['Estado', x => x.activa ? '<span class="ambar">vigilando</span>' : `disparada ${fecha(x.disparada)} a ${np(x.precio_disparo)}`], ['', x => `<button data-borrar-alerta="${x.id}">✕</button>`]], a, 'Sin alertas.'); }
$('#btnAlerta').onclick = async () => { try { await api('/alertas', 'POST', { simbolo: $('#aSim').value.trim(), condicion: $('#aCond').value, valor: +$('#aVal').value, nota: $('#aNota').value }); cargarAlertas(); } catch (e) { toast(e.message); } };
async function cargarAvisos(abrir) { try { const a = await api('/avisos' + (abrir ? '' : '?nuevos=1&marcar=0'));
  if (!abrir) { $('#nAvisos').textContent = a.length || ''; return; }
  $('#avisosPanel').innerHTML = a.map(x => `<div class="aviso">${esc(x.condicion === 'aviso' ? x.nota : `🔔 ${x.simbolo} ${x.condicion} ${np(x.valor)} → ${np(x.precio_disparo)} ${x.nota || ''}`)}<small>${fecha(x.disparada)}</small></div>`).join('') || '<div class="tenue">Sin avisos.</div>';
  $('#nAvisos').textContent = ''; } catch { } }
$('#btnAvisos').onclick = () => { const p = $('#avisosPanel'); p.classList.toggle('oculto'); if (!p.classList.contains('oculto')) cargarAvisos(true); };

// ---------- ajustes ----------
const CFG = [['riesgo_max_pct', 'Riesgo máximo por operación (%)'], ['perdida_diaria_max_pct', 'Pérdida diaria máxima (%) → bloquea el día'], ['max_posiciones', 'Máximo de posiciones abiertas'],
  ['apalancamiento', 'Apalancamiento (1 = sin margen)'], ['comision_fija', 'Comisión fija por orden'], ['comision_pct', 'Comisión % por orden'], ['deslizamiento_pct', 'Deslizamiento simulado (%)'],
  ['exigir_stop', 'Exigir stop-loss en cada entrada'], ['riesgo_estricto', 'Rechazar órdenes que superen el riesgo'], ['permitir_cortos', 'Permitir cortos']];
function pintarCfg() { const c = E.cuenta.config; $('#ajCuentaNombre').textContent = '· ' + E.cuenta.nombre; $('#formCfg').innerHTML = CFG.map(([k, t]) => typeof c[k] === 'boolean' ? `<label class="check"><input type="checkbox" data-cfg="${k}" ${c[k] ? 'checked' : ''}> ${t}</label>` : `<label>${t} <input data-cfg="${k}" type="number" step="any" value="${c[k]}"></label>`).join(''); }
$('#btnCfg').onclick = async () => { const b = {}; $$('[data-cfg]').forEach(i => b[i.dataset.cfg] = i.type === 'checkbox' ? i.checked : +i.value);
  try { await api(`/cuentas/${E.cuenta.id}`, 'PATCH', b); await cargarCuenta(); msg('#cfgMsg', 'Reglas guardadas.', true); } catch (e) { msg('#cfgMsg', e.message); } };
$('#btnNueva').onclick = async () => { try { const c = await api('/cuentas', 'POST', { nombre: $('#nNombre').value, capital: +$('#nCap').value, moneda: $('#nMon').value }); await cargarCuentas(); $('#selCuenta').value = c.id; await cambiarCuenta(); msg('#ajMsg', 'Cuenta creada.', true); } catch (e) { msg('#ajMsg', e.message); } };
$('#btnReset').onclick = async () => { if (!confirm('¿Reiniciar la cuenta? Se borran posiciones, órdenes, diario y curva.')) return; await api(`/cuentas/${E.cuenta.id}/reiniciar`, 'POST'); await cambiarCuenta(); msg('#ajMsg', 'Cuenta reiniciada.', true); };
$('#btnBorrar').onclick = async () => { if (E.cuentas.length < 2) return msg('#ajMsg', 'Es la única cuenta.'); if (!confirm('¿Eliminar esta cuenta para siempre?')) return;
  await api(`/cuentas/${E.cuenta.id}`, 'DELETE'); localStorage.removeItem('dmn.cuenta'); await cargarCuentas(); await cambiarCuenta(); msg('#ajMsg', 'Cuenta eliminada.', true); };
$('#btnWebhook').onclick = async () => { try { await api('/ajustes', 'POST', { webhook: $('#wUrl').value.trim() }); msg('#avMsg', 'Webhook guardado: los avisos (stops, alertas, bloqueos) llegarán ahí.', true); } catch (e) { msg('#avMsg', e.message); } };

// ---------- enjambre ----------
async function cargarAgente() {
  try {
    const [m, inf] = await Promise.all([api('/enjambre/mapa'), api('/enjambre/informe')]);
    const loc = m.nodos.find(x => x.local) || m.nodos[0], k = loc.kpis, t = (x, y) => `<div class="tile"><small>${x}</small><b>${y}</b></div>`;
    $('#agEstado').innerHTML = k.activo ? `<span class="verde">● ${k.agentes} agentes operando</span> · ciclo ${k.tick_s}s` : '<span class="tenue">○ parado</span>';
    if (k.esperando_cierre) $('#agEstado').innerHTML += ` · <span class="ambar">${n(k.esperando_cierre, 0)} esperando cierre anterior; conservan capital y se incorporan cuando quede confirmado</span>`;
    $('#agKpis').innerHTML = [t('Agentes (todos los nodos)', m.total.agentes), t('Generación', k.generacion), t('Operaciones', m.total.trades), t('Acierto', k.trades ? n(k.ganados / k.trades * 100, 0) + '%' : '—'),
      t('R medio', k.r_medio != null ? sg(k.r_medio) : '—'), t('Quiebras', m.total.quiebras), t('Metas 🏆', m.total.metas), t('Mejor capital', n(k.max_equity, 2) + ' $')].join('');
    const cajas = $('#agPorNodo'), vistos = [...cajas.querySelectorAll('input')].map(i => i.dataset.nodo);
    if (m.nodos.map(x => x.nodo).join() !== vistos.join()) cajas.innerHTML = m.nodos.map(x => `<label>${esc(x.nodo)}${x.local ? ' (central)' : ''} <input type="number" min="0" max="2000" data-nodo="${esc(x.nodo)}" value="${(x.kpis || {}).activo ? (x.kpis.n || (x.agentes || []).length) : 0}"></label>`).join('');
    $('#agNodos').textContent = m.nodos.map(x => `${x.nodo}: ${(x.agentes || []).length}${x.kpis?.esperando_cierre ? ' + ' + x.kpis.esperando_cierre + ' esperando cierre' : ''}${x.local ? ' (aquí)' : x.en_linea ? ' (en línea)' : ' (desconectado)'}`).join(' · ');
    const todos = m.nodos.flatMap(x => (x.agentes || []).map(a => ({ ...a, nodo: x.nodo }))).sort((a, b) => (b.r_medio ?? -9) - (a.r_medio ?? -9));
    $('#agTabla').innerHTML = tabla([['Agente', a => `<b>${esc(a.nombre)}</b>`], ['Nodo', a => a.nodo], ['Gen', a => a.generacion], ['Sistema', a => `${a.estrategia} · ${a.marco}${a.tendencia ? ' · tend' : ''} · ${a.riesgo_pct}%`],
      ['Capital', a => n(a.equity, 2)], ['Máximo', a => n(a.max_equity, 2)], ['Trades', a => `${a.trades} (${a.ganados}✔)`], ['R medio', a => a.r_medio != null ? sg(a.r_medio) : '—'],
      ['Intentos', a => a.intentos], ['🏆', a => a.metas || ''], ['Estado', a => esc(a.estado)], ['Último', a => `<span class="chico">${esc(a.ultimo || '')}</span>`]], todos);
    $('#agInforme').textContent = inf.texto;
    guardarCerebro('enjambre', m.lecciones || [], m.log || []);
  } catch (e) { msg('#agMsg', e.message); }
}
$('#agIniciar').onclick = async () => { try {
  const hecho = [];
  for (const i of $('#agPorNodo').querySelectorAll('input')) {
    const nodo = i.dataset.nodo, k = +i.value;
    if (k > 0) await api('/enjambre/iniciar', 'POST', { nodo, modo: 'simulado', agentes: k, capital: +$('#agCap').value, meta: +$('#agMeta').value, hasta: $('#agHasta').value || null, mercados: $('#agMerc').value.split(','), duracionMin: +$('#agDuracion').value, perdidaMaxPct: +$('#agPerdida').value });
    else await api('/enjambre/parar', 'POST', { nodo });
    hecho.push(`${nodo}: ${k || 'parado'}`);
  }
  msg('#agMsg', hecho.join(' · ') + ' (los otros equipos lo aplican en ≤ 10 s)', true); setTimeout(cargarAgente, 1500); } catch (e) { msg('#agMsg', e.message); } };
$('#agParar').onclick = async () => { try { for (const i of $('#agPorNodo').querySelectorAll('input')) { await api('/enjambre/parar', 'POST', { nodo: i.dataset.nodo }); i.value = 0; }
  msg('#agMsg', 'Parada solicitada a todos los nodos. Comprueba su estado: una orden remota pendiente no confirma la parada.', true); cargarAgente(); } catch (e) { msg('#agMsg', e.message); } };
setInterval(() => { if (!document.hidden && E.pag === 'lab-enjambre') cargarAgente(); }, 10000);

// ---------- cuenta real (GMGN) ----------
const hace_ = s => s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : s < 172800 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
async function cargarReal() {
  let r;
  try { r = await api('/real'); } catch (e) { $('#reEstado').textContent = e.message; return; }
  if (!r.ts) { $('#reEstado').innerHTML = '<span class="tenue">○ sin datos: enciende el servidor del PC (servidor-pc.bat)</span>'; return; }
  const viva = r.edad < 60;
  $('#reEstado').innerHTML = `${viva ? '<span class="verde">● en vivo</span>' : '<span class="ambar">● sin actualizar</span>'} · hace ${hace_(r.edad)}${r.vigilando_aqui ? ' · vigilando desde este equipo' : r.recibido ? ' · desde el PC' : ''}${r.error && r.error_ts > r.ts ? ` · <span class="rojo">${esc(r.error)}</span>` : ''}`;
  $('#reWallet').innerHTML = r.wallet ? `<a href="https://solscan.io/account/${esc(r.wallet)}" target="_blank" rel="noopener">${esc(r.wallet.slice(0, 6))}…${esc(r.wallet.slice(-4))}</a>` : '';
  const P = r.posiciones || [], vPos = P.reduce((a, p) => a + (p.valor || 0), 0), pnl = P.reduce((a, p) => a + (p.pnl || 0), 0), solUsd = r.sol_usd ? r.sol * r.sol_usd : null;
  const t = (x, y) => `<div class="tile"><small>${x}</small><b>${y}</b></div>`;
  $('#reKpis').innerHTML = [t('SOL en la wallet', `${n(r.sol, 4)} <span class="tenue chico">≈ ${n(solUsd)} $</span>`), t('En posiciones', n(vPos) + ' $'), t('PnL abierto', sg(pnl, 3) + ' $'),
    t('Total', solUsd != null ? n(solUsd + vPos) + ' $' : '—'), t('Posiciones', P.length), t('Precio SOL', r.sol_usd ? n(r.sol_usd) + ' $' : '—')].join('');
  const ahora = Date.now() / 1000;
  $('#reBot').innerHTML = P.length ? P.map(p => `<div class="re-bot">Vigilando <b>${esc(p.simbolo)}</b>: va ${sg(p.pnl_pct, 1)} % (${np(p.precio)} $).
    ${p.tp ? `Vende el ${n(p.condiciones.find(c => c.tipo.startsWith('profit'))?.venta ?? 100, 0)} % si sube a <b class="verde">${np(p.tp)} $</b> (${sg(p.dist_tp, 1)} % desde aquí)` : 'Sin take-profit puesto'}${p.sl ? ` o si baja a <b class="rojo">${np(p.sl)} $</b> (${sg(p.dist_sl, 1)} %).` : '. <span class="rojo">Sin stop-loss puesto.</span>'}
    ${p.caduca ? `<span class="tenue chico"> Las órdenes caducan en ${hace_(p.caduca - ahora)}.</span>` : ''}</div>`).join('')
    : '';
  const pend = (r.estrategias || []).filter(e => ['open', 'prepare'].includes(e.estado) && !P.some(p => p.token === e.token) && e.condiciones.some(c => c.tipo.startsWith('buy') && c.estado === 'check'));
  $('#reBot').innerHTML += pend.map(e => { const b = e.condiciones.find(c => c.tipo.startsWith('buy')), tp = e.condiciones.find(c => c.tipo.startsWith('profit')), sl = e.condiciones.find(c => c.tipo.startsWith('loss'));
    return `<div class="re-bot" style="border-left-color:var(--ambar)">Esperando para comprar <b>${esc(e.simbolo || e.token.slice(0, 6))}</b>${e.invertir_sol ? ` con ${n(e.invertir_sol, 3)} SOL` : ''}: compra si baja a <b>${np(b.precio)} $</b>.
      Después vende si sube un <span class="verde">${tp ? n(tp.pct, 0) : '—'} %</span> o si baja un <span class="rojo">${sl ? n(sl.pct, 0) : '—'} %</span>.${e.caduca ? `<span class="tenue chico"> Caduca en ${hace_(e.caduca - ahora)}.</span>` : ''}</div>`; }).join('');
  if (!P.length && !pend.length) $('#reBot').innerHTML = `<div class="tenue">El bot no vigila nada ahora. ${r.sol ? `Hay ${n(r.sol, 4)} SOL libres para la próxima operación.` : ''}</div>`;
  velasReal(r, P, pend);
  $('#rePos').innerHTML = P.length ? P.map(p => {
    const lo = Math.min(p.sl || p.precio, p.precio, p.entrada || p.precio) * 0.99, hi = Math.max(p.tp || p.precio, p.precio, p.entrada || p.precio) * 1.01, x = v => ((v - lo) / (hi - lo) * 100).toFixed(1) + '%';
    return `<div class="re-pos"><div>${p.logo ? `<img src="${esc(p.logo)}" alt="">` : ''}<b>${esc(p.simbolo)}</b> <span class="tenue chico">${esc(p.nombre || '')}</span>
      <div class="chico">${n(p.cantidad, 2)} · ${n(p.valor, 3)} $ · coste ${n(p.coste, 3)} $</div><div class="grande">${sg(p.pnl_pct, 2)} %</div>
      <div class="tenue chico">desde ${fecha(p.desde)} · liquidez ${n(p.liquidez / 1000, 0)} k$ · <a href="https://gmgn.ai/sol/token/${esc(p.token)}" target="_blank" rel="noopener">GMGN</a></div></div>
      <div class="re-barra">${p.sl ? `<span class="ab" style="left:${x(p.sl)}">SL ${np(p.sl)}</span>` : ''}${p.tp ? `<span class="ab" style="left:${x(p.tp)}">TP ${np(p.tp)}</span>` : ''}${p.entrada ? `<i class="ent" style="left:${x(p.entrada)}"></i><span class="ar" style="left:${x(p.entrada)}">entrada ${np(p.entrada)}</span>` : ''}<i class="act" style="left:${x(p.precio)}" title="ahora"></i></div></div>`; }).join('') : '<div class="tenue">Nada abierto.</div>';
  E.real = r;
  guardarCerebro('eventos', [], (r.eventos || []).map(([ts, txt]) => [ts, txt]));
  $('#reAct').innerHTML = tabla([['Hora', a => fecha(a.ts)], ['Tipo', a => a.tipo === 'buy' ? '<span class="verde">compra</span>' : a.tipo === 'sell' ? '<span class="rojo">venta</span>' : esc(a.tipo)],
    ['Token', a => esc(a.simbolo) + (a.ruta && a.ruta.length ? ` <span class="tenue chico">vía ${a.ruta.map(esc).join(' → ')}</span>` : '')], ['Cantidad', a => n(a.cantidad, 2)], ['$', a => n(a.usd, 3)], ['Precio', a => np(a.precio)], ['Costes $', a => n((a.gas_usd || 0) + (a.dex_usd || 0), 3)],
    ['Tx', a => `<a href="https://solscan.io/tx/${esc(a.tx)}" target="_blank" rel="noopener">${esc((a.tx || '').slice(0, 8))}…</a>`]], r.actividad || [], 'Sin actividad.');
}
// gráfico de velas del token que se está operando (posición abierta, compra pendiente o, si no hay, el último operado)
const GR = { tok: null };
function velasReal(r, P, pend) {
  const V = r.velas || {}, toks = Object.keys(V);
  if (!toks.length) { $('#reVTok').innerHTML = '<span class="tenue chico">sin velas todavía</span>'; return; }
  if (!toks.includes(GR.tok)) GR.tok = toks[0];
  const nombre = tk => (P.find(p => p.token === tk) || {}).simbolo || (pend.find(e => e.token === tk) || {}).simbolo || ((r.actividad || []).find(a => a.token === tk) || {}).simbolo || tk.slice(0, 6);
  $('#reVTok').innerHTML = toks.map(tk => `<button class="chico${tk === GR.tok ? ' activo' : ''}" data-tk="${esc(tk)}">${esc(nombre(tk))}</button>`).join(' ');
  $('#reVTok').querySelectorAll('button').forEach(b => b.onclick = () => { GR.tok = b.dataset.tk; GR.ult = null; velasReal(r, P, pend); });
  if (!GR.c) {
    GR.c = LW.createChart($('#reChart'), { ...temaG, autoSize: true });
    GR.s = GR.c.addCandlestickSeries({ upColor: '#22c55e', downColor: '#ef4444', borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444' });
    GR.vol = GR.c.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: 'vol', color: '#334155' });
    GR.c.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    GR.lineas = [];
  }
  const v = V[GR.tok], ult = v[v.length - 1], d = Math.min(12, pd(ult.c) + 1);
  GR.s.applyOptions({ priceFormat: { type: 'price', precision: d, minMove: Math.pow(10, -d) } });
  GR.s.setData(v.map(b => ({ time: b.t, open: b.o, high: b.h, low: b.l, close: b.c })));
  GR.vol.setData(v.map(b => ({ time: b.t, value: b.v, color: b.c >= b.o ? '#22c55e44' : '#ef444444' })));
  GR.lineas.forEach(l => GR.s.removePriceLine(l)); GR.lineas = [];
  const linea = (price, color, title, lineStyle = 2) => price && GR.lineas.push(GR.s.createPriceLine({ price, color, title, lineStyle, lineWidth: 1, axisLabelVisible: true }));
  const p = P.find(x => x.token === GR.tok), e = pend.find(x => x.token === GR.tok);
  if (p) { linea(p.entrada, '#94a3b8', 'entrada', 0); linea(p.tp, '#22c55e', 'TP'); linea(p.sl, '#ef4444', 'SL'); }
  if (e) { const c = t => e.condiciones.find(x => x.tipo.startsWith(t)); linea(c('buy')?.precio, '#f59e0b', 'compra si baja a'); }
  const t0 = v[0].t;
  GR.s.setMarkers((r.actividad || []).filter(a => a.token === GR.tok && a.ts >= t0).sort((a, b) => a.ts - b.ts).map(a => ({ time: a.ts - a.ts % 60,
    position: a.tipo === 'buy' ? 'belowBar' : 'aboveBar', color: a.tipo === 'buy' ? '#22c55e' : '#ef4444', shape: a.tipo === 'buy' ? 'arrowUp' : 'arrowDown',
    text: `${a.tipo === 'buy' ? 'compra' : 'venta'} ${n(a.usd, 2)} $` })));
  if (GR.ult !== GR.tok) { GR.c.timeScale().fitContent(); GR.ult = GR.tok; }
}
// ---------- piloto automático real ----------
function pintarConciencia(C) {
  $('#piCon').innerHTML = C ? `<div class="chico">Nivel <b style="font-size:1.3em">${n(C.nivel, 0)}</b>/100 · ${C.velas_total} muestras aprendidas · grafo: ${Object.entries(C.nodos || {}).map(([k, v]) => `${v} ${esc(k)}`).join(', ')}, ${C.aristas} enlaces${C.apolo_pendientes ? ` · <span class="tenue">${C.apolo_pendientes} hitos esperando a APOLO</span>` : ''}</div>`
    + lineaConciencia(C.linea || [], 'Memecoins') + lineaConciencia(C.linea_futuros || [], 'Futuros')
    + `<div class="tenue chico" style="margin-top:4px">${(C.eventos || []).map(e => `${fecha(e.ts)} · ${{ ronda: '🔁', record: '🏅', promocion: '💰', real: '💸', diario: '📔' }[e.tipo] || '•'} ${esc(e.titulo)}${e.apolo && e.apolo !== 'pendiente' ? ' 🧩' : ''}`).join('<br>')}</div>`
    : '<div class="tenue chico">Solo en el PC.</div>';
}
async function cargarConciencia() { try { pintarConciencia((await api('/piloto')).conciencia); } catch (e) { $('#piCon').innerHTML = `<div class="tenue chico">${esc(e.message)}</div>`; } }
setInterval(() => { if (!document.hidden && E.pag === 'cerebro-conciencia') cargarConciencia(); }, 30000);
async function cargarPiloto() {
  let p;
  try { p = await api('/piloto'); } catch (e) { $('#piEstado').textContent = e.message; return; }
  const c = p.cfg || {}, ahora = Date.now() / 1000;
  if (document.activeElement?.closest?.('#piPorOp,#piPerd,#piOps,#piUmbral') == null) {
    $('#piPorOp').value = c.por_op_sol; $('#piPerd').value = c.perdida_max_sol; $('#piOps').value = c.max_ops_dia; $('#piUmbral').value = c.umbral; }
  const vivo = p.ts && ahora - p.ts < 120;
  $('#piEstado').innerHTML = `${c.activo ? '<span class="verde">● ACTIVO</span>' : '<span class="tenue">○ parado</span>'}${p.ts ? ` · ${vivo ? 'PC conectado' : '<span class="ambar">PC sin responder</span>'}` : ' · <span class="ambar">esperando al PC</span>'}${p.motivo ? ` · <span class="ambar">${esc(p.motivo)}</span>` : ''}${p.saldo_inicio ? ` · variación de saldo ${sg(((p.saldo ?? p.saldo_inicio) - p.saldo_inicio), 4)} SOL` : ''}${p.error && p.error_ts > (p.ts || 0) - 60 ? ` · <span class="rojo">${esc(p.error)}</span>` : ''}`;
  const supervision = p.supervision;
  if (supervision) $('#piEstado').innerHTML += `<div class="chico" style="margin-top:6px"><b>${esc(({apagado:'Piloto apagado',validando:'Validando estrategias',esperando_senal:'Esperando señal',protegida:'Protecciones verificadas',conciliacion:'Conciliación pendiente',saliendo:'Confirmando salida',requiere_revision:'Requiere revisión'})[supervision.estado] || supervision.estado)}</b> · ${esc(supervision.motivo)}</div>`;
  const o = p.orden;
  $('#piOrden').innerHTML = o ? `<div class="re-bot" style="border-left-color:var(--ambar)">${o.entro ? `Dentro de <b>${esc(o.simbolo)}</b> desde ${fecha(o.entro)} a ${np(o.precio_entrada)} $` : `Esperando comprar <b>${esc(o.simbolo)}</b> a ${np(o.compra)} $ (${o.estrategia ? esc(o.estrategia) : o.pts + ' pts'})`}${o.estrategia ? ` · 🏭 ${esc(o.estrategia)}` : ''} con ${n(o.sol, 3)} SOL · TP +${n(o.tp, 0)} % / SL −${n(o.sl, 0)} %${o.pnl_pct != null ? ` · va ${sg(o.pnl_pct, 1)} %` : ''}${o.stop_dyn ? ` · 🔒 stop subido a ${np(o.stop_dyn)} $` : ''}${o.salida ? ` · 🚪 saliendo: ${esc(o.salida)}` : ''}${o.ia && o.ia.motivo ? `<div class="chico">🧠 IA (${fecha(o.ia.ts)}): <b>${esc(o.ia.accion)}</b> ${n((o.ia.confianza || 0) * 100, 0)} % — ${esc(o.ia.motivo)}</div>` : ''}</div>` : '<div class="tenue chico">Sin orden del piloto ahora.</div>';
  if (o && o.modo === 'mercado' && !o.proteccion_verificada) $('#piOrden').innerHTML += '<div class="tenue chico">Los niveles TP/SL previstos todavía no son protecciones verificadas.</div>';
  if (supervision && supervision.incidencias?.length) $('#piOrden').innerHTML += `<div class="re-bot" style="border-left-color:var(--rojo)"><b>Incidencias pendientes</b>${supervision.incidencias.map(x => `<div class="chico">${esc(x.motivo)}</div>`).join('')}<div class="tenue chico">Resolver la incidencia no activa automáticamente el piloto.</div></div>`;
  if (supervision && (supervision.requiere_revision || p.intencion_entrada)) $('#piOrden').innerHTML += `<button id="piConciliado" class="ambar" style="margin-top:6px">✅ Ya lo revisé en GMGN</button>`;
  const bc = $('#piConciliado');
  if (bc) bc.onclick = () => { if (confirm('¿Revisaste en GMGN las órdenes y la wallet?\nSe resuelven las incidencias y se olvida la entrada sin confirmar (si la hubiera). Una orden que siga abierta en GMGN y no esté en el panel tendrás que cerrarla tú.')) guardarPiloto({ conciliado: Math.floor(Date.now() / 1000) }); };
  $('#piScan').innerHTML = ((p.ultimo_scan || {}).top || []).some(x => x.estrategia) ? tabla([['Token', x => esc(x.simbolo)], ['Estrategia', x => esc(x.estrategia)], ['TP', x => '+' + n(x.tp, 0) + ' %'], ['SL', x => '−' + n(x.sl, 0) + ' %'], ['Máx.', x => x.hold * 5 + ' min']], p.ultimo_scan.top, '') : tabla([['Token', x => esc(x.simbolo)], ['Pts', x => x.pts], ['Desde máx. 4h', x => sg(x.ret, 1) + ' %'], ['24h', x => sg(x.t24, 1) + ' %'], ['RSI15', x => x.rsi], ['ATR15', x => n(x.atr, 1) + ' %'], ['Smart $', x => n(x.smart, 0)]],
    (p.ultimo_scan || {}).top || [], 'Aún no ha escaneado.') + (p.ultimo_scan ? `<div class="tenue chico">${fecha(p.ultimo_scan.ts)}</div>` : '');
  $('#piOps2').innerHTML = tabla([['Fecha', x => fecha(x.creada)], ['Token', x => esc(x.simbolo)], ['Resultado', x => ({ tp: '<span class="verde">TP</span>', sl: '<span class="rojo">SL</span>', no_entro: 'no entró', fallo_compra: '<span class="ambar">fallo compra</span>' })[x.resultado] || esc(x.resultado)], ['Neto SOL', x => x.pnl_confirmado ? sg(x.pnl_sol, 4) : 'pendiente'], ['Flujo de recibos SOL', x => sg(x.contabilidad?.flujo_sol_identificado, 6)], ['Recibos', x => x.contabilidad ? `${x.contabilidad.recibos_verificados} verificados / ${x.contabilidad.recibos_pendientes} pendientes${x.contabilidad.firmas_faltantes?.length ? "; faltan firmas: " + x.contabilidad.firmas_faltantes.map(esc).join(", ") : ""}` : '—']],
    (p.ops || []).slice().reverse().slice(0, 20), 'Sin operaciones todavía.');
  if ((p.ops || []).length) $('#piOps2').innerHTML += '<div class="tenue chico">El flujo de recibos muestra movimientos identificados. El neto permanece pendiente mientras falten costes o recibos; un depósito no se cuenta como ganancia.</div>';
  const L = p.lab;
  const labTabla = (X, nombre) => `<div class="chico" style="margin-top:8px"><b>${nombre}</b></div>` + tabla([['Modelo', x => esc(x.modelo)], ['Preguntas', x => x.preguntas], ['Aprueba', x => n(x.aprueba_pct, 0) + ' %'], ['Acierto', x => n(x.acierto_pct, 0) + ' %'],
    ['Resultado siguiéndolo', x => sg(x.resultado_siguiendolo_pct, 1) + ' %'], ['Por entrada', x => sg(x.medio_por_entrada_pct, 1) + ' %'], ['Tarda', x => n(x.ms_medio / 1000, 1) + ' s'], ['Errores', x => x.errores]],
    X.modelos || [], 'Aún sin resultados: cada caso se evalúa 1 h después de preguntar.') + `<div class="tenue chico">Referencia (entrar en todo): ${sg(X.referencia_entrar_en_todo_pct, 1)} % en ${X.casos} casos${X.ganadoras_pct != null ? ` (${X.ganadoras_pct} % ganadoras)` : ''} · pendientes ${X.pendientes}</div>`;
  $('#piLab').innerHTML = L ? labTabla(L, '📉 Compra en retroceso') + (L.smart ? labTabla(L.smart, '🐋 Smart money (moneda asentada)') : '')
    + (L.smart_nuevo ? labTabla(L.smart_nuevo, '🎯 Smart money (lanzamiento recién salido)') : '')
    + (L.smart && (L.smart.senales || []).length ? `<div class="tenue chico">Últimas señales: ${L.smart.senales.slice(0, 8).map(x => `${esc(x.simbolo)} (${x.pts} wallets, +${x.neto} $, ${x.estrategia === 'smart' ? 'asentada' : 'nueva'})`).join(' · ')}</div>` : '')
    + `<div class="tenue chico">Modelos: ${(L.instalados || []).map(esc).join(', ')}</div>` : '<div class="tenue chico">Solo en el PC.</div>';
  const F = p.fabrica, est = x => x && x.n ? `${x.n} · ${sg(x.medio, 1)} % · ${x.gana_pct} % gana` : '—';
  $('#piFab').innerHTML = F && F.muestras ? `<div class="tenue chico">${F.muestras.listas} muestras listas (${F.muestras.vivas} en vivo, ${F.muestras.tokens} tokens) · ${F.muestras.pendientes} esperando su resultado · entrena con lo anterior a ${fecha(F.corte)} · selección hasta ${fecha(F.validacion?.seleccion_hasta || F.corte)} · ${esc(F.msg || '')}${F.evo ? ` · última evolución ${fecha(F.evo)}` : ''}</div>`
    + `<div class="re-bot" style="margin-top:6px;border-left-color:var(--verde)">💰 <b>Candidatas que superan los filtros</b> (en vivo ≥ ${F.promo ? F.promo.vivo_n : 10} ops, ≥ +${F.promo ? F.promo.vivo_medio : 1.5} %/op, ≥ ${F.promo ? F.promo.vivo_gana : 35} % ganadoras): ${(F.promovidas || []).length ? F.promovidas.map(x => `<b>${esc(x.nombre)}</b> (${est(x.vivo)})`).join(' · ') : '<span class="tenue">ninguna todavía — el piloto real espera</span>'}</div>`
    + `<div class="chico" style="margin-top:6px"><b>Reglas registradas (propuestas en simulación)</b> (operaciones · media neta con costes · % ganadoras)</div>`
    + tabla([['Estrategia', x => `<b>${esc(x.nombre)}</b><div class="tenue chico">${esc(x.regla)}</div>`], ['Entreno', x => est(x.entreno)], ['Validación', x => est(x.prueba)], ['En vivo', x => est(x.vivo)], ['Adverso', x => est(x.estres?.adverso)], ['Parcial', x => est(x.estres?.parcial)], ['Con demora', x => est(x.estres?.demora)]], F.manuales || [], '')
    + `<div class="chico" style="margin-top:6px"><b>🏆 Salón del generador</b>: propuestas que superan la selección histórica y necesitan validación posterior. «En vivo» sigue siendo simulación con datos posteriores al registro.</div>`
    + tabla([['Id', x => esc(x.id)], ['Regla', x => `<span class="chico">${esc(x.regla)}</span>`], ['Entreno', x => est(x.entreno)], ['Validación', x => est(x.prueba)], ['En vivo', x => est(x.vivo)], ['Adverso', x => est(x.estres?.adverso)], ['Parcial', x => est(x.estres?.parcial)], ['Con demora', x => est(x.estres?.demora)]], F.salon || [], 'Ninguna pasa la validación todavía.')
    : `<div class="tenue chico">${F && F.error ? esc(F.error) : 'Solo en el PC.'}</div>`;
  pintarConciencia(p.conciencia);
  if (F && F.validacion) $('#piFab').innerHTML += `<div class="re-bot"><b>Validación registrada</b><div class="chico">Datos reservados desde ${fecha(F.validacion.reservado_desde)}. Evaluaciones de reglas: ${n(F.validacion.reglas_probadas || 0, 0)}.</div><div class="chico">Para aprobar: al menos ${F.promo.vivo_n} operaciones en ${F.promo.dias} días y ${F.promo.tokens} tokens, sin concentración excesiva, con resultado positivo en las pruebas adversas.</div><div class="tenue chico">Los datos reservados no se usan para seleccionar reglas. ${n(F.muestras?.cuarentena_temporal || 0, 0)} muestras antiguas conservadas y apartadas de la validación temporal.</div></div>`;

  if (p.observatorio) {
    const O = p.observatorio, C = O.costes_acumulados || {}, R = O.conciliacion, V = p.observatorio_servicio;
    $('#piFab').innerHTML += `<div class="re-bot"><b>Observatorio sin dinero real · ${esc(O.dia_utc)} UTC</b>
      <div class="chico">Servicio: ${esc(V?.estado || 'pendiente')} ${V?.ts ? '· ' + fecha(V.ts) : ''}. ${n(O.sombra_pendientes || 0,0)} señales en observación; ${n(O.sombra_completas || 0,0)} completas.</div>
      <div class="chico">Variación media cotizada al vencimiento: ${O.media_cotizada_pct == null ? 'pendiente' : sg(O.media_cotizada_pct,2) + ' %'}. Es una cotización hipotética; no demuestra ejecución, TP/SL ni beneficio neto.</div>
      <div class="chico">Recibos acumulados: ${n(C.recibos || 0,0)}. Comisión de red medida: ${C.comision_red_sol == null ? 'pendiente' : n(C.comision_red_sol,9) + ' SOL'}.</div>
      <div class="tenue chico">Mínimo viable completo: pendiente de todos los costes. ${C.minimo_parcial_solo_red_sol == null ? '' : 'Suelo parcial solo de red: ' + n(C.minimo_parcial_solo_red_sol,6) + ' SOL para un margen hipotético de ' + n(C.margen_hipotetico_pct,1) + ' %; excluye otros costes.'}</div>
      <div class="chico">Conciliación independiente: ${R ? esc(R.estado) + ' · ' + esc(R.motivo) + ' · ' + fecha(R.ts) : 'sin posición local que conciliar'}.</div>
      <div class="chico"><b>Decisiones del día</b>: ${Object.entries(O.decisiones || {}).map(([k,v]) => esc(k) + ': ' + n(v,0)).join(' · ') || 'todavía sin registros'}</div>
      ${(O.motivos || []).map(x => `<div class="tenue chico">${esc(x.motivo)}: ${n(x.n,0)}</div>`).join('')}
      ${(O.ultimas || []).slice(0,8).map(x => `<div class="chico">${fecha(x.ts)} · ${esc(x.regla || '')} ${esc(x.tipo)} · ${esc(x.motivo)}</div>`).join('')}
      <div class="tenue chico">El modo sombra solo consulta. No promueve estrategias ni activa el piloto.</div></div>`;
  }

  const U = p.futuros, FF = U && U.fabrica, usd = x => x && x.n ? `${x.n} · ${sg(x.medio, 1)} $ · ${x.gana_pct} % gana` : '—';
  if (U && U.cfg && document.activeElement?.closest?.('#fuCuenta,#fuFreno,#fuQty') == null) {
    const fc = U.cfg, reales = (U.cuentas || []).filter(x => !x.simulada).map(x => x.nombre);
    if (fc.cuenta_real && !reales.includes(fc.cuenta_real)) reales.push(fc.cuenta_real);
    $('#fuCuenta').innerHTML = '<option value="">— ninguna —</option>' + reales.map(x => `<option ${x === fc.cuenta_real ? 'selected' : ''}>${esc(x)}</option>`).join('');
    $('#fuSim').checked = !!fc.activo; $('#fuFreno').value = fc.real_perdida_dia; $('#fuQty').value = fc.qty;
  }
  const fcfg = (U && U.cfg) || {}, autorizada = fcfg.cuenta_real && (U.permitidas || []).includes(fcfg.cuenta_real);
  const estadoReal = !fcfg.cuenta_real ? 'cuenta real: ninguna elegida (conéctala en NinjaTrader)' : !fcfg.real_activo ? `cuenta real ${esc(fcfg.cuenta_real)}: <b>parada</b>`
    : !autorizada ? `⚠️ ${esc(fcfg.cuenta_real)} activada aquí pero falta autorizarla en puente.json del PC` : `💵 <b>${esc(fcfg.cuenta_real)} ACTIVA</b> · freno ${fcfg.real_perdida_dia} $/día · ${fcfg.qty} contrato(s)`;
  $('#piFut').innerHTML = (U ? `<div class="chico" style="margin-bottom:4px">${fcfg.activo ? '🧪 Sim operando' : '⏸️ Futuros parados'} · ${estadoReal}</div>` : '') + (U ? (U.error ? `<div class="re-bot" style="border-left-color:var(--rojo)">⚠️ ${esc(U.error)}</div>` : '')
    + `<div class="chico">${(U.cuentas || []).filter(c => c.conectada).map(c => `<b>${esc(c.nombre)}</b> ${c.simulada ? '(sim)' : '<b>(REAL)</b>'}: ${n(c.saldo, 2)} $ · realizado ${sg(c.realizado, 2)} $`).join(' · ') || 'NinjaTrader sin cuentas conectadas'}</div>`
    + `<div class="chico">Abiertas: ${Object.values(U.abiertas || {}).map(o => `${o.real ? '💵' : '🧪'} ${esc(o.nt)} ${o.lado === 1 ? 'largo' : 'corto'} («${esc(o.estrategia)}»)`).join(' · ') || 'ninguna'}</div>`
    + (FF && FF.promovidas ? `<div class="re-bot" style="margin-top:6px;border-left-color:var(--verde)">💰 <b>Estrategias autorizadas para la cuenta real</b>: ${FF.promovidas.length ? FF.promovidas.map(x => `<b>${esc(x.nombre || x.id)}</b> (${usd(x.vivo)})`).join(' · ') : '<span class="tenue">ninguna todavía (se promueven en Laboratorio → Fábrica)</span>'}</div>` : '')
    : '<div class="tenue chico">Solo en el PC.</div>');
  // la fábrica de futuros es simulación: va al Laboratorio
  $('#labFut').innerHTML = FF && FF.manuales ? `<div class="tenue chico">${FF.muestras} muestras · ${esc(FF.msg || '')} · resultados en $ por contrato, con costes</div>`
    + `<div class="re-bot" style="margin-top:6px;border-left-color:var(--verde)">💰 <b>Promovidas a la cuenta real</b> (en vivo ≥ ${FF.promo.vivo_n} ops, ≥ +${FF.promo.vivo_usd} $/op, ≥ ${FF.promo.vivo_gana} % ganadoras): ${(FF.promovidas || []).length ? FF.promovidas.map(x => `<b>${esc(x.nombre || x.id)}</b> (${usd(x.vivo)})`).join(' · ') : '<span class="tenue">ninguna todavía</span>'}</div>`
    + tabla([['Estrategia', x => `<b>${esc(x.nombre || x.id)}</b><div class="tenue chico">${esc(x.regla)}</div>`], ['Entreno', x => usd(x.entreno)], ['Validación', x => usd(x.prueba)], ['En vivo', x => usd(x.vivo)]], (FF.manuales || []).concat(FF.salon || []), '')
    : '<div class="tenue chico">Solo en el PC.</div>';
  guardarCerebro('futuros', [], (U && U.log) || []);
  guardarCerebro('piloto', [], p.log || []);
  E.piloto = p;
  pintarSeguridad(p);
}
async function guardarFuturos(extra = {}) {
  try {
    await api('/futuros/config', 'POST', { activo: $('#fuSim').checked, cuenta_real: $('#fuCuenta').value || null, real_perdida_dia: +$('#fuFreno').value, qty: +$('#fuQty').value, ...extra });
    toast('Futuros: guardado (llega al PC en menos de 1 min)'); cargarPiloto();
  } catch (e) { toast(e.message); }
}
$('#fuGuardar').onclick = () => guardarFuturos();
$('#fuSim').onchange = () => guardarFuturos();
$('#fuRealOn').onclick = () => { if (!$('#fuCuenta').value) return toast('Elige primero la cuenta real');
  if (confirm(`¿Activar futuros con DINERO REAL en ${$('#fuCuenta').value}?
Solo estrategias que ya ganaron en vivo, ${$('#fuQty').value} contrato(s), se para el día que pierda ${$('#fuFreno').value} $.`)) guardarFuturos({ real_activo: true }); };
$('#fuRealOff').onclick = () => guardarFuturos({ real_activo: false });
$('#fuCerrar').onclick = () => { if (confirm('¿Cerrar TODAS las posiciones de futuros (Sim y real) ahora?')) guardarFuturos({ cerrar_todo: Math.floor(Date.now() / 1000) }); };
async function guardarPiloto(extra = {}) {
  try {
    await api('/piloto/config', 'POST', { por_op_sol: +$('#piPorOp').value, perdida_max_sol: +$('#piPerd').value, max_ops_dia: +$('#piOps').value, umbral: +$('#piUmbral').value, ...extra });
    toast(extra.activo === true ? 'Piloto ACTIVADO: opera con dinero real dentro de esos límites' : extra.activo === false ? 'Piloto parado (si tiene una orden dentro, GMGN sigue vigilando su TP/SL)' : 'Guardado'); cargarPiloto();
  } catch (e) { toast(e.message); }
}
$('#piGuardar').onclick = () => guardarPiloto();
$('#piOn').onclick = () => { if (confirm(`¿Activar el piloto con DINERO REAL?\n${$('#piPorOp').value} SOL por operación, se apaga solo si pierde ${$('#piPerd').value} SOL.`)) guardarPiloto({ activo: true }); };
$('#piOff').onclick = () => guardarPiloto({ activo: false });
setInterval(() => { if (document.hidden) return; if (E.pag === 'real-memes') { cargarReal(); cargarPiloto(); } else if (['real-futuros', 'real-seguridad'].includes(E.pag)) cargarPiloto(); }, 5000);
setInterval(() => { if (!document.hidden && ['lab-fabrica', 'lab-modelos', 'lab-escaner', 'cerebro-decisiones'].includes(E.pag)) cargarPiloto().then(() => E.pag === 'cerebro-decisiones' && pintarDecisiones()); }, 20000);

// ---------- Up/Down de Polymarket (simulado) ----------
async function cargarUpdown() {
  try {
    const [m, inf] = await Promise.all([api('/updown'), api('/updown/informe')]);
    const t = (x, y) => `<div class="tile"><small>${x}</small><b>${y}</b></div>`, pc = x => x == null ? '—' : n(x * 100, 1) + ' %';
    $('#udEstado').innerHTML = m.activo ? `<span class="verde">● ${m.agentes} apostando</span>` : '<span class="tenue">○ parado</span>';
    $('#udKpis').innerHTML = [t('Apostadores', m.agentes), t('Apuestas resueltas', m.apuestas), t('Abiertas', m.abiertas), t('Acierto', pc(m.acierto)),
      t('Resultado', sg(m.pnl) + ' $'), t('Capital total', n(m.capital_total) + ' $'), t('Quiebras', m.quiebras)].join('');
    $('#udInforme').textContent = inf.texto;
    $('#udMerc').innerHTML = tabla([['Mercado', x => `<b>${x.activo}</b> ${x.marco}`], ['Inicio', x => np(x.K)], ['Ahora', x => np(x.S)], ['Δ', x => sg((x.S / x.K - 1) * 100, 3) + ' %'],
      ['Modelo P(Up)', x => pc(x.p_up)], ['Up', x => x.up != null ? n(x.up, 2) : '—'], ['Down', x => x.down != null ? n(x.down, 2) : '—'],
      ['Ventaja Up', x => x.up != null ? sg((x.p_up - x.up) * 100, 1) + ' pt' : '—'], ['Quedan', x => hace_(x.segs)]], m.mercados || [], 'Sin mercados ahora mismo.');
    $('#udEst').innerHTML = tabla([['Estrategia', ([k]) => `<b>${k}</b>`], ['Apuestas', ([, v]) => v.apuestas], ['Acierto', ([, v]) => pc(v.acierto)],
      ['Necesita', ([, v]) => '> ' + pc(v.precio_medio)], ['ROI', ([, v]) => v.roi != null ? sg(v.roi * 100, 1) + ' %' : '—'], ['Resultado', ([, v]) => sg(v.pnl) + ' $'],
      ['Comisiones', ([, v]) => n(v.comisiones) + ' $']], Object.entries(m.estrategias || {}), 'Aún no se ha resuelto ninguna apuesta.');
    $('#udAg').innerHTML = tabla([['Apostador', a => `<b>${esc(a.nombre)}</b>`], ['Estrategia', a => a.estrategia], ['Mercados', a => `${a.genoma.activos.join(',')} · ${a.genoma.marcos.join(',')}`],
      ['Capital', a => n(a.capital, 2) + ' $'], ['Apuestas', a => `${a.apuestas} (${a.ganadas}✔)`], ['Resultado', a => sg(a.pnl) + ' $'], ['Gen', a => a.generacion],
      ['Quiebras', a => a.quiebras || ''], ['Último', a => `<span class="chico">${esc(a.ultimo || '')}</span>`]], m.apostadores || []);
    $('#udAp').innerHTML = tabla([['Hora', x => fecha(x.ts)], ['Apostador', x => esc(x.nombre)], ['Estrategia', x => x.estrategia], ['Mercado', x => `${x.activo.toUpperCase()} ${x.marco}`],
      ['Lado', x => x.lado === 'Up' ? '<span class="verde">Up</span>' : '<span class="rojo">Down</span>'], ['Precio', x => n(x.precio, 2)], ['$', x => n(x.coste + x.comision, 2)],
      ['Quedaban', x => hace_(x.segs_rest)], ['Resultado', x => x.resultado ? (x.resultado === x.lado ? '<span class="verde">✔ ganó</span>' : '<span class="rojo">✘ perdió</span>') : '<span class="tenue">abierta</span>'],
      ['P&L', x => x.pnl != null ? sg(x.pnl) + ' $' : '—']], m.recientes || [], 'Sin apuestas todavía.');
    guardarCerebro('updown', m.lecciones || [], m.log || []);
  } catch (e) { msg('#udMsg', e.message); }
}
$('#udIniciar').onclick = async () => { try { await api('/updown/iniciar', 'POST', { agentes: +$('#udN').value, capital: +$('#udCap').value }); msg('#udMsg', 'Apostando (simulado).', true); cargarUpdown(); } catch (e) { msg('#udMsg', e.message); } };
$('#udParar').onclick = async () => { try { await api('/updown/parar', 'POST'); msg('#udMsg', 'Parado.', true); cargarUpdown(); } catch (e) { msg('#udMsg', e.message); } };
setInterval(() => { if (!document.hidden && E.pag === 'lab-updown') cargarUpdown(); }, 5000);

// ---------- navegación: sección → subpestaña ----------
const SUBS = {
  resumen: [],
  real: [['memes', '🪙 Memecoins (GMGN)'], ['futuros', '📈 Futuros (NinjaTrader)'], ['seguridad', '🛡️ Seguridad']],
  lab: [['fabrica', '🏭 Fábrica'], ['enjambre', '🤖 Enjambre'], ['updown', '🎲 Up/Down'], ['modelos', '🧪 Modelos de IA'], ['escaner', '🔎 Escáner del piloto']],
  manual: [['mesa', 'Mesa'], ['practica', 'Práctica'], ['backtest', 'Backtest'], ['diario', 'Diario'], ['stats', 'Estadísticas'], ['alertas', 'Escáner y alertas']],
  cerebro: [['decisiones', 'Decisiones de APOLO'], ['conciencia', 'Conciencia y grafo'], ['lecciones', 'Lecciones'], ['actividad', 'Actividad']],
  ajustes: [['conexiones', 'Conexiones'], ['cuentas', 'Cuentas simuladas'], ['avisos', 'Avisos']],
};
// nombres antiguos (enlaces internos y memoria del navegador)
const ALIAS = { mesa: ['manual', 'mesa'], agente: ['lab', 'enjambre'], updown: ['lab', 'updown'], diario: ['manual', 'diario'], stats: ['manual', 'stats'], backtest: ['manual', 'backtest'], practica: ['manual', 'practica'], escaner: ['manual', 'alertas'] };
const CARGAR = {
  resumen: () => cargarResumen(),
  'real-memes': () => { cargarReal(); cargarPiloto(); }, 'real-futuros': () => cargarPiloto(), 'real-seguridad': () => cargarPiloto(),
  'lab-fabrica': () => cargarPiloto(), 'lab-enjambre': () => cargarAgente(), 'lab-updown': () => cargarUpdown(), 'lab-modelos': () => cargarPiloto(), 'lab-escaner': () => cargarPiloto(),
  'manual-diario': () => cargarDiario(), 'manual-stats': () => cargarStats(), 'manual-practica': () => cargarPracticas(), 'manual-alertas': () => cargarAlertas(),
  'cerebro-decisiones': () => cargarPiloto().then(pintarDecisiones), 'cerebro-conciencia': () => { cargarConciencia(); abrirGrafo(); },
  'cerebro-lecciones': () => cargarCerebroTodo().then(pintarLecciones), 'cerebro-actividad': () => cargarCerebroTodo().then(pintarActividad),
  'ajustes-conexiones': () => cargarConexiones(), 'ajustes-cuentas': () => pintarCfg(),
};
const memo = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { } };
function irA(p, s) {
  if (ALIAS[p]) [p, s] = ALIAS[p];
  if (!SUBS[p]) p = 'resumen';
  const subs = SUBS[p]; if (subs.length && !subs.some(x => x[0] === s)) s = memo('dmn.sub.' + p) || subs[0][0];
  if (subs.length && !subs.some(x => x[0] === s)) s = subs[0][0];
  E.sec = p; E.sub2 = s; E.pag = subs.length ? p + '-' + s : p; memo('dmn.sec', p); if (s) memo('dmn.sub.' + p, s);
  $$('#pestanas button').forEach(b => b.classList.toggle('on', b.dataset.p === p));
  $('#subpestanas').innerHTML = subs.map(([k, t]) => `<button data-s="${k}" class="${k === s ? 'on' : ''}">${t}</button>`).join('');
  $('#subpestanas').className = 'subpestanas ' + (p === 'real' ? 'real' : ['lab', 'manual'].includes(p) ? 'sim' : '');
  $('#subpestanas').hidden = !subs.length;
  $('#barraManual').classList.toggle('oculto', p !== 'manual');
  $$('.pag').forEach(x => x.classList.toggle('on', x.id === 'p-' + E.pag));
  if (p !== 'cerebro' || s !== 'conciencia') cerrarGrafo();
  if (E.pag === 'manual-mesa' && !E.mesaVista) { E.mesaVista = true; setTimeout(() => cargarGrafico(), 0); }   // los gráficos se crean ocultos: encuadrar al verlos
  CARGAR[E.pag]?.();
}
$$('#pestanas button').forEach(b => b.onclick = () => irA(b.dataset.p));
$('#subpestanas').onclick = e => { const b = e.target.closest('[data-s]'); if (b) irA(E.sec, b.dataset.s); };
async function cambiarCuenta() { localStorage.setItem('dmn.cuenta', $('#selCuenta').value); pararAuto(); await cargarCuenta(); limpiarTicket(); await cargarGrafico(); cargarCotizacion(); cargarAnalisis(); if (E.pag && E.pag !== 'manual-mesa') irA(E.sec, E.sub2); }
$('#selCuenta').onchange = cambiarCuenta;
$$('.lado button').forEach(b => b.onclick = () => ponerLado(b.dataset.lado));
$('#tTipo').onchange = ponerTipo;
$$('#subTablas button').forEach(b => b.onclick = () => { E.sub = b.dataset.t; $$('#subTablas button').forEach(x => x.classList.toggle('on', x === b)); pintarAbajo(); });
$('#btnCerrarTodo').onclick = async () => { if (!E.cuenta.posiciones.length || !confirm('¿Cerrar TODAS las posiciones a mercado?')) return; try { await api(`/cuentas/${E.cuenta.id}/cerrar`, 'POST', { simbolo: '*' }); cargarCuenta(); } catch (e) { toast(e.message); } };
$('#intervalos').innerHTML = INTERVALOS.map(i => `<button data-i="${i}" class="${i === E.intervalo ? 'on' : ''}">${i}</button>`).join('');
$$('#intervalos button').forEach(b => b.onclick = () => { E.intervalo = b.dataset.i; localStorage.setItem('dmn.int', E.intervalo); $$('#intervalos button').forEach(x => x.classList.toggle('on', x === b)); cargarGrafico(); });
$$('#capas button').forEach(b => { b.classList.toggle('on', E.capas.has(b.dataset.c)); b.onclick = () => { E.capas.has(b.dataset.c) ? E.capas.delete(b.dataset.c) : E.capas.add(b.dataset.c); b.classList.toggle('on'); localStorage.setItem('dmn.capas', JSON.stringify([...E.capas])); dibujar(E.velas); }; });
document.addEventListener('keydown', e => { if (!esReplay() || e.target.matches('input,select')) return; if (e.key === 'ArrowRight') avanzar(e.shiftKey ? 10 : 1); if (e.key === ' ') { e.preventDefault(); $('#rAuto').click(); } });

(async function inicio() {
  crearGraficos(); ponerTipo(); prepararBT();
  await cargarCuentas(); await cargarCuenta(); irA(memo('dmn.sec') || 'resumen'); actualizarChips();
  await cargarGrafico(); cargarCotizacion(); cargarAnalisis(); cargarVig(); cargarAvisos();
  setInterval(() => { if (!document.hidden) actualizarChips(); }, 30000);
  setInterval(() => { if (!document.hidden && E.pag === 'resumen') cargarResumen(); }, 15000);
  setInterval(() => { if (document.hidden) return; cargarCotizacion(); if (!esReplay()) cargarCuenta(); cargarAvisos(); }, 10000);
  setInterval(() => { if (!document.hidden && !esReplay()) cargarVig(); }, 30000);
  setInterval(() => { if (!document.hidden && !esReplay() && ['5m', '15m'].includes(E.intervalo)) cargarGrafico(false); }, 60000);
})();

function lineaConciencia(L, titulo = '') {
  const cab = titulo ? `<div class="chico" style="margin-top:8px"><b>${titulo}</b> · nivel ${L.length ? n(L[L.length - 1].nivel, 0) : '—'}/100</div>` : '';
  if (L.length < 2) return cab + '<div class="tenue chico">La línea aparece a partir de la segunda ronda de entrenamiento (cada 30 min).</div>';
  const W = 600, H = 120, x = i => 4 + i * (W - 8) / (L.length - 1), y = v => H - 4 - (Math.max(0, Math.min(100, v)) / 100) * (H - 8);
  const pts = L.map((r, i) => `${x(i).toFixed(1)},${y(r.nivel).toFixed(1)}`).join(' ');
  const ult = L[L.length - 1];
  return cab + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:120px;margin-top:6px;background:rgba(127,127,127,.06);border-radius:6px">`
    + [25, 50, 75].map(v => `<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}" stroke="currentColor" opacity=".12"/>`).join('')
    + `<polyline points="${pts}" fill="none" stroke="var(--verde)" stroke-width="2" vector-effect="non-scaling-stroke"/>`
    + L.map((r, i) => r.mejor_vivo != null ? `<circle cx="${x(i)}" cy="${y(r.nivel)}" r="3" fill="var(--verde)"><title>en vivo ${r.mejor_vivo} %/op</title></circle>` : '').join('')
    + `</svg><div class="tenue chico">${L.length} rondas · desde ${fecha(L[0].ts)} · ahora: ${ult.pasan} pasan la validación${ult.mejor_prueba != null ? `, mejor ${sg(ult.mejor_prueba, 1)} %/op fuera de muestra` : ''}. Hasta 40 = solo datos y exploración; por encima solo sube con ventaja demostrada.</div>`;
}


// ---------- datos compartidos (las consultas pesadas se reutilizan un rato) ----------
const CACHE = {};
async function apiCache(ruta, ttl) {
  const c = CACHE[ruta], ahora = Date.now();
  if (c && ahora - c.t < ttl * 1000) return c.p;
  const p = api(ruta); CACHE[ruta] = { t: ahora, p };
  p.catch(() => { delete CACHE[ruta]; });
  return p;
}
const tile = (x, y, z = '') => `<div class="tile"><small>${x}</small><b>${y}</b>${z ? `<span class="tenue chico">${z}</span>` : ''}</div>`;
const SUPERV = { apagado: 'Piloto apagado', validando: 'Validando estrategias', esperando_senal: 'Esperando señal', protegida: 'Protecciones verificadas', conciliacion: 'Conciliación pendiente', saliendo: 'Confirmando salida', requiere_revision: 'Requiere revisión' };

// ---------- cabecera: estado de todo de un vistazo ----------
async function actualizarChips() {
  const mapa = apiCache('/enjambre/mapa', 60);   // lento: se añade cuando llega
  const [p, r] = await Promise.allSettled([api('/piloto'), api('/real')]);
  const ch = [], P = p.value, R = r.value;
  if (P) {
    E.piloto = P; const c = P.cfg || {}, o = P.orden;
    ch.push(`<span class="chip real ${c.activo ? 'on' : ''}" data-ir="real-memes">● Piloto ${c.activo ? 'activo' : 'parado'}${o ? ` · ${esc(o.simbolo)}` : ''}</span>`);
    const f = P.futuros?.cfg;
    if (f) ch.push(`<span class="chip real ${f.real_activo ? 'on' : ''}" data-ir="real-futuros">● Futuros ${f.real_activo ? 'real activo' : f.activo ? 'Sim101' : 'parados'}</span>`);
    if (P.supervision?.requiere_revision) ch.push(`<span class="chip alerta" data-ir="real-seguridad">⚠ Revisión pendiente</span>`);
  }
  if (R) ch.push(`<span class="chip ${R.ts && R.edad < 60 ? 'on' : 'apagado'}" data-ir="ajustes-conexiones">● PC ${R.ts && R.edad < 60 ? 'conectado' : 'sin datos'}</span>`);
  $('#chips').innerHTML = ch.join('');
  const M = await mapa.catch(() => null);
  if (M) { const k = (M.nodos.find(x => x.local) || M.nodos[0] || {}).kpis || {}; $('#chips').insertAdjacentHTML('beforeend', `<span class="chip sim ${k.activo ? 'on' : ''}" data-ir="lab-enjambre">● Enjambre ${k.activo ? (k.agentes || '') + ' agentes' : 'parado'}</span>`); }
}
$('#chips').onclick = e => { const c = e.target.closest('[data-ir]'); if (!c) return; const [a, b] = c.dataset.ir.split('-'); irA(a, b); };

// ---------- resumen ----------
async function cargarResumen() {
  const [p, r, m, u] = await Promise.allSettled([api('/piloto'), api('/real'), apiCache('/enjambre/mapa', 60), apiCache('/updown', 60)]);
  const P = p.value || {}, R = r.value || {}, M = m.value, U = u.value, c = P.cfg || {}, o = P.orden, F = P.fabrica || {}, FU = P.futuros || {}, fc = FU.cfg || {};
  if (p.value) E.piloto = P;
  const conf = (P.ops || []).filter(x => x.pnl_confirmado), neto = conf.reduce((a, x) => a + (x.pnl_sol || 0), 0);
  const vPos = (R.posiciones || []).reduce((a, x) => a + (x.valor || 0), 0);
  $('#rsReal').innerHTML = [
    tile('Wallet GMGN', R.sol != null ? n(R.sol, 4) + ' SOL' : '—', R.sol_usd ? '≈ ' + n(R.sol * R.sol_usd + vPos) + ' $' : ''),
    tile('Piloto memecoins', c.activo ? '<span class="verde">● Activo</span>' : '<span class="tenue">○ Parado</span>', esc(SUPERV[P.supervision?.estado] || '')),
    tile('Posición', o ? esc(o.simbolo) + (o.pnl_pct != null ? ' ' + sg(o.pnl_pct, 1) + ' %' : '') : '<span class="tenue">ninguna</span>', o ? (o.proteccion_verificada ? 'TP/SL verificados' : 'conciliando') : ''),
    tile('Neto confirmado', conf.length ? sg(neto, 4) + ' SOL' : '—', `${conf.length} operaciones con recibos`),
    tile('Futuros real', fc.real_activo ? `<span class="verde">● ${esc(fc.cuenta_real)}</span>` : '<span class="tenue">○ Parado</span>', fc.real_activo ? `freno ${fc.real_perdida_dia} $/día` : ''),
  ].join('');
  $('#rsRealTxt').innerHTML = P.error && P.error_ts > Date.now() / 1000 - 600 ? `<div class="re-bot" style="border-left-color:var(--ambar)">Último aviso del piloto: ${esc(P.error)}</div>` : '';
  const k = M ? ((M.nodos.find(x => x.local) || M.nodos[0] || {}).kpis || {}) : {}, con = P.conciencia;
  $('#rsLab').innerHTML = [
    tile('Promovidas a real', `${(F.promovidas || []).length} memes · ${((FU.fabrica || {}).promovidas || []).length} futuros`, (F.promovidas || []).length ? '' : 'el dinero real espera'),
    tile('Fábrica', F.muestras ? n(F.muestras.listas, 0) + ' muestras' : '—', F.muestras ? `${F.muestras.pendientes} esperando resultado` : ''),
    tile('Enjambre', M ? (k.activo ? `<span class="verde">● ${k.agentes}</span>` : '<span class="tenue">○ parado</span>') : '—', M ? `${M.total.trades} operaciones · R medio ${k.r_medio != null ? n(k.r_medio, 2) : '—'}` : ''),
    tile('Up/Down', U ? (U.activo ? `<span class="verde">● ${U.agentes}</span>` : '<span class="tenue">○ parado</span>') : '—', U ? `acierto ${U.acierto != null ? n(U.acierto * 100, 1) + ' %' : '—'}` : ''),
    tile('Conciencia', con ? n(con.nivel, 0) + '/100' : '—', con ? `${con.velas_total} muestras aprendidas` : ''),
  ].join('');
  $('#rsLabTxt').innerHTML = '';
  $('#rsApolo').innerHTML = decisiones(P).slice(0, 6).map(filaDecision).join('') || '<span class="tenue">APOLO todavía no ha decidido ninguna operación real.</span>';
  const at = [];
  if (P.supervision?.requiere_revision) at.push(`<div class="re-bot" style="border-left-color:var(--rojo)">Incidencia en el piloto real: ${esc(P.supervision.motivo)} <a href="#" data-irsec="real-seguridad">revisar</a></div>`);
  if (P.intencion_entrada) at.push('<div class="re-bot" style="border-left-color:var(--rojo)">Hay una entrada sin confirmar en GMGN. <a href="#" data-irsec="real-seguridad">revisar</a></div>');
  if (R.ts && R.edad > 120) at.push(`<div class="re-bot" style="border-left-color:var(--ambar)">El PC no manda datos desde hace ${hace_(R.edad)}.</div>`);
  if (FU.error && (fc.activo || fc.real_activo)) at.push(`<div class="re-bot" style="border-left-color:var(--ambar)">Futuros: ${esc(FU.error)}</div>`);
  $('#rsAtencion').innerHTML = at.join('') || '<span class="verde">Nada pendiente.</span>';
}
document.addEventListener('click', e => { const a = e.target.closest('[data-irsec]'); if (!a) return; e.preventDefault(); const [x, y] = a.dataset.irsec.split('-'); irA(x, y); });

// ---------- seguridad del dinero real ----------
function pintarSeguridad(p) {
  const c = p.cfg || {}, sv = p.supervision || {}, fc = (p.futuros || {}).cfg || {};
  $('#seEstado').innerHTML = `<div class="tiles">${tile('Estado', esc(SUPERV[sv.estado] || sv.estado || '—'))}${tile('Piloto', c.activo ? '<span class="verde">Activo</span>' : 'Parado')}${tile('Entrada sin confirmar', p.intencion_entrada ? '<span class="rojo">Sí</span>' : 'No')}${tile('Incidencias abiertas', (sv.incidencias || []).length)}</div><div class="chico">${esc(sv.motivo || '')}</div>`;
  $('#seIncidencias').innerHTML = ((sv.incidencias || []).map(x => `<div class="re-bot" style="border-left-color:${x.critica ? 'var(--rojo)' : 'var(--ambar)'}"><b>${esc(x.codigo)}</b> · ${esc(x.motivo)}<div class="tenue chico">desde ${fecha(x.creada)} · última ${fecha(x.ultima)}</div></div>`).join('') || '<div class="verde chico">Sin incidencias.</div>')
    + (sv.requiere_revision || p.intencion_entrada ? '<button id="seConciliado" class="ambar" style="margin-top:8px">✅ Ya lo revisé en GMGN</button><div class="tenue chico">Resuelve las incidencias y olvida la entrada sin confirmar. No activa el piloto.</div>' : '');
  const b = $('#seConciliado');
  if (b) b.onclick = () => { if (confirm('¿Revisaste en GMGN las órdenes y la wallet?\nSe resuelven las incidencias y se olvida la entrada sin confirmar (si la hubiera). Una orden que siga abierta en GMGN y no esté en el panel tendrás que cerrarla tú.')) guardarPiloto({ conciliado: Math.floor(Date.now() / 1000) }); };
  $('#seLimites').innerHTML = `<div>Memecoins: ${n(c.por_op_sol, 3)} SOL por operación · se apaga si pierde ${n(c.perdida_max_sol, 3)} SOL desde ${p.saldo_inicio != null ? n(p.saldo_inicio, 4) + ' SOL' : '—'} · máximo ${c.max_ops_dia} operaciones al día · reserva ${n(c.reserva_sol, 3)} SOL para comisiones · tras 2 stops seguidos, pausa de ${c.pausa_stops_h} h.</div>
    <div>Futuros: ${fc.cuenta_real ? `cuenta ${esc(fc.cuenta_real)} · freno ${fc.real_perdida_dia} $/día · ${fc.qty} contrato(s)` : 'sin cuenta real'}.</div>
    <div class="tenue">Sin APOLO no se abre ninguna operación real. Los límites dependen de que el motor esté en marcha: no garantizan un máximo ante saltos de precio.</div>`;
}

// ---------- cerebro: decisiones de APOLO, lecciones y actividad en un solo sitio ----------
function decisiones(P) {
  const out = (P.log || []).filter(([, l]) => /🧠/.test(l)).map(([ts, l]) => ({ ts, txt: l.replace(/^🧠\s*/, ''), tipo: /veta|no contestó/.test(l) ? 'veta' : /aprueba/.test(l) ? 'aprueba' : 'info' }));
  const o = P.orden;
  if (o && o.ia && o.ia.motivo) out.push({ ts: o.ia.ts, txt: `Revisión de ${o.simbolo}: ${o.ia.accion} (${n((o.ia.confianza || 0) * 100, 0)} %) — ${o.ia.motivo}`, tipo: o.ia.accion === 'salir' ? 'veta' : 'aprueba' });
  for (const op of P.ops || []) if (/APOLO lo ve mal/.test(op.salida || '')) out.push({ ts: op.cerrada, txt: `Cerró ${op.simbolo}: ${op.salida}`, tipo: 'veta' });
  return out.sort((a, b) => b.ts - a.ts);
}
const filaDecision = d => `<div class="decision ${d.tipo}"><span>${d.tipo === 'veta' ? '✋' : d.tipo === 'aprueba' ? '✅' : '🧠'}</span><div>${esc(d.txt)}<div class="tenue chico">${fecha(d.ts)}</div></div></div>`;
function pintarDecisiones() {
  $('#ceDecisiones').innerHTML = decisiones(E.piloto || {}).map(filaDecision).join('') || '<div class="tenue">APOLO todavía no ha decidido ninguna operación real. Cuando haya una estrategia promovida, cada entrada pasa por él.</div>';
}
E.cb = { lec: {}, act: {} };
function guardarCerebro(origen, lecciones, log) {
  E.cb.lec[origen] = lecciones.map(l => ({ ts: l.ts, txt: l.texto, tipo: l.tipo, origen }));
  E.cb.act[origen] = log.map(x => Array.isArray(x) ? { ts: x[0], txt: x[1], origen } : { ts: null, txt: String(x), origen });
}
async function cargarCerebroTodo() {
  const [m, u, p, r] = await Promise.allSettled([apiCache('/enjambre/mapa', 30), apiCache('/updown', 30), api('/piloto'), api('/real')]);
  if (m.value) guardarCerebro('enjambre', m.value.lecciones || [], m.value.log || []);
  if (u.value) guardarCerebro('updown', u.value.lecciones || [], u.value.log || []);
  if (p.value) { E.piloto = p.value; guardarCerebro('piloto', [], p.value.log || []); guardarCerebro('futuros', [], (p.value.futuros || {}).log || []); }
  if (r.value) guardarCerebro('eventos', [], r.value.eventos || []);
}
const ORIGEN = { enjambre: '🤖', updown: '🎲', piloto: '🧭', futuros: '📈', eventos: '⚡' };
let filtroLec = '', filtroAct = '';
function pintarLecciones() {
  const L = Object.entries(E.cb.lec).filter(([k]) => !filtroLec || k === filtroLec).flatMap(([, v]) => v).sort((a, b) => (b.ts || 0) - (a.ts || 0)).slice(0, 200);
  $('#ceLecciones').innerHTML = L.map(l => `<div class="lec">${ORIGEN[l.origen] || ''} ${esc(l.txt)}<br><small>${fecha(l.ts)} · ${esc(l.origen)}${l.tipo ? ' · ' + esc(l.tipo) : ''}</small></div>`).join('') || '<span class="tenue">Aún sin lecciones.</span>';
}
function pintarActividad() {
  const A = Object.entries(E.cb.act).filter(([k]) => !filtroAct || k === filtroAct).flatMap(([, v]) => v);
  const conTs = A.filter(x => x.ts).sort((a, b) => b.ts - a.ts), sinTs = A.filter(x => !x.ts).reverse();
  $('#ceActividad').textContent = [...conTs.map(x => `${fecha(x.ts)}  ${ORIGEN[x.origen] || ''} ${x.txt}`), ...sinTs.map(x => `${ORIGEN[x.origen] || ''} ${x.txt}`)].slice(0, 400).join('\n') || 'Sin actividad.';
}
$('#leFiltro').onclick = e => { const b = e.target.closest('[data-f]'); if (!b) return; filtroLec = b.dataset.f; $$('#leFiltro button').forEach(x => x.classList.toggle('on', x === b)); pintarLecciones(); };
$('#acFiltro').onclick = e => { const b = e.target.closest('[data-f]'); if (!b) return; filtroAct = b.dataset.f; $$('#acFiltro button').forEach(x => x.classList.toggle('on', x === b)); pintarActividad(); };
setInterval(() => {
  if (document.hidden) return;
  if (E.pag === 'cerebro-lecciones') cargarCerebroTodo().then(pintarLecciones);
  if (E.pag === 'cerebro-actividad') cargarCerebroTodo().then(pintarActividad);
}, 30000);

// ---------- ajustes → conexiones ----------
async function cargarConexiones() {
  const [s, r, p, m] = await Promise.allSettled([api('/salud'), api('/real'), api('/piloto'), apiCache('/enjambre/mapa', 30)]);
  const S = s.value, R = r.value || {}, P = p.value || {}, M = m.value, U = P.futuros || {};
  const fila = (ok, t, d) => `<div class="conex"><span class="${ok ? 'verde' : ok === false ? 'rojo' : 'tenue'}">●</span><div><b>${t}</b><div class="tenue chico">${d}</div></div></div>`;
  $('#ajConexiones').innerHTML = [
    fila(!!S, 'Motor DMN Trader', S ? `en ${esc(location.host)} · versión ${esc(S.version)} · ${S.cuentas} cuentas simuladas` : 'no responde'),
    fila(R.ts ? R.edad < 60 : null, 'GMGN (wallet real)', R.wallet ? `wallet ${esc(R.wallet.slice(0, 6))}…${esc(R.wallet.slice(-4))} · datos de hace ${hace_(R.edad)} · la clave de GMGN vive solo en el PC` : 'sin datos: enciende el servidor del PC (servidor-pc.bat)'),
    fila(P.cfg ? true : null, 'Piloto real', P.aqui ? 'corre en este equipo' : 'corre en el PC y manda su estado a esta central'),
    fila(U.cuentas ? U.cuentas.some(c => c.conectada) : null, 'NinjaTrader (futuros)', U.cuentas ? (U.cuentas.map(c => `${esc(c.nombre)} ${c.simulada ? '(sim)' : '(REAL)'} ${c.conectada ? 'conectada' : 'desconectada'}`).join(' · ') || 'sin cuentas') : (U.error ? esc(U.error) : 'abre NinjaTrader con el AddOn DmnPuente')),
    ...(M ? M.nodos.map(x => fila(x.local || x.en_linea, `Nodo ${esc(x.nodo)}${x.local ? ' (central)' : ''}`, `${(x.agentes || []).length} agentes · ${x.local ? 'aquí' : x.en_linea ? 'en línea' : 'desconectado'}`)) : []),
  ].join('');
}

// ---------- grafo de la conciencia: el mismo grafo 3D que la memoria de APOLO ----------
const GR_COL = { ronda: '#22c55e', estrategia: '#a855f7', moneda: '#f59e0b', futuro: '#38bdf8', 'operación': '#ef4444', aprendizaje: '#94a3b8', sistema: '#eab308' };
const G3 = { g3: null, modo: 'top', sel: null };
function cerrarGrafo() { G3.g3?.destruir(); G3.g3 = null; G3.sel = null; }
async function abrirGrafo() {
  if (G3.g3) return;
  let g; try { g = await api('/conciencia/grafo'); } catch (e) { $('#grInfo').textContent = e.message; return; }
  if (E.pag !== 'cerebro-conciencia' || G3.g3) return;
  // nodo central (como «tú» en la memoria de APOLO) enlazado a las estrategias y los futuros
  const base = { ronda: .2, moneda: 1, futuro: 6, estrategia: 10, 'operación': 8, aprendizaje: 3, sistema: 6 };
  const nodos = [{ id: 'usuario', tipo: 'sistema', nombre: 'DMN Trader', peso: 30, importancia: 1e9 },
    ...(g.nodos || []).map(x => ({ id: x.id, tipo: x.tipo, nombre: x.nombre, peso: x.veces || 1, importancia: (base[x.tipo] ?? 1) * (x.veces || 1) }))];
  const top = new Set([...nodos].sort((a, b) => b.importancia - a.importancia).slice(0, 120).map(x => x.id));
  nodos.forEach(x => { x.top = top.has(x.id); x.antes = false; });
  const por = new Map(nodos.map(x => [x.id, x]));
  const enlaces = [...(g.aristas || []), ...nodos.filter(x => ['estrategia', 'futuro'].includes(x.tipo)).map(x => ({ de: 'usuario', a: x.id, peso: 2, tipo: 'raiz' }))];
  const aristas = enlaces.map(a => ({ ...a, s: por.get(a.de), t: por.get(a.a) })).filter(a => a.s && a.t);
  const vec = new Map();
  for (const a of aristas) for (const [x, y] of [[a.s, a.t], [a.t, a.s]]) { if (!vec.has(x.id)) vec.set(x.id, new Set()); vec.get(x.id).add(y.id); }
  const visible = x => x.id === 'usuario' || G3.modo === 'todo' || x.top || (G3.sel && (x === G3.sel || vec.get(G3.sel.id)?.has(x.id)));
  const linea = a => visible(a.s) && visible(a.t) && (G3.modo === 'todo' || a.tipo !== 'simuló' || (G3.sel && (a.s === G3.sel || a.t === G3.sel)));
  const cuenta = {}; nodos.forEach(x => { if (x.id !== 'usuario') cuenta[x.tipo] = (cuenta[x.tipo] || 0) + 1; });
  $('#grLeyenda').innerHTML = Object.entries(cuenta).map(([k, v]) => `<span style="color:${GR_COL[k] || '#888'}">●</span> ${esc(k)} ${v}`).join(' · ') + ` · ${aristas.length} enlaces`;
  const ir = () => {
    if (E.pag !== 'cerebro-conciencia' || G3.g3) return;
    G3.g3 = window.Grafo3D.crear($('#grCaja'), { nodos, aristas, visible, linea, colores: GR_COL, acento: '#22c55e',
      alTocar: x => {
        G3.sel = x || null; G3.g3.seleccionar(G3.sel); if (x) G3.g3.enfocar(x);
        $('#grInfo').innerHTML = x ? `<b style="color:${GR_COL[x.tipo] || '#22c55e'}">${esc(x.nombre)}</b> <span class="tenue">(${esc(x.tipo)} · ${vec.get(x.id)?.size || 0} enlaces)</span>` : '';
      } });
  };
  if (window.Grafo3D) ir(); else window.addEventListener('grafo3d-listo', ir, { once: true });
}
$('#grModo').onclick = e => { const b = e.target.closest('[data-m]'); if (!b) return; G3.modo = b.dataset.m; $$('#grModo button').forEach(x => x.classList.toggle('on', x === b)); G3.g3?.refrescar(); };
