- Rules that live in both Python and JavaScript are now guarded (#234).
  The Connections console renders the cost-provenance label and the
  onboarding gate the backend ships, instead of keeping its own copies;
  its seat badge and promote wording collapse into the one lifecycle
  module, resolving three quiet drifts in the visible text. A committed
  contract fixture lets the frontend suite assert backend constants, so
  a backend rename now fails a test instead of leaving the two sides
  politely disagreeing.
