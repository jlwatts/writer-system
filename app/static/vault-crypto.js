(function (root) {
    'use strict';

    const HEADER = 'WRITERENC1';
    const VERSION = 1;
    const KDF = 'PBKDF2-HMAC-SHA-256';
    const ITERATIONS = 600000;
    const MIN_ITERATIONS = 100000;
    const MAX_ITERATIONS = 1000000;
    const SALT_BYTES = 16;
    const NONCE_BYTES = 12;
    const TAG_BYTES = 16;
    const FILE_ID_BYTES = 16;
    const MIN_PASSPHRASE = 12;
    const EXPECTED_FIELDS = [
        'ciphertext', 'file_id', 'iterations', 'kdf', 'nonce',
        'salt', 'tag', 'v'
    ];
    const keys = new Map();
    const encoder = new TextEncoder();
    const decoder = new TextDecoder('utf-8', { fatal: true });

    function randomBytes(length) {
        const out = new Uint8Array(length);
        crypto.getRandomValues(out);
        return out;
    }

    function bytesToBase64(bytes) {
        let binary = '';
        for (let index = 0; index < bytes.length; index += 0x8000) {
            binary += String.fromCharCode.apply(null, bytes.subarray(index, index + 0x8000));
        }
        return btoa(binary);
    }

    function base64ToBytes(value, expectedLength) {
        if (typeof value !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) {
            throw new Error('Invalid encrypted file');
        }
        let binary;
        try {
            binary = atob(value);
        } catch (_error) {
            throw new Error('Invalid encrypted file');
        }
        const out = Uint8Array.from(binary, character => character.charCodeAt(0));
        if (bytesToBase64(out) !== value || (expectedLength != null && out.length !== expectedLength)) {
            throw new Error('Invalid encrypted file');
        }
        return out;
    }

    function concatBytes(...parts) {
        const length = parts.reduce((total, part) => total + part.length, 0);
        const out = new Uint8Array(length);
        let offset = 0;
        parts.forEach(part => {
            out.set(part, offset);
            offset += part.length;
        });
        return out;
    }

    function metadata(record) {
        return {
            v: VERSION,
            kdf: KDF,
            iterations: record.iterations,
            salt: record.salt,
            file_id: record.file_id,
        };
    }

    function aad(record) {
        return encoder.encode(JSON.stringify(metadata(record)));
    }

    function parseEnvelope(text) {
        if (typeof text !== 'string' || !text.startsWith(`${HEADER}\n`)) {
            throw new Error('Invalid encrypted file');
        }
        const rawPayload = text.slice(HEADER.length + 1);
        let payload;
        try {
            payload = JSON.parse(rawPayload);
        } catch (_error) {
            throw new Error('Invalid encrypted file');
        }
        if (!payload || Array.isArray(payload) || typeof payload !== 'object') {
            throw new Error('Invalid encrypted file');
        }
        // WriterSystem emits canonical compact JSON. Requiring that exact
        // representation also rejects duplicate keys, which JSON.parse would
        // otherwise silently collapse.
        if (JSON.stringify(payload) !== rawPayload) {
            throw new Error('Invalid encrypted file');
        }
        const fields = Object.keys(payload).sort();
        if (JSON.stringify(fields) !== JSON.stringify(EXPECTED_FIELDS)) {
            throw new Error('Invalid encrypted file');
        }
        if (
            payload.v !== VERSION ||
            payload.kdf !== KDF ||
            !Number.isInteger(payload.iterations) ||
            payload.iterations < MIN_ITERATIONS ||
            payload.iterations > MAX_ITERATIONS
        ) {
            throw new Error('Unsupported encrypted file');
        }
        base64ToBytes(payload.salt, SALT_BYTES);
        base64ToBytes(payload.file_id, FILE_ID_BYTES);
        base64ToBytes(payload.nonce, NONCE_BYTES);
        base64ToBytes(payload.tag, TAG_BYTES);
        base64ToBytes(payload.ciphertext);
        return payload;
    }

    function isEnvelope(text) {
        if (typeof text !== 'string' || !text.startsWith(`${HEADER}\n`)) return false;
        try {
            parseEnvelope(text);
            return true;
        } catch (_error) {
            return false;
        }
    }

    async function importPassphrase(passphrase) {
        if (typeof passphrase !== 'string' || passphrase.length < MIN_PASSPHRASE) {
            throw new Error(`Passphrase must be at least ${MIN_PASSPHRASE} characters`);
        }
        return crypto.subtle.importKey(
            'raw',
            encoder.encode(passphrase),
            'PBKDF2',
            false,
            ['deriveKey', 'deriveBits']
        );
    }

    async function deriveAesKey(passphrase, salt, iterations) {
        const material = await importPassphrase(passphrase);
        return crypto.subtle.deriveKey(
            { name: 'PBKDF2', hash: 'SHA-256', salt, iterations },
            material,
            { name: 'AES-GCM', length: 256 },
            false,
            ['encrypt', 'decrypt']
        );
    }

    async function deletionVerifier(passphrase, record) {
        const material = await importPassphrase(passphrase);
        const salt = concatBytes(
            base64ToBytes(record.salt, SALT_BYTES),
            encoder.encode('WriterSystem/delete-verifier/v1')
        );
        const bits = await crypto.subtle.deriveBits(
            { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: record.iterations },
            material,
            256
        );
        return bytesToBase64(new Uint8Array(bits));
    }

    async function sealRecord(record, plaintext) {
        const nonce = randomBytes(NONCE_BYTES);
        const encrypted = new Uint8Array(await crypto.subtle.encrypt(
            { name: 'AES-GCM', iv: nonce, additionalData: aad(record), tagLength: TAG_BYTES * 8 },
            record.key,
            encoder.encode(String(plaintext ?? ''))
        ));
        const ciphertext = encrypted.subarray(0, encrypted.length - TAG_BYTES);
        const tag = encrypted.subarray(encrypted.length - TAG_BYTES);
        const payload = {
            ...metadata(record),
            nonce: bytesToBase64(nonce),
            ciphertext: bytesToBase64(ciphertext),
            tag: bytesToBase64(tag),
        };
        return `${HEADER}\n${JSON.stringify(payload)}`;
    }

    async function create(passphrase, plaintext) {
        const salt = randomBytes(SALT_BYTES);
        const record = {
            iterations: ITERATIONS,
            salt: bytesToBase64(salt),
            file_id: bytesToBase64(randomBytes(FILE_ID_BYTES)),
            key: await deriveAesKey(passphrase, salt, ITERATIONS),
        };
        return {
            envelope: await sealRecord(record, plaintext),
            record,
            verifier: await deletionVerifier(passphrase, record),
        };
    }

    async function openWithRecord(envelope, record) {
        const payload = parseEnvelope(envelope);
        if (
            record.iterations !== payload.iterations ||
            record.salt !== payload.salt ||
            record.file_id !== payload.file_id ||
            !record.key
        ) throw new Error('Could not unlock file');
        const combined = concatBytes(
            base64ToBytes(payload.ciphertext),
            base64ToBytes(payload.tag, TAG_BYTES)
        );
        let clear;
        try {
            clear = await crypto.subtle.decrypt(
                {
                    name: 'AES-GCM',
                    iv: base64ToBytes(payload.nonce, NONCE_BYTES),
                    additionalData: aad(record),
                    tagLength: TAG_BYTES * 8,
                },
                record.key,
                combined
            );
        } catch (_error) {
            throw new Error('Could not unlock file');
        }
        return { plaintext: decoder.decode(clear), record };
    }

    async function open(envelope, passphrase) {
        const payload = parseEnvelope(envelope);
        const record = {
            iterations: payload.iterations,
            salt: payload.salt,
            file_id: payload.file_id,
            key: await deriveAesKey(
                passphrase,
                base64ToBytes(payload.salt, SALT_BYTES),
                payload.iterations
            ),
        };
        return openWithRecord(envelope, record);
    }

    function remember(path, record) {
        if (!path || !record || !record.key) throw new Error('Invalid key record');
        keys.set(String(path), record);
    }

    function recall(path) {
        return keys.get(String(path)) || null;
    }

    function forget(path) {
        keys.delete(String(path));
    }

    function forgetAll() {
        keys.clear();
    }

    function moveKey(oldPath, newPath) {
        const record = keys.get(String(oldPath));
        if (!record) return;
        keys.delete(String(oldPath));
        keys.set(String(newPath), record);
    }

    root.WriterCrypto = {
        HEADER,
        ITERATIONS,
        MIN_PASSPHRASE,
        parseEnvelope,
        isEnvelope,
        create,
        open,
        openWithRecord,
        seal: sealRecord,
        deletionVerifier,
        remember,
        recall,
        forget,
        forgetAll,
        moveKey,
        keyCount: () => keys.size,
    };
})(window);
