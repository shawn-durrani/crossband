- A message you send while a round is finishing now lands in the chat
  once (#434). The app holds a message like that and sends it again
  when the round ends, but the server had already saved the first try
  before it noticed the round. The chat then had the message twice,
  and the seats and memory read both. Now the server turns the send
  away before it saves anything, and the retry is the only copy. Slash
  commands still go through while a round runs, and a voice turn's
  speaker name still lands on the one copy.
