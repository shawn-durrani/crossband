// WebAuthn wire plumbing: base64url round-trips, option decoding, credential
// serialisation, cancellation copy.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  b64uDecode, b64uEncode, ceremonyErrorCopy, decodeCreationOptions,
  decodeRequestOptions, serialiseAssertion, serialiseAttestation,
} from './webauthnCodec.js'

test('base64url round-trips arbitrary bytes, unpadded', () => {
  for (const bytes of [[], [0], [255], [1, 2, 3], Array.from({ length: 33 }, (_, i) => i * 7 % 256)]) {
    const enc = b64uEncode(new Uint8Array(bytes))
    assert.ok(!enc.includes('='), 'no padding')
    assert.ok(!enc.includes('+') && !enc.includes('/'), 'url-safe alphabet')
    assert.deepEqual(Array.from(b64uDecode(enc)), bytes)
  }
})

test('request options: challenge and allowCredentials ids become bytes', () => {
  const out = decodeRequestOptions({
    challenge: b64uEncode(new Uint8Array([9, 8, 7])),
    rpId: 'localhost',
    allowCredentials: [{ type: 'public-key', id: b64uEncode(new Uint8Array([1, 2])) }],
  })
  assert.deepEqual(Array.from(out.challenge), [9, 8, 7])
  assert.deepEqual(Array.from(out.allowCredentials[0].id), [1, 2])
  assert.equal(out.rpId, 'localhost')
})

test('creation options: user.id and excludeCredentials become bytes', () => {
  const out = decodeCreationOptions({
    challenge: b64uEncode(new Uint8Array([5])),
    user: { id: b64uEncode(new Uint8Array([4, 4])), name: 'crossband owner' },
    excludeCredentials: [{ type: 'public-key', id: b64uEncode(new Uint8Array([6])) }],
  })
  assert.deepEqual(Array.from(out.challenge), [5])
  assert.deepEqual(Array.from(out.user.id), [4, 4])
  assert.equal(out.user.name, 'crossband owner')
  assert.deepEqual(Array.from(out.excludeCredentials[0].id), [6])
})

function fakeCred(responseExtra) {
  return {
    id: 'abc', rawId: new Uint8Array([1]).buffer, type: 'public-key',
    getClientExtensionResults: () => ({}),
    response: {
      clientDataJSON: new Uint8Array([2]).buffer,
      ...responseExtra,
    },
  }
}

test('assertion serialisation carries every proof field, null-safe userHandle', () => {
  const out = serialiseAssertion(fakeCred({
    authenticatorData: new Uint8Array([3]).buffer,
    signature: new Uint8Array([4]).buffer,
    userHandle: null,
  }))
  assert.equal(out.rawId, b64uEncode(new Uint8Array([1])))
  assert.equal(out.response.signature, b64uEncode(new Uint8Array([4])))
  assert.equal(out.response.userHandle, null)
})

test('attestation serialisation tolerates missing getTransports (older Safari)', () => {
  const out = serialiseAttestation(fakeCred({
    attestationObject: new Uint8Array([9]).buffer,
  }))
  assert.equal(out.response.attestationObject, b64uEncode(new Uint8Array([9])))
  assert.deepEqual(out.response.transports, [])
})

test('cancellation copy never reads as a failure of the system', () => {
  const cancel = { name: 'NotAllowedError', message: 'op cancelled' }
  assert.match(ceremonyErrorCopy(cancel, 'login'), /cancelled or timed out/i)
  assert.match(ceremonyErrorCopy(cancel, 'register'), /nothing was enrolled/i)
  assert.match(ceremonyErrorCopy({ name: 'InvalidStateError' }, 'register'), /already has a passkey/i)
  assert.match(ceremonyErrorCopy(new Error('boom'), 'login'), /unlock failed: boom/i)
})
