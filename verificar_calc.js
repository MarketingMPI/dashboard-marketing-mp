// Roda o MESMO núcleo de cálculo do dashboard (template.html) em Node, sem navegador.
// Uso: node verificar_calc.js <payload.json> <cenarios.json>  -> imprime JSON [{cenario, filtros, resultado}]
const fs = require('fs'), path = require('path'), vm = require('vm');
const tpl = fs.readFileSync(path.join(__dirname, 'template.html'), 'utf8');
const a = tpl.indexOf('const DAY = 86400000;'), b = tpl.indexOf('window.MP_DASH = {');
if (a < 0 || b < 0) { console.error('núcleo não encontrado no template'); process.exit(2); }
const D0 = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const cen = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const ctx = {console};
vm.createContext(ctx);
vm.runInContext(tpl.slice(a, b) + '\nD = __D; STAGES = D.stages; this.computeFull = computeFull;', Object.assign(ctx, {__D: D0}));
const out = cen.map(c => ({cenario: c.cenario, filtros: c.filtros, resultado: ctx.computeFull(D0.leads, c.filtros, D0.gerado_em).res}));
process.stdout.write(JSON.stringify(out));
