// Firma el paquete para APOLO tal como lo bajará cualquiera desde GitHub (git archive de HEAD, saltos de línea de
// .gitattributes) y deja FIRMA.json en la raíz del repo para hacer commit.
// uso: node apolo/firmar.js <ruta a APOLO/core/skills/firmar.js> <clave privada .pem>   (la clave NUNCA va al repo)
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const [modulo, clave] = process.argv.slice(2);
if (!modulo || !clave) { console.error('uso: node apolo/firmar.js <APOLO/core/skills/firmar.js> <clave.pem>'); process.exit(2); }
const raiz = path.join(__dirname, '..');
if (execFileSync('git', ['status', '--porcelain'], { cwd: raiz, encoding: 'utf8' }).split('\n').some(l => l && !/ FIRMA\.json$/.test(l))) {
  console.error('hay cambios sin commit: la firma se hace sobre HEAD'); process.exit(1);
}
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'dmn-firma-'));
try {
  const tar = path.join(tmp, 'p.tar');
  execFileSync('git', ['archive', '--format=tar', '-o', tar, 'HEAD'], { cwd: raiz });
  const dir = path.join(tmp, 'p'); fs.mkdirSync(dir);
  execFileSync('tar', ['-xf', 'p.tar', '-C', 'p'], { cwd: tmp });   // rutas relativas: el tar de Git Bash toma "C:" por un servidor
  fs.rmSync(path.join(dir, 'FIRMA.json'), { force: true });
  const firmar = require(path.resolve(modulo));
  firmar.firmar(dir, fs.readFileSync(clave, 'utf8'), { autor: 'DMNENGINE' });
  fs.copyFileSync(path.join(dir, 'FIRMA.json'), path.join(raiz, 'FIRMA.json'));
  const j = JSON.parse(fs.readFileSync(path.join(raiz, 'FIRMA.json'), 'utf8'));
  console.log(`FIRMA.json: ${Object.keys(j.archivos).length} archivos firmados (clave ${j.clavePublica.slice(0, 12)}…). Haz commit de FIRMA.json.`);
} finally { fs.rmSync(tmp, { recursive: true, force: true }); }
