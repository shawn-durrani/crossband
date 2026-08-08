// The gate's view rule (#25), pure so `node --test` guards it (house rule:
// rules live in .js modules, not buried in components).
//
// The server's /api/auth/session shape: { enrolled, authenticated, passkey }.
// `authenticated` is true on an unenrolled install by design (the gate is
// enrolment-activated), so "open" covers both the unlocked and the
// never-locked cases; the AuthGate banner handles nudging enrolment.

export function gateView(session, webauthnAvailable = false) {
  if (!session) return 'probing'
  if (!session.enrolled || session.authenticated) return 'open'
  if (session.passkey && webauthnAvailable) return 'passkey'
  return 'password'
}

// Client-side validation for the setup/reset form: mirrors the server's
// minimum so the round trip is a confirmation, not the first check.
export const MIN_PASSWORD_LEN = 8

export function setupProblem({ recovery, password, confirm }) {
  if (!recovery) return 'Paste the recovery secret first.'
  if ((password || '').length < MIN_PASSWORD_LEN)
    return `Password must be at least ${MIN_PASSWORD_LEN} characters.`
  if (password !== confirm) return "The two passwords don't match."
  return null
}
