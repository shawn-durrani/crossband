- The app refuses to serve over Tailscale Funnel instead of warning
  about it in the docs (#363). Every few minutes it asks Tailscale
  whether Funnel has its port on the public internet. While it does, the
  app serves nothing but a page that says so and posts one line in chat,
  and it resumes on its own once Funnel is off. Between checks, a
  request on a trusted host without the identity header Tailscale adds
  for tailnet users is refused before the lock screen. Two new settings,
  `funnel_check_s` and `tailscale_identity_required`.
