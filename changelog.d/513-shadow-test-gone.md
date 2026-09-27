- The voice shadow test is gone, with its second speaker model, the
  `voice_shadow_model` setting and `GET /api/voice/shadow` (#513). The
  session naming's own record of each turn moves to
  `GET /api/voice/sessions`, and shows the name each voice had at the
  time and at the end of its session.
