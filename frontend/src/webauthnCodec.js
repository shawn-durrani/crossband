// WebAuthn wire plumbing (#25 slice 2), pure so `node --test` guards it.
//
// The server speaks py_webauthn's JSON (base64url strings); the browser's
// navigator.credentials API speaks ArrayBuffers. These helpers translate in
// both directions and serialise credential responses for the finish
// endpoints. No browser globals beyond atob/btoa (Node has both).

export const b64uEncode = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')

export const b64uDecode = (s) => Uint8Array.from(
  atob(s.replace(/-/g, '+').replace(/_/g, '/')
    .padEnd(s.length + (4 - (s.length % 4)) % 4, '=')),
  (ch) => ch.charCodeAt(0))

// Server JSON -> the object navigator.credentials.get wants.
export function decodeRequestOptions(publicKey) {
  const out = { ...publicKey, challenge: b64uDecode(publicKey.challenge) }
  if (publicKey.allowCredentials) {
    out.allowCredentials = publicKey.allowCredentials.map(
      (c) => ({ ...c, id: b64uDecode(c.id) }))
  }
  return out
}

// Server JSON -> the object navigator.credentials.create wants.
export function decodeCreationOptions(publicKey) {
  const out = {
    ...publicKey,
    challenge: b64uDecode(publicKey.challenge),
    user: { ...publicKey.user, id: b64uDecode(publicKey.user.id) },
  }
  if (publicKey.excludeCredentials) {
    out.excludeCredentials = publicKey.excludeCredentials.map(
      (c) => ({ ...c, id: b64uDecode(c.id) }))
  }
  return out
}

// A PublicKeyCredential assertion -> the JSON the login endpoint verifies.
export function serialiseAssertion(cred) {
  return {
    id: cred.id, rawId: b64uEncode(cred.rawId), type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: b64uEncode(cred.response.clientDataJSON),
      authenticatorData: b64uEncode(cred.response.authenticatorData),
      signature: b64uEncode(cred.response.signature),
      userHandle: cred.response.userHandle
        ? b64uEncode(cred.response.userHandle) : null,
    },
  }
}

// A PublicKeyCredential attestation -> the JSON the register endpoint verifies.
export function serialiseAttestation(cred) {
  return {
    id: cred.id, rawId: b64uEncode(cred.rawId), type: cred.type,
    clientExtensionResults: cred.getClientExtensionResults(),
    response: {
      clientDataJSON: b64uEncode(cred.response.clientDataJSON),
      attestationObject: b64uEncode(cred.response.attestationObject),
      transports: cred.response.getTransports ? cred.response.getTransports() : [],
    },
  }
}

// Human copy for ceremony failures - shared by the lock screen and the
// management panel so the two never drift.
export function ceremonyErrorCopy(err, kind) {
  if (err && err.name === 'NotAllowedError') {
    return kind === 'register'
      ? 'Cancelled or timed out - nothing was enrolled.'
      : 'The passkey prompt was cancelled or timed out. Try again, or use your password.'
  }
  if (err && err.name === 'InvalidStateError' && kind === 'register') {
    return 'This device already has a passkey for this address.'
  }
  return `${kind === 'register' ? 'Enrolment' : 'Passkey unlock'} failed: ${err && err.message ? err.message : err}`
}
