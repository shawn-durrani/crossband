- Handing a chat to memory no longer stalls silently when membro gates
  its job routes (#298). The status poll now carries the same owner
  token the search call sends, and a refusal fails loudly at once
  instead of spinning for fifteen quiet minutes per chat.
