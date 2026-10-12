- When you talk over a spoken reply and it's unclear whether you meant
  the AIs, like "these are not gonna work" said to someone beside you,
  the app now asks TypeSafe's Jev. Jev answers in about a quarter of a
  second while the reply is paused. If the words were for someone else,
  or just listening, the reply carries on. It needs `TYPESAFE_API_KEY`
  in `.env`, and `interrupt_judge: off` turns it off. Without it, the
  word rules decide as before.
