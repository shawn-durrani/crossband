- Every recall now names the chat it's for (membro#72, contract 1.6).
  Membro binds a guest's facts to the chat they came from and hands them
  back only to that chat, so the ambient recall before each round and a
  model's `recall_memory` call both carry the chat. An older membro
  ignores the two fields and answers as before.
