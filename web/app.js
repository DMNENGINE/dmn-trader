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
      Capital final: 5 % peor <b>${n(mc.final_5)}</b> · mediano <b>${n(mc.final_mediano)}</b><br>Probabilidad de acabar perdiendo: <b>${mc.prob_perdida}%</b>` : '<span class="tenue">Pocos trades para Monte Carlo.</span>';
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
function pintarCfg() { const c = E.cuenta.config; $('#formCfg').innerHTML = CFG.map(([k, t]) => typeof c[k] === 'boolean' ? `<label class="check"><input type="checkbox" data-cfg="${k}" ${c[k] ? 'checked' : ''}> ${t}</label>` : `<label>${t} <input data-cfg="${k}" type="number" step="any" value="${c[k]}"></label>`).join(''); }
$('#btnCfg').onclick = async () => { const b = {}; $$('[data-cfg]').forEach(i => b[i.dataset.cfg] = i.type === 'checkbox' ? i.checked : +i.value);
  try { await api(`/cuentas/${E.cuenta.id}`, 'PATCH', b); await cargarCuenta(); msg('#cfgMsg', 'Reglas guardadas.', true); } catch (e) { msg('#cfgMsg', e.message); } };
$('#btnNueva').onclick = async () => { try { const c = await api('/cuentas', 'POST', { nombre: $('#nNombre').value, capital: +$('#nCap').value, moneda: $('#nMon').value }); await cargarCuentas(); $('#selCuenta').value = c.id; await cambiarCuenta(); msg('#ajMsg', 'Cuenta creada.', true); } catch (e) { msg('#ajMsg', e.message); } };
$('#btnReset').onclick = async () => { if (!confirm('¿Reiniciar la cuenta? Se borran posiciones, órdenes, diario y curva.')) return; await api(`/cuentas/${E.cuenta.id}/reiniciar`, 'POST'); await cambiarCuenta(); msg('#ajMsg', 'Cuenta reiniciada.', true); };
$('#btnBorrar').onclick = async () => { if (E.cuentas.length < 2) return msg('#ajMsg', 'Es la única cuenta.'); if (!confirm('¿Eliminar esta cuenta para siempre?')) return;
  await api(`/cuentas/${E.cuenta.id}`, 'DELETE'); localStorage.removeItem('dmn.cuenta'); await cargarCuentas(); await cambiarCuenta(); msg('#ajMsg', 'Cuenta eliminada.', true); };
$('#btnWebhook').onclick = async () => { try { await api('/ajustes', 'POST', { webhook: $('#wUrl').value.trim() }); msg('#ajMsg', 'Webhook guardado: los avisos (stops, alertas, bloqueos) llegarán ahí.', true); } catch (e) { msg('#ajMsg', e.message); } };

// ---------- enjambre ----------
async function cargarAgente() {
  try {
    const [m, inf] = await Promise.all([api('/enjambre/mapa'), api('/enjambre/informe')]);
    const loc = m.nodos.find(x => x.local) || m.nodos[0], k = loc.kpis, t = (x, y) => `<div class="tile"><small>${x}</small><b>${y}</b></div>`;
    $('#agEstado').innerHTML = k.activo ? `<span class="verde">● ${k.agentes} agentes operando</span> · ciclo ${k.tick_s}s` : '<span class="tenue">○ parado</span>';
    $('#agKpis').innerHTML = [t('Agentes (todos los nodos)', m.total.agentes), t('Generación', k.generacion), t('Operaciones', m.total.trades), t('Acierto', k.trades ? n(k.ganados / k.trades * 100, 0) + '%' : '—'),
      t('R medio', k.r_medio != null ? sg(k.r_medio) : '—'), t('Quiebras', m.total.quiebras), t('Metas 🏆', m.total.metas), t('Mejor capital', n(k.max_equity, 2) + ' $')].join('');
    const cajas = $('#agPorNodo'), vistos = [...cajas.querySelectorAll('input')].map(i => i.dataset.nodo);
    if (m.nodos.map(x => x.nodo).join() !== vistos.join()) cajas.innerHTML = m.nodos.map(x => `<label>${esc(x.nodo)}${x.local ? ' (central)' : ''} <input type="number" min="0" max="2000" data-nodo="${esc(x.nodo)}" value="${(x.kpis || {}).activo ? (x.kpis.n || (x.agentes || []).length) : 0}"></label>`).join('');
    $('#agNodos').textContent = m.nodos.map(x => `${x.nodo}: ${(x.agentes || []).length}${x.local ? ' (aquí)' : x.en_linea ? ' (en línea)' : ' (desconectado)'}`).join(' · ');
    const todos = m.nodos.flatMap(x => (x.agentes || []).map(a => ({ ...a, nodo: x.nodo }))).sort((a, b) => (b.r_medio ?? -9) - (a.r_medio ?? -9));
    $('#agTabla').innerHTML = tabla([['Agente', a => `<b>${esc(a.nombre)}</b>`], ['Nodo', a => a.nodo], ['Gen', a => a.generacion], ['Sistema', a => `${a.estrategia} · ${a.marco}${a.tendencia ? ' · tend' : ''} · ${a.riesgo_pct}%`],
      ['Capital', a => n(a.equity, 2)], ['Máximo', a => n(a.max_equity, 2)], ['Trades', a => `${a.trades} (${a.ganados}✔)`], ['R medio', a => a.r_medio != null ? sg(a.r_medio) : '—'],
      ['Intentos', a => a.intentos], ['🏆', a => a.metas || ''], ['Estado', a => esc(a.estado)], ['Último', a => `<span class="chico">${esc(a.ultimo || '')}</span>`]], todos);
    $('#agInforme').textContent = inf.texto;
    $('#agLec').innerHTML = m.lecciones.map(l => `<div class="lec">${esc(l.texto)}<br><small>${fecha(l.ts)} · ${l.tipo}</small></div>`).join('') || '<span class="tenue">Aún sin lecciones.</span>';
    $('#agLog').textContent = (m.log || []).slice().reverse().join('\n') || 'Sin actividad.';
  } catch (e) { msg('#agMsg', e.message); }
}
$('#agIniciar').onclick = async () => { try {
  const hecho = [];
  for (const i of $('#agPorNodo').querySelectorAll('input')) {
    const nodo = i.dataset.nodo, k = +i.value;
    if (k > 0) await api('/enjambre/iniciar', 'POST', { nodo, agentes: k, capital: +$('#agCap').value, meta: +$('#agMeta').value, hasta: $('#agHasta').value || null, mercados: $('#agMerc').value.split(',') });
    else await api('/enjambre/parar', 'POST', { nodo });
    hecho.push(`${nodo}: ${k || 'parado'}`);
  }
  msg('#agMsg', hecho.join(' · ') + ' (los otros equipos lo aplican en ≤ 10 s)', true); setTimeout(cargarAgente, 1500); } catch (e) { msg('#agMsg', e.message); } };
$('#agParar').onclick = async () => { try { for (const i of $('#agPorNodo').querySelectorAll('input')) { await api('/enjambre/parar', 'POST', { nodo: i.dataset.nodo }); i.value = 0; }
  msg('#agMsg', 'Parados todos.', true); cargarAgente(); } catch (e) { msg('#agMsg', e.message); } };
setInterval(() => { if (!document.hidden && E.pag === 'agente') cargarAgente(); }, 10000);

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
  $('#reEv').textContent = (r.eventos || []).slice().reverse().map(([ts, txt]) => `${fecha(ts)}  ${txt}`).join('\n') || 'Sin eventos todavía: aparecerán aquí las compras, ventas y los TP/SL que salten.';
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
setInterval(() => { if (!document.hidden && E.pag === 'real') cargarReal(); }, 5000);

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
    $('#udLec').innerHTML = (m.lecciones || []).map(l => `<div class="lec">${esc(l.texto)}<br><small>${fecha(l.ts)}</small></div>`).join('') || '<span class="tenue">La primera evolución llega a las 3 h.</span>';
    $('#udLog').textContent = (m.log || []).slice().reverse().join('\n') || 'Sin actividad.';
  } catch (e) { msg('#udMsg', e.message); }
}
$('#udIniciar').onclick = async () => { try { await api('/updown/iniciar', 'POST', { agentes: +$('#udN').value, capital: +$('#udCap').value }); msg('#udMsg', 'Apostando (simulado).', true); cargarUpdown(); } catch (e) { msg('#udMsg', e.message); } };
$('#udParar').onclick = async () => { try { await api('/updown/parar', 'POST'); msg('#udMsg', 'Parado.', true); cargarUpdown(); } catch (e) { msg('#udMsg', e.message); } };
setInterval(() => { if (!document.hidden && E.pag === 'updown') cargarUpdown(); }, 5000);

// ---------- navegación ----------
function irA(p) { $$('#pestanas button').forEach(b => b.classList.toggle('on', b.dataset.p === p)); $$('.pag').forEach(s => s.classList.toggle('on', s.id === 'p-' + p)); E.pag = p;
  ({ agente: cargarAgente, real: cargarReal, updown: cargarUpdown, diario: cargarDiario, stats: cargarStats, practica: cargarPracticas, escaner: cargarAlertas, ajustes: pintarCfg })[p]?.(); }
$$('#pestanas button').forEach(b => b.onclick = () => irA(b.dataset.p));
async function cambiarCuenta() { localStorage.setItem('dmn.cuenta', $('#selCuenta').value); pararAuto(); await cargarCuenta(); limpiarTicket(); await cargarGrafico(); cargarCotizacion(); cargarAnalisis(); if (E.pag && E.pag !== 'mesa') irA(E.pag); }
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
  await cargarCuentas(); await cargarCuenta(); await cargarGrafico(); cargarCotizacion(); cargarAnalisis(); cargarVig(); cargarAvisos();
  setInterval(() => { if (document.hidden) return; cargarCotizacion(); if (!esReplay()) cargarCuenta(); cargarAvisos(); }, 10000);
  setInterval(() => { if (!document.hidden && !esReplay()) cargarVig(); }, 30000);
  setInterval(() => { if (!document.hidden && !esReplay() && ['5m', '15m'].includes(E.intervalo)) cargarGrafico(false); }, 60000);
})();
