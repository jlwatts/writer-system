// Browser/WebCrypto unit requirements. Run with Node 20+:
//   node tests/test_vault_crypto.mjs
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

globalThis.window = globalThis;
vm.runInThisContext(
    await readFile(new URL('../app/static/vault-crypto.js', import.meta.url), 'utf8'),
    { filename: 'vault-crypto.js' },
);

const C = globalThis.WriterCrypto;
const passphrase = 'correct horse battery staple';

// ENC-U-001/ENC-U-002 — Given supported Markdown values, when sealed and
// reopened, then bytes round-trip and plaintext/passphrase markers are absent.
for (const plaintext of ['', 'ASCII', '# Multi\n\nline', 'Unicode café 漢字 😀', 'x'.repeat(1024 * 1024)]) {
    const created = await C.create(passphrase, plaintext);
    assert.equal((await C.open(created.envelope, passphrase)).plaintext, plaintext);
    assert.equal(created.envelope.includes(plaintext || 'impossible-empty-marker'), false);
    assert.equal(created.envelope.includes(passphrase), false);
}

// ENC-U-003/ENC-U-004 — Given one record and unchanged plaintext, when sealed
// twice, then salt/file identity remain stable while nonce/ciphertext differ.
const first = await C.create(passphrase, 'same text');
const secondEnvelope = await C.seal(first.record, 'same text');
const firstPayload = C.parseEnvelope(first.envelope);
const secondPayload = C.parseEnvelope(secondEnvelope);
assert.equal(firstPayload.salt, secondPayload.salt);
assert.equal(firstPayload.file_id, secondPayload.file_id);
assert.notEqual(firstPayload.nonce, secondPayload.nonce);
assert.notEqual(firstPayload.ciphertext, secondPayload.ciphertext);

// ENC-U-005/ENC-U-006 — Given independent files and a derived key, then salts
// differ and WebCrypto refuses key export.
const independent = await C.create(passphrase, 'same text');
assert.notEqual(C.parseEnvelope(independent.envelope).salt, firstPayload.salt);
assert.equal(first.record.key.extractable, false);
await assert.rejects(webcrypto.subtle.exportKey('raw', first.record.key));

// ENC-U-007/ENC-U-008 — Given a valid envelope, when passphrase or any
// authenticated byte is wrong, then no plaintext is returned.
await assert.rejects(C.open(first.envelope, 'wrong password value'));
for (const field of ['salt', 'file_id', 'nonce', 'ciphertext', 'tag']) {
    const changed = { ...firstPayload };
    const bytes = Buffer.from(changed[field], 'base64');
    bytes[0] ^= 1;
    changed[field] = bytes.toString('base64');
    await assert.rejects(C.open(`WRITERENC1\n${JSON.stringify(changed)}`, passphrase));
}

// ENC-U-009/ENC-U-010 — Given malformed, duplicated, or resource-unbounded
// metadata, when parsed, then it is rejected before decryption.
for (const malformed of [
    'WRITERENC1\n{}',
    first.envelope.replace('"v":1', '"v":1,"v":1'),
    first.envelope.replace('"iterations":600000', '"iterations":1'),
    first.envelope.replace('"iterations":600000', '"iterations":1000001'),
    `${first.envelope} `,
]) {
    assert.throws(() => C.parseEnvelope(malformed));
}

// KEY-B-003/KEY-B-004/LOCK-B-010 — Given in-memory records, when moved and
// repeatedly forgotten, then no persistent or stale key mapping remains.
C.remember('old.md', first.record);
C.moveKey('old.md', 'new.md');
assert.equal(C.recall('old.md'), null);
assert.equal(C.recall('new.md'), first.record);
C.remember('keep.md', first.record);
C.remember('notes/a.md', first.record);
C.remember('notes/b.md', first.record);
C.forgetUnder('notes');
assert.equal(C.recall('keep.md'), first.record);
assert.equal(C.recall('notes/a.md'), null);
assert.equal(C.recall('notes/b.md'), null);
C.forgetUnder('new.md');
assert.equal(C.recall('new.md'), null);
assert.equal(C.recall('keep.md'), first.record);
C.forgetAll();
C.forgetAll();
assert.equal(C.keyCount(), 0);

// REKEY-U-001/REKEY-U-002 — Given an unlocked record, when rewrapped with a
// new passphrase, then file identity is stable, the old passphrase fails,
// and the deletion verifier changes.
const oldPass = 'old passphrase value';
const newPass = 'new passphrase value';
const original = await C.create(oldPass, 'keep this text');
const rekeyed = await C.rewrap('keep this text', newPass, original.record);
const originalPayload = C.parseEnvelope(original.envelope);
const rekeyedPayload = C.parseEnvelope(rekeyed.envelope);
assert.equal(rekeyedPayload.file_id, originalPayload.file_id);
assert.notEqual(rekeyedPayload.salt, originalPayload.salt);
assert.equal((await C.open(rekeyed.envelope, newPass)).plaintext, 'keep this text');
await assert.rejects(C.open(rekeyed.envelope, oldPass));
assert.notEqual(rekeyed.verifier, original.verifier);
assert.equal(rekeyed.envelope.includes(newPass), false);
assert.equal(rekeyed.envelope.includes(oldPass), false);

console.log('vault crypto WebCrypto requirements passed');
