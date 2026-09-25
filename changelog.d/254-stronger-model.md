- "Think harder" and "research more" can now move a chat onto a
  stronger model (#254). Each seat the cue moves asks its own provider
  for the models your key can use, keeps the ones the price card prices
  that fit the chat, and runs one web search to rank them, so the order
  comes from the search and never from price. Before the switch takes
  effect a line says what moved, where the ranking came from, and what a
  turn like the seat's recent ones costs each way. When no stronger
  model can be found the seat stays put and the line says why. It lasts
  for that chat only, "back to normal" returns every seat to its
  configured model, and `model_step_up` turns it off.
