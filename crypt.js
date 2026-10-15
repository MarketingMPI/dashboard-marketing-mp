// AES-256-GCM com chave PBKDF2-SHA256 da senha em PAINEL_SENHA.
//   node crypt.js enc  < bytes  > {"v":1,"iter":N,"salt":b64,"iv":b64,"ct":b64}   (ct = ciphertext||tag, formato WebCrypto)
//   node crypt.js dec  < json   > bytes
// O mesmo formato é decifrado no navegador pelo template (WebCrypto).
const crypto = require('crypto');
const mode = process.argv[2];
const chunks = [];
process.stdin.on('data', c => chunks.push(c));
process.stdin.on('end', () => {
  const pw = process.env.PAINEL_SENHA;
  if (!pw) { console.error('PAINEL_SENHA ausente'); process.exit(2); }
  const input = Buffer.concat(chunks);
  if (mode === 'enc') {
    const iter = 600000, salt = crypto.randomBytes(16), iv = crypto.randomBytes(12);
    const key = crypto.pbkdf2Sync(pw, salt, iter, 32, 'sha256');
    const c = crypto.createCipheriv('aes-256-gcm', key, iv);
    const ct = Buffer.concat([c.update(input), c.final(), c.getAuthTag()]);
    process.stdout.write(JSON.stringify({v: 1, iter, salt: salt.toString('base64'), iv: iv.toString('base64'), ct: ct.toString('base64')}));
  } else if (mode === 'dec') {
    const E = JSON.parse(input.toString('utf8'));
    const key = crypto.pbkdf2Sync(pw, Buffer.from(E.salt, 'base64'), E.iter, 32, 'sha256');
    const ct = Buffer.from(E.ct, 'base64');
    const d = crypto.createDecipheriv('aes-256-gcm', key, Buffer.from(E.iv, 'base64'));
    d.setAuthTag(ct.subarray(ct.length - 16));
    process.stdout.write(Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]));
  } else { console.error('uso: node crypt.js enc|dec'); process.exit(2); }
});
