// Criptografa o JSON do painel (stdin) com a senha em PAINEL_SENHA.
// Saída: {"v":1,"iter":N,"salt":b64,"iv":b64,"ct":b64} (ct = ciphertext||tag, formato WebCrypto AES-GCM)
const crypto = require('crypto');
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', () => {
  const pw = process.env.PAINEL_SENHA;
  if (!pw) { console.error('PAINEL_SENHA ausente'); process.exit(2); }
  const iter = 600000;
  const salt = crypto.randomBytes(16), iv = crypto.randomBytes(12);
  const key = crypto.pbkdf2Sync(pw, salt, iter, 32, 'sha256');
  const c = crypto.createCipheriv('aes-256-gcm', key, iv);
  const ct = Buffer.concat([c.update(Buffer.concat(chunks)), c.final(), c.getAuthTag()]);
  process.stdout.write(JSON.stringify({ v: 1, iter, salt: salt.toString('base64'), iv: iv.toString('base64'), ct: ct.toString('base64') }));
});
