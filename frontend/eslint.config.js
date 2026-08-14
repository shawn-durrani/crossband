// The regression guard behind the blank-chat incident (2026-08-14): two
// free identifiers (a bare `msg`, a missing `canDiscard` import) shipped
// to production - the bundler treats unknown names as possible globals and
// the pure-module tests never render JSX, so nothing failed until every
// existing chat blanked at runtime. no-undef is the entire point of this
// config; style stays the humans' business.
import globals from 'globals'

export default [
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.node },
    },
    rules: { 'no-undef': 'error' },
  },
]
