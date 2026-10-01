# Changelog

House convention: one entry per user-visible change, newest first. Keep an
entry to a short paragraph; the issue holds the detail.

## Unreleased

New entries land as one file each in `changelog.d/`; a release folds
them in here, newest first.

## v0.3.0 (2026-10-02)

- When you ask the AIs for advice, a recommendation or ideas, they now
  build the answer on what memory holds about you. That's what you own,
  what you like and what you're working on, where they used to give
  advice anyone could get. When a detail is missing, like where you are,
  they give that answer first and ask for the detail after it. They still
  leave out remembered facts that have nothing to do with what you asked.

- The voice rig can lay a recorded room under a conversation, as well as
  its made-up cafe and road noise (#416). ElevenLabs' sound effects
  model makes each room once, a cafe full of people talking and a
  kitchen with a fan, a tap and dishes going, and the rig keeps it like
  the lines it speaks. Two new scripts play earlier conversations again
  over them. Making the rooms needs the sound effects permission on the
  ElevenLabs key, and without it those scripts are left out of the run
  and the report says why.

- The voice rig now scores the naming pass that ends a voice session,
  and the answers to "who's this?" (#416). Its second copy of the app
  ends a voice session after 45 quiet seconds, through a new
  `voice_session_idle_s` setting that stays at 10 minutes for you, and
  the report sets the names after that pass beside the names when the
  conversation ended. Two new scripts let a voice nobody knows talk
  until the app asks who it is: in one the owner says it's a TV, and in
  the other names the person and spells the name. The report says
  whether the right turn took the answer, the voice's earlier turns were
  relabelled and a clip was saved. Anyone the app meets in a
  conversation is forgotten when it ends, so every conversation starts
  knowing the same people.

- When a seat writes a few words, looks something up and then carries
  on, the two parts no longer run together. A finished sentence before
  the lookup now has a new paragraph after it, and one that stopped mid
  sentence gets a space. What you see, what you hear and what the chat
  saves are the same words.

- The app's own search of your saved chats now tells membro it was
  automatic, the way its recall before every reply already does.
  Membro's access log and its live view then show it as the app
  preparing a reply, not a model choosing to search. A seat's own
  `search_history` call is unchanged.

- When you ask about yourself or your past, the app now searches your
  saved chats itself instead of leaving it to the seats, which often
  skipped it and said memory held nothing (membro#136). The search
  starts beside the recall, the moment you stop talking in a voice chat.
  When the facts recall found have little on your question, every seat
  gets the matching messages too. If the search is still running when
  the first reply is due, a voice seat opens with a short line such as
  "Let me go deeper into our memories" and a typed chat shows "Searching
  past chats", so the wait is never silent. It runs only on your own
  turns, typed or in your voice, never on a guest's, an unnamed voice's
  or the TV's (crossband#588). The seats can still search for those
  themselves. A turn that doesn't ask about you is unchanged.
  `history_prefetch: false` turns it off.

- A search of your past chats now stays useful in the turns after it.
  Every later turn re-reads what a seat's tools found, and a search used
  to be cut to its first 1,200 characters there, which held two or three
  of the longer hits and often stopped partway through one. It now keeps
  about the top ten hits, each whole, and says how many it left out. The
  header's context estimate counts it the same way. A new setting,
  `search_log_chars`, sets the size.

- A search of your past chats now shows the seats up to 600 characters of
  each matching message, up from 300, so the longer excerpts membro sends
  reach the detail the search was for. When the hits don't all fit in one
  tool result, each one shown is whole and the result says how many were
  left out, where the last one used to stop partway.

- A new report says how often the memory summary repeats between
  Claude seat calls, and what caching it in a block of its own would
  save at each model's rate card (#565). Each call now records a
  fingerprint and the length of the summary it sent, never its text.
  Run `.venv/bin/python scripts/summary_reuse_report.py` for the last
  seven days. It reads the database and changes nothing.

- The Spend page now counts model calls cut off partway (#576). A reply
  you talked over, one that stalled or one the provider dropped reported
  no usage, so it counted nothing though you paid for the input it had
  read. It now counts what the provider had reported when it stopped. A
  line under the Spend page's detail says how much of the total that
  is, and the reply's own cost in the chat reads "at least".

- A model that uses a tool and then writes nothing, or passes, keeps the
  record of what its tools did (#575). The app saved no message for such
  a reply, so the tool activity you saw during the round was gone after
  a reload, and the other models never saw the results. It's now saved
  as a message with no words and its tool calls, and so is a call that
  fails or that you talk over after its tools ran.

- A model that repeats its chat's secret context marker no longer shows
  it on screen, or says it aloud in a voice chat, while its reply
  streams (#574). The saved reply was already clean, and now the live
  reply and the tool activity beside it are too. Only the few
  characters that could be the start of the marker wait for the next
  piece of the reply, so nothing else is slowed down.

- Prompt cache health on the Spend page now judges each model on the
  share of all its input read back from the cache (#561). It used to
  compare cache reads with cache writes only, which left out the input
  sent at full price, so a seat reading only 62% of its input from the
  cache showed as healthy. Now 80% or more is healthy, 50 to 80% is one
  to watch, and under half is poor, and GPT seats get a real verdict
  too. Each row shows what was read, written and sent at full price.

- A restart no longer makes every seat write its cached prompt again
  (#562). The secret marker that vouches for the app's own context block
  used to be made fresh at each start, and the cached part of every
  prompt names it. Each chat's marker now comes from a key the app keeps
  in `data/context_marker.key`, readable only by your account, so it
  stays the same across restarts. A model that repeats its marker has it
  taken out before the reply is saved or a tool runs.

- A table in a chat message keeps its words whole on a phone. It used to
  break them into single letters to fit the screen. Now a wide table
  scrolls sideways inside its own box, and the page stays the width of the
  screen. A table in your own message does the same.

- The models' tool list no longer changes mid-chat (#564). It used to
  lose the Claude Code summons while a job was running, the memory tools
  when the memory service didn't answer, and an outside tool server's
  tools when it dropped, and each change made every seat write its whole
  cached prompt again. The list now follows only the chat's switches and
  what's installed. A tool that's unavailable right now answers the call
  with a reason instead: the summons says a job is already running,
  memory says nothing was read or saved, and an outside server says it's
  disconnected.

- The Spend page now counts every model call you pay for (#560). A
  model's `[pass]`, a first try the app turned down and asked for again,
  and an empty reply were all paid calls that left no message, and the
  app only recorded cost on messages, so about a quarter of Claude seat
  spend never showed. Each is now recorded without a message, so a pass
  stays invisible in the chat while its cost counts in the Spend page,
  the chat's running cost and the prompt cache numbers. The detail view
  says how much of the window went on them.

- Tapping a text field on a phone no longer leaves the page zoomed in.
  iOS Safari zooms in on any field under 16px and stays there, so every
  field you type into is now 16px on a touch screen. The desktop keeps its
  smaller fields. The Voices page's rename row wraps on a phone instead of
  running past its card.

- When the voice tracker splits one person into two voices and both
  have said a few seconds, the app now joins them back into one voice.
  The turns the second voice spoke take the person's name, and so does
  everything it says after. The bar is strict: two voices that talked
  at the same time, or that carry two different names, never join, and
  an ordinary conversation joins nothing.

- Correcting or confirming the name on a long spoken turn now teaches
  the app from the part of the turn with the most clear speech. It used
  to learn from the turn's last part, which could be a single word. An
  introduction said in a long turn is learnt the same way.

- Saying you're heading out and will be back, like "heading to the
  shops, back in ten", no longer turns room mode off. The spoken intent
  check read it as you being on your own now in every run. Room mode
  off still needs "solo mode", room mode off by name, or saying you're
  on your own.

- A sound under a second that transcribes to nothing no longer saves the
  voice diagnostics by itself. A cough or a door can do that, nothing is
  lost, and those files were false alarms that used up the three
  automatic saves a page gets. It still shows in the diagnostics and the
  service log, and every other stuck turn saves as before.

- A voice the app splits off from another, because its speech plainly
  belonged to someone else, now shows as the next voice number in a
  two-voice turn, like "Voice 9", instead of "Voice 100".

- You can ask for a measurement in a chat now, typed or out loud (#407).
  Say "run the recall replay" and a seat starts it with the Analysis
  page's own runner, telling you what it costs and what it touches as
  it starts. When it ends, a line in the chat gives the headline and
  what it spent, with a link that opens the report on the Analysis page,
  and one seat passes the result on. A real run starts only when you
  asked, typed or in a voice the app matched to your name, and only
  once an owner password is set. A guest, a voice nobody could name or
  the TV gets a free practice run offer and the reason instead. At most
  three real runs start from chats each day, and the report never goes
  into the chat.

- A one-word reply the voice tracker files under the wrong voice no
  longer takes that voice's name. When nothing in a turn is long enough
  to fingerprint, the app now scores the whole turn against everyone's
  saved voice. It leaves the turn unnamed when it plainly isn't that
  voice's person and sounds like someone else the app knows. The voice
  keeps its name, and a reply that sounds like nobody the app knows
  keeps it too. It adds about 40 to 50 ms to those short turns only.

- A long voice turn is named from all its pieces even when a piece went
  by the backup recording. The backup upload now says which piece came
  before it, as live transcription already did. Before, a piece live
  transcription never heard started a turn of its own, so the name came
  from that piece alone.

- Turning live transcription off and on again quickly no longer leaves
  a second microphone open. The old connection's close could land after
  the new one opened and cut the app loose from it. The new connection
  then stayed open on the server, your words on it went unheard, and a
  third one opened. An old connection's close now changes nothing once
  a newer one has taken over.

- A new Analysis page in the sidebar runs the eval measurements from the
  app, on the Mac or your phone (#407). The critic eval, the attribution
  replay, the recall replay, the spoken intent check and the voice rig
  each state what they cost and what they touch before their Run
  button, and a free practice run checks a harness with made-up
  numbers. A run keeps going in the background with a status line, a
  deploy waits for it, and every report stays on the Mac in owner-only
  files, listed newest first. Only a signed-in owner can use the page,
  even before a password is set, and the recall replay never writes the
  words of your chats from it. Each harness also takes `--json-out` to
  write its JSON report beside the one it prints.

- When the voice tracker files one person's turn under someone else's
  voice, and their own voice hasn't said much yet, the turn no longer
  takes the other person's name. Each voice's speech in a turn is now
  scored against everyone's saved voice on its own. When it plainly is
  someone else, it moves to that person's voice, or to a voice of its
  own, before the app learns from it. The bar is strict, and an ordinary
  conversation moves nothing. The session rows list every move.

- The data folder is private to your own account on the Mac (#542).
  The chat database, its backups, attachments, voice diagnostic dumps
  and the service log could be read by any other account on the machine.
  At startup the app now takes that access away from everything already
  in the folder, and every file it makes after that is private from the
  start. The stored-pass repair script does the same for its safety
  copy and journal.

- Live transcription comes back on its own after it fails. A network
  blip used to switch the voice session to the slower backup
  transcription until the page was reloaded. It's now tried again after
  10 seconds, then after longer waits if it keeps failing, and the
  banner goes once a turn is transcribed live again. A bad key or a used
  up quota still switches it off for the session.

- When the app asks "Someone new is talking. Who's this?" about a TV,
  a radio or a video, you can answer "that's the TV" (#523). That voice
  is then ignored for the rest of the voice session: it's never named,
  seated, asked about again or learnt from, and nothing it says is
  taken as an instruction. Its turns stay in the chat with no name, and
  the AIs read them as background audio, not a person in the room. The
  question itself now says you can answer this way, and a tap on one of
  its turns still names it if you got it wrong.

- A restart or a deploy no longer signs you out (#471). Sign-ins used
  to live only in the server's memory, so every deploy dropped a phone
  mid-call to the lock screen. They're now kept in the database as a
  hash with their 24 hour expiry, never the cookie itself, so a copy of
  the database can't sign anyone in. Signing out still ends that
  sign-in, and resetting the password still ends every one. Removing a
  passkey now ends every other sign-in too, the way a restart used to,
  so a lost phone's sign-in goes with its passkey. Backups carry no
  sign-ins, so restoring one signs every browser out. The first start
  on this version signs everyone out once.

- With the calibrated scorer on, the checks on stored voices use it too
  (#523). The hygiene guard sets a stored clip aside when the scorer that
  names voices hears it as someone else, instead of going by the older
  matcher's average fingerprint. A clip's saved score is now the chance
  its voice is that person, and a voice whose human-backed clips have all
  rotated out is paused when its middle clip is under 0.5. A clip saved
  with the older matcher's score is rescored once, after the scorer's
  next build, and keeps its old score beside the new one. The rescore
  never deletes, moves or sets aside a clip. With the scorer off, both
  checks work as before.

- A long voice turn rescued after a live transcription failure no longer
  repeats its first part. The backup recording covers the whole turn, so
  its words used to include the pieces already sent. The app now says
  where those pieces end, and only the words after that point are kept.

- You can answer "Someone new is talking. Who's this?" out loud (#523).
  Say "that's Dave", or let the new person say "I'm Dave", and it does
  what tapping the turn does. The turn is named Dave, the voice keeps
  that name for the rest of the session and its other turns take it, and
  the turn's audio becomes Dave's first voice clip, kept as an
  introduction. The new voice names itself only in its own words, and a
  voice the app hasn't named yet can't answer for it, since it might be
  a second new person. Typing the answer works too.

- Live transcription stays connected through quiet stretches.
  ElevenLabs closes a realtime connection that hears nothing for about
  15 seconds, so the app used to reconnect after every pause, and a
  turn spoken just then lost its start or took the slower backup way.
  The app now sends a sliver of silence while the room is quiet. If the
  connection is replaced mid-sentence anyway, the turn is sent whole
  from the backup recording. The log says why each backup turn happened.

- When a voice session ends, after 10 minutes of quiet, the app names
  every voice in it once more and relabels any turn whose name changed.
  That includes the session's last turn, which nothing relabelled
  before. A turn whose voice ends the session unnamed keeps its label.

- When the voice tracker splits one person into two voices, or gives a
  stretch of one person's speech to someone else, the app now moves that
  stretch to the voice it plainly belongs to before learning from it, so
  the turn takes the right name. The bar is strict, and an ordinary
  conversation moves nothing. The session rows list every move with its
  scores.

- A long spoken turn is now named from all its pieces, not just the last
  one. The app sends a long turn in pieces of about 12 seconds, and a
  last piece too short to recognise used to leave the whole turn "still
  listening" even when an earlier piece named the speaker clearly. The
  turn now takes the voice its pieces had together, and pieces spoken by
  different people make it a two-voice turn rather than giving it one
  name (#469).

- On an iPhone with iOS 17.1 or later, a spoken reply starts playing as
  its first audio arrives, as it does on a Mac (#520). The phone used to
  wait until the whole reply had been turned into speech, which left 3 to
  37 seconds of silence before a reply in the owner's traces. It now
  streams through Safari's ManagedMediaSource. If that stream fails
  before a word is heard, the reply plays whole once all of it has
  arrived, and an older iPhone plays every reply that way.

- Talking over a reply on an iPhone stops it straight away, and what you
  say is heard and sent (#518). The phone holds a reply's audio until the
  whole reply has arrived, and a cut couldn't reach it while it waited, so
  the reply kept the mic shut and your words went nowhere. Talking over a
  reply now stops it once, where it used to ask the server to stop the
  round again on every frame of your speech.

- A reply you cut off never plays later (#518). On an iPhone, switching
  voice off after talking over a reply played that reply about half a
  minute late, with the session already over. Audio that turns up for a
  cut reply, or after voice is off, is now thrown away, and so is a reply
  that was queued behind the one you cut.

- Asking for research no longer moves any seat to a stronger model
  (#516). "Do some research", "find me an answer", "look it up" and
  "research this properly" turn research mode on, and every seat keeps
  the model it's on. A seat moves only when someone asks for a stronger
  model, with "use your best model", "use a stronger model" or a
  standing "think harder".

- A line about a seat's model now says what the next reply runs on
  (#516). A move to a stronger model waits for any reply already on its
  way, so the reply after the line is the first on the new model. A move
  found for a seat whose model changed meanwhile, or after "back to
  normal", is dropped without a line. Changing a seat's model on the
  Models page ends a chat's step-up, and one line before the seat's next
  reply there names the model it's on now.

- A reply on an Eleven v3 voice no longer goes quiet while the AI
  searches the web. A finished sentence is spoken straight away, where
  it used to wait for the next word, which kept one voice silent for 16
  seconds on 27 September.

- The voice shadow test is gone, with its second speaker model, the
  `voice_shadow_model` setting and `GET /api/voice/shadow` (#513). The
  session naming's own record of each turn moves to
  `GET /api/voice/sessions`, and shows the name each voice had at the
  time and at the end of its session.

- Every spoken turn is named one way now, in every mode (#513). The app
  follows each voice through the voice session on your computer, names
  each voice from everything it has said, with the calibrated scorer
  once it's ready, and names every turn before the AIs read it. A voice
  named later fills in its earlier turns, a new voice is asked about
  once, and naming a turn by hand names its voice for the rest of the
  session. With no diariser set, or with it down, each turn is named on
  its own. The settings `voice_session_shadow`, `voice_session_labels`,
  `voice_session_live`, `voice_session_only` and `voice_id_pending_extra`
  are gone: what the switches turned on always runs, and a config file
  that still names them loads as before. With `voice_id_enabled` off, no
  spoken turn is checked or named.

- The cross-check that flags a name the words don't fit runs on named
  voice turns again (#513). A turn named as a guest gets it in every
  mode, and a turn named as you gets it while the room is on. It never
  changes a name.

- Your first spoken introduction still gives your own voice its first
  clip, now from the introduction's own turn (#513). It waits a moment
  for the voice check to finish with that turn, and it never takes the
  clip from a turn the check heard as someone else.

- A voice the app knows well now settles and keeps its best clips. Once a
  bank is established, an automatic clip gets in at most once a week, and
  only when it comes from a day the bank doesn't hold yet or beats the
  weakest automatic clip, which it replaces. Established means the
  readiness test says ready when `voice_calibrated_scorer` is on, and
  otherwise enough speech in at least 10 clips from at least two days.
  Clips from an introduction, a correction, a recording or a move always
  go in, and a bank that isn't established learns as before. A refused
  clip is counted with the reason "voice is settled", and the voice
  dock's learning line says "settled".

- A clip the app drops from a voice now leaves membro too (memory
  contract 1.8). Membro kept every clip it was ever sent, so it held far
  more of each voice than the app uses. When rotation drops a clip, a
  settled bank replaces one, or the hygiene guard sets one aside, the
  next sync deletes membro's copy through the same ledger as your own
  deletes, with the reason, so a rebuild can't bring it back. A set-aside
  clip goes back up if a later check reinstates it. Each sync also sends
  membro the list of clips every voice keeps, so its People page can
  count the rest and delete them when you press its button. A 1.7 membro
  gets no list and still takes the deletes. Deploy after membro.

- A spoken turn two people talked in is labelled with both of them, and
  the words under it are split by who said them, on your computer. No
  voice clips go to the cloud for it, and memory never files such a turn
  under one person's name (#510).

- You can now record someone's voice from the Voices page (#504). Press
  "Record their voice" beside a person and have them read the short
  passage shown, about 30 seconds, with the same microphone setting a
  voice chat uses. The speech is kept as introduced clips, so the bank
  counts as vouched and keeps them through rotation. Straight after, the
  page says whether the voice is ready, and when it isn't, what's
  missing, such as another day or another room. Noise, silence and very
  short recordings are turned away with the reason.

- The mic uses one setting in every mode (#505). In solo the browser used
  to clean out background noise and even out your volume, and in room
  mode it didn't, so a voice saved in one mode sounded different in the
  other. Every mode now keeps echo cancellation on, so the AIs' own
  voices stay out of the mic, and leaves noise suppression and automatic
  gain off, as room mode always did. Changing mode during a call leaves
  the mic as it is.

- The transcriber and the voice checks hear cleaner audio (#505). The
  browser brings the mic down to the 16 kHz they read, and it used to do
  that by keeping one sample in three, which folded hiss, taps and other
  high sounds into the speech range as sounds nobody made. It now filters
  them out first, for the live stream and for the backup copy of a turn.

- The first voice turn after a restart gets its session name in time
  (#482). The app loads everyone's saved voices at startup and when a
  voice chat begins, so that first turn no longer waits five seconds
  while it does.

- When you've asked the AIs to stay quiet, people in the room asking each
  other things no longer makes one of them speak. A message with a
  question mark in it still asks the first AI to look again, and a second
  `[pass]` now stands, so it no longer answers an old question or explains
  why it's passing. A short "that wasn't a question for me" before a
  `[pass]` counts as the pass too. An AI you name still has to answer
  (#497).

- A word you spell out in a game is no longer heard as a name
  correction. Spelling a word letter by letter, like "is Z-O-O-S a
  word?", could post "Heard a name correction, and nothing changed", and
  with a guest talking just before, it could have renamed them to the
  word. Now spelt letters only count as a name when the turn says it's a
  name, like "her name's spelt S-A-M-M", or says the name and then spells
  it, like "it's Mateo, M-A-T-E-O". A word set aside posts nothing in the
  chat (#494).

- A saved voice you've approved no longer gets set aside again just
  because its matches score a little under 0.65 (#499). The pause now
  starts under 0.6, the same bar a clip must clear to be saved, so a
  voice that saves clips cleanly keeps being named in every chat.

- On Eleven v3, a reply now goes to ElevenLabs a whole sentence at a
  time, so no stretch of speech starts or stops mid-sentence (#493).
  The first word waits for the first sentence to end, which added a
  median of 0.09 to 0.18 seconds in testing. Set
  `tts_v3_sentence_chunks` to `false` to send each piece as the model
  writes it. Every other model is unchanged.

- Eleven v3 voices now speak at Robust stability, the steadiest of v3's
  three modes, so a reply's accent drifts less (#493). Set
  `tts_v3_stability` to `natural` in `config.local.json` for the sound
  v3 had before, or `creative` for the most expressive. Every other
  model is unchanged.

- A new `tts_v3_accent_tag` setting puts an ElevenLabs audio tag, such
  as `[Australian accent]`, in front of every piece of a v3 reply to
  hold the accent (#493). A seat can set its own in its editor on the
  Models page. The tag only goes to ElevenLabs, and it never shows in
  the chat, the transcript, memory or captions. It starts empty.

- In a room, the AIs stop telling you they can't hear and stop reciting
  what the voice check said. Asked who's talking, they give the name, or
  say they don't know yet, and explain how they know only if you ask.

- The Voices page can now say whether each stored voice is ready, which
  means the app should name that person on a day it hasn't heard them
  yet. It cuts their stored speech into 2 second pieces, leaves each
  day's clips out in turn, and checks those pieces are still named as
  them, and never as anyone else. It's off until you set
  `voice_calibrated_scorer`, and then it downloads a second speaker
  model once, about 26MB. It works in the background and changes
  nothing about how turns are named (#489).

- A spoken turn left unnamed because someone in the room isn't learnt
  yet now reads to the AIs as an unidentified speaker, the way memory
  already filed it, instead of as you (#484).

- A turn you named yourself, by tapping it or because the speaker
  introduced themselves, now reaches memory as your correction or an
  introduction (#484). Facts from it link to that person without
  waiting for review.

- You can now choose which ElevenLabs model speaks replies, on the
  Models page (#480). Each option says what it's good at, and Automatic
  follows the newest model the app can stream live, which today is
  Eleven v3 Conversational. v3 streams through ElevenLabs' dialogue
  socket, since the socket used until now refuses it. A seat can pick
  its own model, a model ElevenLabs refuses falls back to Flash v2.5
  within the same reply, and the voice traces record which model spoke.
  The setting starts on Flash v2.5, the model used until now.

- Room mode keeps more of each voice, and from more days. A bank now
  holds up to ten longer clips and five short ones, and a new clip
  competes first with clips from its own session, so one long evening
  can't push out every other day. A clip from an introduction, a
  correction or a move you made yourself is never rotated out for an
  automatic one. The voice fingerprint is built from the speech in each
  clip with the long pauses left out, and the stored clips don't
  change. Nothing already stored is deleted. A bank that was full shows
  as still learning until it fills to the new size, and with membro set
  up it refills from membro's copies, each keeping the day it was
  recorded (#477).

- You can now tell room mode a turn's name is right and have it learn
  from that turn. The "Who spoke this?" menu on a named turn starts with
  "Yes, that's Sam: learn from this", which keeps the name and adds the
  turn to Sam's voice, however close to the bar it scored. A remembered
  voice that says its own name ("this is Sam") is learnt from the same
  way, beside any rename. When the recording has already gone, the turn
  says so. A turn longer than ten seconds now keeps its best ten
  seconds of speech instead of its first ten (#477).

- Spelling a word out letter by letter no longer counts as a name
  correction. When the transcript got a word wrong and you spelt it
  out, the app heard a correction and posted "Heard a name correction,
  and nothing changed". With a guest talking just before, it could
  have renamed them to the word. Now only a person's name spelt out
  counts, and its letters are joined into the name instead of being
  kept as a second spelling (#474).

- The first thing you say after the app restarts can now be named.
  The voice matcher used to load only when the first voice turn needed
  it, and that turn went unnamed while it loaded. The app restarts on
  every update, so this happened after each one. The matcher now loads
  in the background while the app starts, as long as voice
  identification is on and its model is already downloaded (#473).

- The models answer questions about the room from what's true now
  (#460). With each reply, every model is told whether room mode is on,
  whether it can switch itself back on, who's in the room, and whose
  voices the check put on the last few spoken turns. When the room goes
  off, the note says names already on earlier turns still stand, so a
  guest whose turns carry her name is no longer called unidentified.
  The models bring up the room only when someone asks, and never
  confirm or deny a room switch you've just asked for, since the app
  posts its own line for that.

- A model that writes a short "nothing to add" or "staying quiet" and
  then `[pass]` has passed, so nothing is saved, shown, spoken or sent to
  memory. Any other reply that ends in `[pass]` keeps its words and loses
  the token, on screen, in Copy chat and in what's spoken. Replies saved
  that way before this change show the same way now (#460).

- In a room with two people, every spoken turn now gets the voice
  check, and a new voice gets asked about. About half of what was said
  used to reach the seats and memory as yours, whoever said it. A turn
  transcribed by the backup path, after live transcription failed, was
  never checked. It now goes with a small copy of its audio for the
  check. After "solo mode" nothing was checked at all. Solo now names
  the voices it knows and marks a clear stranger "voice not
  recognised", without switching the room on or asking. The "who's
  joined?" question, the cross-check that flags a name the words don't
  fit, and the audio tap-to-correct learns from all waited for a label
  the new message already carried, so none of them ran. They run again
  (#461).

- When you switch room mode on or off by voice, the chat now gets a line
  saying what changed and what to say to undo it. Before, the room could
  switch itself off with nothing in the chat to show it.

- Asking the seats to hold back leaves room mode alone. "You don't need
  to respond", "just eavesdrop until we ask" and "go to eavesdropping
  mode, we're just talking" are no longer heard as room mode on or off.
  Each seat answers with `[pass]` until someone asks it something. Solo
  mode now needs you to say it by name, or say you're on your own now.

- A voice turn is sent once after the app locks during a call. A restart
  used to sign the browser out, and when the lock screen took over, the
  call kept running out of sight with its microphone open. Once you
  unlocked and started voice again, two sessions heard you, so every turn
  reached the chat twice and each seat answered it twice. The call now
  ends when the lock screen appears, and starting voice ends any older
  session on the page first.

- A voice remark that runs 10 to 12 seconds is now sent when you stop
  talking. The app cuts long speech into pieces about 12 seconds in and
  joins them into one message, and the cut could land on the pause at the
  end of your turn. The app then waited for the rest of a longer turn that
  never came: the screen stayed on Listening, and your words only went out
  with whatever someone said next. Now the same pause that ends a short
  turn ends a long one after a cut, and every piece is sent once, even if
  its words are still being transcribed. Muting right after a cut sends
  the turn too. In a long turn, an earlier piece's words arriving late no
  longer flip the screen back to Listening or switch off the backup for
  the last piece (#453).

- A voice turn is no longer lost when realtime transcription fails while
  it's still being transcribed (#304). If the transcription service
  errored after you'd finished speaking but before your words came back,
  the app switched to standard transcription and dropped that turn: red
  errors, then Listening, and someone had to speak again. Now the copy the
  app records alongside is transcribed instead and the turn is sent once,
  even if the realtime words turn up late, and the screen stays on
  Thinking until it's done. A long turn whose last piece comes back empty
  now sends the parts already heard, rather than holding them until
  someone speaks again. On a phone, "save voice diagnostics" is now on the
  call screen: tap the voice line under the agents to find it.

- A model's pass stays hidden when you talk over it (#456). A model
  with nothing to add replies `[pass]` and the app hides the turn, but
  if you interrupted it while it was still writing that, the app saved
  what it had so far. The chat then showed a message reading `[pass`
  with the cut-off note, and memory kept it. Now a reply cut off while
  it could still become `[pass]` counts as a pass, and nothing is saved.
  On screen, a pass no longer flashes up as brackets while it streams,
  and a model you cut off before it wrote anything no longer leaves its
  name with nothing under it. Copy chat leaves those turns out too, so
  it no longer prints "(no text)" rows for models that stayed quiet.

- Your other apps are one tap away (workbench#100). A thin row at the
  top of every page links Membro, Spendglass and Threadfold. Crossband
  asks each one on this computer where a browser can open it, keeps the
  answers for a minute, and leaves out any app that doesn't answer. On
  the Mac every running app shows. From your phone only the apps served
  on your tailnet show, so the row never offers a link that won't open.
  Links open in a new tab, so a live voice call keeps going.
  `/api/auth/session` now also answers `app` and `browser_origin`, and
  two new settings, `browser_origin` and `sibling_apps`, cover an
  unusual serve and a different set of apps.

- Backups no longer stall while the Mac sleeps. The six hours between
  automatic snapshots used to count only time the Mac was awake, so a
  laptop that slept most of the day could go days without a new restore
  point. The timer now checks every five minutes and goes by the clock,
  so a snapshot that fell due during sleep is taken within five minutes
  of the Mac waking. A copy identical to the newest snapshot is still
  thrown away. Setting `backup_interval_hours` to `0` now turns the timer
  off, where it used to copy the database over and over (#447).

- A fact a model saves while a guest is in the room now stays in that
  chat once you approve it (membro#115, contract 1.7). Every direct
  `save_memory` carries the chat it was made in, the same pair ingest
  and recall use, and membro binds a guest-present save to it. A save
  made with only you in the room is recalled in every chat as before.
  An older membro ignores the field, so its guest-present saves stay
  global until it's updated.

- A message you send while a round is finishing now lands in the chat
  once (#434). The app holds a message like that and sends it again
  when the round ends, but the server had already saved the first try
  before it noticed the round. The chat then had the message twice,
  and the seats and memory read both. Now the server turns the send
  away before it saves anything, and the retry is the only copy. Slash
  commands still go through while a round runs, and a voice turn's
  speaker name still lands on the one copy.

- Crossband now pins the Anthropic Python SDK to its 1.x line, at
  1.8.0 or newer. The requirement carried no version, so a fresh install
  and CI already got 1.8.0 while an older install kept the 0.x release
  it first installed, and the two could quietly differ. Nothing in
  crossband's own code needed changing for 1.x. Chat replies, streaming,
  the model list and the small utility calls work as before. An existing
  install moves to 1.8.0 on its next start, because `start.sh`
  reinstalls the Python packages whenever `requirements.txt` changes.

- The seat ledger now flags a reply that copies another seat's reply
  as well as a seat repeating itself (#162). A reply that matches one of
  the chat's last 12 replies from any seat is marked with the seat and
  round it copied. The match ignores case, spacing and a name label
  copied onto the front. The echo guard's catches land in the ledger
  too, naming whose reply was restated, which matters most in voice,
  where the guard can only log. Nothing changes about the reply itself:
  it still posts, speaks and reaches memory. Read the flags at
  `GET /api/models/seat_trace` or in the voice diagnostics dump.

- "Think harder" and "use your best model" can now move a chat onto a
  stronger model (#254). Each seat the cue moves asks its own provider
  for the models your key can use, keeps the ones the price card prices
  that fit the chat, and runs one web search to rank them, so the order
  comes from the search and never from price. Before the switch takes
  effect a line says what moved, where the ranking came from, and what a
  turn like the seat's recent ones costs each way. When no stronger
  model can be found the seat stays put and the line says why. It lasts
  for that chat only, "back to normal" returns every seat to its
  configured model, and `model_step_up` turns it off.

- A point release of a priced model no longer borrows the older
  model's price (#254). The price card read a one-digit version as a date
  stamp, so a new point release recorded the older model's rates, which it
  may not share. A stamp now needs four digits or more, so a point release
  with no row of its own records unknown cost until you price it on the
  Models page, and a dated reissue such as `claude-haiku-4-5-20251001`
  still prices as before.

- Seats on Claude Opus 5.5, Fable 5.1 or Mythos 5.1 now record the right
  cost. The price card had no rows for them, so Opus 5.5 was charged as
  Opus 5, at $5 and $25 per million tokens instead of $4 and $20. All
  three also paid the standard cache-read rate, when Opus 5.5's is half
  that and Fable 5.1's is a quarter. The card's Anthropic prices were
  checked against the pricing page on 25 September 2026.

- When voice gets stuck, the app now saves the voice diagnostics by itself
  (#304). If a turn you finished still hasn't reached the models 30
  seconds later, or a round goes quiet and never ends, the app saves the
  same file the "save voice diagnostics" button does and shows one quiet
  line saying where it went. Like the button, it keeps what the app was
  doing and never what anyone said. It saves at most once every ten
  minutes and three times per page load, so a patchy connection can't
  fill the folder.

- With room mode off, the voice tray shows only you (#306). It used to
  list every voice the app remembers, each with a tick, so a call with
  nobody else in it read "Listening · Alex ✓ +3" on a phone and looked
  like a list of who was there. The tick only ever meant enough of that
  voice had been learnt. Now the tray and the phone's one-line summary
  show your own chip, with a tick once your voice is remembered, and no
  chip until the app has started learning it. Everyone it remembers is
  still listed on the Voices page and in the desktop voice settings.
  With room mode on, the tray shows the people seated in the room, as
  before.

- "Research more" now turns on a research mode for the chat (#253). Say
  it, or "look into that properly," or "go deeper," and a system line
  names who asked. For the rest of that chat the seats get a bigger
  tool budget and a routine to match: plan the searches first, weigh
  what comes back, say plainly when the evidence falls short, and
  finish with a written answer that cites its sources, memory included.
  "Back to normal" turns it off, the running-cost line says what it
  spent while it ran, and a new chat always starts at the defaults.
  Before this, the app heard the cue and said the mode wasn't built.

- When the app can't name a voice, the seats can now say why (#411).
  A room turn the voice check looked at and couldn't name used to read
  as "identity pending" and, after a few seconds, as you. Now the turn
  carries the check's own reason, "voice not recognised", "too short to
  judge", "too close to call" or "no voices learnt yet", the seats see
  it and repeat it when asked, and memory files the turn as a doubted
  guest's rather than yours. The seats are also told that a spoken name
  never names a turn, so "this is Dave" gets an explanation instead of
  an argument.

- Every spoken instruction is now heard, not just the ones on four fixed
  phrase lists (#258, #412). Each turn goes to the cheap utility model
  once, in the background after it is sent. The model says which of a
  room command, an introduction, a departure, a name correction or a
  thinking depth change it holds. Phrasings like "return to defaults",
  "keep it short" and "this voice is for Dave" now land, where they used
  to go nowhere. When the model hears an instruction and nothing about
  the room changes, one system line says so, naming what was heard. A
  research request is heard and recorded too, though acting on it is a
  later build. Costs about a third of a cent per turn, near $8 a month
  at 100 turns a day.

- The spoken instruction gap can now be measured (#258). A new offline
  harness, eval_intent, holds made-up turns graded by hand and reports
  how many instruction wordings today's phrase lists never send to a
  model, and how one merged model call scores against today's four
  gated prompts on the same turns. A keyless mock run checks the
  harness and the dropped-turn count is real in it; the comparison
  needs a key. Nothing the app hears changes yet.

- A seat can now ask Claude Code to run a command and report what it
  printed (#404). The new run mode has a shell for the project's own
  commands, such as the tests or a harness, and no way to edit, commit,
  push or open a pull request. Before, a "run this and show me" ask
  either landed in investigate mode, which has no shell, or needed
  implement mode for a read-only job. The tool now tells the seats
  that investigate cannot run anything.

- An escalated chat now says what the escalation is costing (#259). While
  a seat sits at deep or maximum thinking, the chat gets one short system
  line every 30 messages naming that seat, when it was raised, and what it
  has spent since, as a rate-card estimate with subscription-covered use
  kept apart, never a combined total. The line stays out of memory, and
  `spend_note_every` sets how often it comes, with 0 turning it off.

- A spoken change of thinking depth now names whoever spoke it (#255).
  In room mode the transcript line and the note each seat reads say the
  guest who asked, by their preferred name, and say nobody when the app
  can't tell who spoke: a doubted or crosstalk turn, an unnamed voice, or
  a label that hasn't landed yet. Before, every escalation was recorded
  as the owner's, whoever said it. The line also stops claiming that only
  the person who set a depth can clear it, since anyone can.

- The ambient memory recall can now be measured on your own install
  (#252). A new offline harness, eval_recall, replays your user turns
  through the same recall a round runs, checks each fact against the
  standing summary, and reports a floor sweep, a per-rank table and the
  latency, so the count and the floor can be tuned on numbers. A keyless
  mock run checks the harness; a real run needs membro up. Nothing in a
  live round changes.

- Every recall now names the chat it's for (membro#72, contract 1.6).
  Membro binds a guest's facts to the chat they came from and hands them
  back only to that chat, so the ambient recall before each round and a
  model's `recall_memory` call both carry the chat. An older membro
  ignores the two fields and answers as before.

- Every seat now knows how hard it's thinking and who can change that
  (#305). A seat with no spoken override used to be told nothing about
  its effort, and one claimed to have changed its own setting, which it
  cannot do. Each seat is now told its configured setting or the spoken
  level in force, that anyone in the chat can change it by saying so,
  and that it can't change it itself. "Use your fastest reasoning
  setting" and its kin now reach the spoken-depth check too.

- A content-free ledger of what each seat's completion did (#162). Per
  completion: the round and seat, time to first token, total time,
  chunk and character counts, the finish reason, how it ended, and a
  short hash of the reply. A reply that repeats an earlier one by the
  same seat in the same chat is marked as a repeat and logged, and a
  doubled send of the same text within ten seconds is written down by
  hash. Read it at `GET /api/models/seat_trace`, or in the voice
  diagnostics dump. Never the text itself.

- The app refuses to serve over Tailscale Funnel instead of warning
  about it in the docs (#363). Every few minutes it asks Tailscale
  whether Funnel has its port on the public internet. While it does, the
  app serves nothing but a page that says so and posts one line in chat,
  and it resumes on its own once Funnel is off. Between checks, a
  request on a trusted host without the identity header Tailscale adds
  for tailnet users is refused before the lock screen. Two new settings,
  `funnel_check_s` and `tailscale_identity_required`.

- A clip you deleted stays deleted through a merge (#353). Moving a
  clip into a person, deleting it out of them and then merging them away
  before a sync used to leave membro holding the clip, and the restore
  step handed it back. The ledger now follows each clip through its
  rows, so the delete looks where the clip actually ended up.

- Empty chats go on their own after two days (#354). A chat with no
  messages that you never renamed or archived, with nobody seated in it,
  is deleted at startup and once a day after that. The log carries one
  line with the count and nothing about the chats. Anything you sent,
  named or put away stays.

- The passkeys list reads cleanly on a phone (#377). Each row's dates,
  sync note and remove link now wrap under the passkey's name instead of
  running off the row and over the one below it.

- Two tests no longer fail on a slow CI run with nothing wrong (#361).
  The restart test for slash-command acks now uses a window a slow
  runner can't cross, and every test gets its own temp directory, so a
  guest worktree torn down late by one test can't wreck the next test's
  worktree at the same path.

- Saying "back to normal" now resets a seat completely (#260). A
  one-reply override parked under a standing depth used to survive the
  reset, so the chat announced the seat was back to its configured
  depth and the very next reply still ran deep. And a seat with only a
  parked override and no standing depth reset to nothing at all, with no
  notice. The plain reset now drops the override too and the notice
  says so. The compound instruction still works: "think hard about just
  this next one, and from now on go back to normal" clears the standing
  depth and keeps the one-reply override.

- docs/GUEST_PERMISSIONS.md is rewritten in the fleet's writing voice.
  It now says what a visit and a worktree are, and what implement mode
  changes, before it lists a single rule. The allow and deny lists are
  two tables, one value and one sentence per cell, and a diagram shows
  how a command goes from the guest through the rules to a run or a
  denial, and on to the merge only you can make. What the rules don't
  protect against has its own section, in plain sentences. Every fact,
  rule, command pattern and path from the earlier page is kept (#342).

- The guide to reading your cost and cache numbers, docs/COST_TELEMETRY.md,
  is rewritten in the fleet's writing voice. Same facts, field names and
  numbers, in shorter, plainer sentences, with each term from the code
  explained before it's used, and one diagram of how a Claude request is
  laid out for the cache. The By source table's utility label and the log
  rotation rule now read as the code has them. Part of the doc rewrites
  (#342).

- The voice identification doc is rewritten in the fleet's writing voice.
  It now opens with how room mode switches on and off, with a diagram of
  the three states a chat can be in, and each word the app uses (seat,
  arm, bank, vouch, cold start) is explained before it's used. Every
  setting, number and limitation is still there, in shorter sentences.
  One of the doc rewrites (#342).

- docs/OPERATIONS.md, the page on keeping the app running, is rewritten
  in the fleet's writing voice. You get the same facts and the same
  commands in shorter, plainer sentences, each term from the app is
  explained before it's used, and one diagram shows the deploy path from
  the chat command to the health check. The doc-style test now holds
  the page to the guide's rules (#342).

- The transcript-shape experiment can now be run (#212). A new offline
  harness, eval_attribution, replays synthetic group chats through three
  projection shapes (today's, own turns labelled, and the member-envelope
  shape) and asks each seat who said what, including about itself. It uses
  the real seat prompt, prices each call, and lists every miss with the
  reply text. A keyless mock run checks the harness; a real run needs the
  provider keys. Nothing in a live round changes.

- A seat can no longer quietly take another seat's words as its own
  (#374). In a group chat each seat sees everyone else's turns labelled
  and its own turns bare, and the rules told it the labels show who
  said what. So a seat looking for who raised something could only
  ever find other people, and a "you" from the owner right after
  another member's turn read as addressed to itself. The rules now say
  both things plainly. The attribution chip also covers two more
  shapes: a seat's own "I said" or "I meant" claim with no match in its
  own messages, and an "only X said" claim when someone else's messages
  carry the same words. Both stay a prompt to check, never a verdict.

- The remote access guide, docs/REMOTE_ACCESS.md, is rewritten in the
  fleet's writing voice. The setup is seven numbered steps, each with
  its command and what you should see after it, and it now says what a
  phone meets on a fresh install: the screen that sets the owner
  password, and where the recovery secret comes from. One diagram shows
  where a request from your phone goes. Every command, setting and
  warning is the same (#342).

- The README is rewritten in the fleet's writing voice, and the guide to
  CONTRIBUTING.md says what that voice is. You get the same facts
  in shorter, plainer sentences, and a term from the app is explained
  before it is used. The doc-style test now holds every doc rewritten
  this way to the guide's rules, starting with those two, and each later
  doc joins the list as it is rewritten. The first of the doc rewrites
  (#342).

- Merging two remembered people now settles the corrections still
  waiting on the one merged away (#338). A move or delete waits in a
  ledger until the next sync sends it to membro, and it named the
  people involved by a local id that stopped resolving the moment one
  of them was merged away. A clip moved into that person then waited
  forever. A clip deleted out of them was dropped as nothing left to
  fix, membro's merge moved its copy into the survivor, and the next
  sync restored it here under the survivor's name: the owner deleted a
  recording and the recording came back. The merge now rewrites those
  rows as it removes the person. A move into them now targets the
  survivor, a merge they won names the survivor as winner, and a move
  or delete out of them keeps their membro address, ahead of the merge,
  so the delete lands before membro's merge moves the rest across.
  Forget has settled its rows this way since #335; one rule now serves
  both.

- The leak scanner is now the fleet's canonical copy (#345). membro and
  spendglass carry `scripts/secret-scan.sh` byte for byte and fail their
  own build when the copy differs, so a pattern fix lands here first and
  is then copied across. Nothing crossband-specific stays in the script:
  the exclusions only this repo needs live in `.secret-scan-exclude`,
  and spendglass's banking-key shape joins the pattern list.

- A membro that refuses Crossband's token is no longer logged as
  "membro unreachable" (#344). The person sync said that at INFO, a
  level the default install discards, so a half-done token rotation
  (membro's `.env` updated, Crossband's not) left search, sync and job
  polling dead with nothing in the log. Every refusal now logs at
  WARNING, once until the outcome changes, names the call membro
  refused and says the likely cause: `MEMORY_AUTH_TOKEN` in Crossband's
  `.env` no longer matches membro's copy. Membro being down keeps its
  wording and moves to WARNING too.

- A deploy now asks crossband whether it is busy before restarting it
  (#343). `GET /api/busy` answers on loopback without a session and
  reports, as fixed labels, a round still generating in any chat, a live
  voice capture, a guest visit running, a person sync pass, a benchmark,
  an import or a backup mid-copy. The fleet's deploy watcher used to
  guess from one machine-wide process search, which saw a Claude Code
  visit for any app and nothing else: it cut off rounds and imports it
  could not see, and held a restart for a visit that belonged to another
  app. Now it waits for your round or visit to finish, and no longer
  waits for unrelated work.

- Forgetting someone now settles the corrections still waiting on them
  (#335). A move, delete or merge waits in a ledger until the next sync
  sends it to membro, and it named the people involved by a local id
  that stopped resolving the moment one of them was forgotten. The row
  then waited forever. Worse, a merge whose winner was forgotten left
  the other record living in membro, and the next sync rebuilt that
  person here, audio and all: the owner forgot a human and the human
  came back. Forget now rewrites those rows as it removes the person.
  The other record in a merge they won is forgotten too, a clip moved
  into them is deleted at its source, and a move or delete out of them
  keeps their membro address so it can still land. The same settling
  runs when the forget came from membro, and the sync applies membro's
  forget marks before it rebuilds anyone, so a settled merge's loser
  cannot be pulled back in between.

- Forget reaches memory the moment you press it (#334). Forgetting a
  voice on the Voices page deletes the audio here at once, but the copy
  in membro used to wait for the next sync pass: after a round, at most
  every two minutes, or at startup. For a round or two the explainer
  said the audio was gone in both apps while it was gone here only, and
  nothing on screen said the rest was still on its way. Forget now
  starts that pass itself, in the background, and answers as quickly as
  before. Membro down, or no token, is the same logged no-op it always
  was, and the forget stays in the ledger for the next pass. Moves,
  deletes and merges still travel with the round-end pass; they correct
  who said what, they do not remove a person.

- A model's direct `save_memory` now carries the guests present in the
  round (#331). Room mode lets people besides the owner speak into a
  chat, and the guest-attribution wall covered only the handoff path,
  where every message names its speaker; the save tool carried nothing,
  so a guest's claim could reach recall as if the owner had said it.
  Each seated guest rides the save as `guest:<name>` and an unnamed
  voice as `guest:unknown`, the classes ingest already uses, and a
  membro speaking contract 1.5 holds such a save for review. Outside
  room mode nothing is sent, and the owner alone never stamps. A 1.4
  membro ignores the field and crossband says so once in its log.

- Forgetting a voice now reaches memory too (workbench#56). Forget on
  the Voices page used to delete the stored audio from this computer
  only, while membro kept its copy and could hand it straight back on
  the next sync. The forget now rides the correction ledger like a
  move or merge: the next sync pass sends it to membro, which deletes
  its audio and holds the facts it learned from that person for
  review. The pull step no longer rebuilds a person whose forget is
  still on its way, so the audio cannot come back in between, and a
  forget membro cannot take yet stays pending rather than being
  dropped. The Forget explainer now says what happens in both apps.

- Crossband now speaks membro's memory contract 1.4 (#318), closing
  four seams that were quietly broken. A `search_history` hit written
  in a round that read the web now carries the same untrusted marker a
  live fetch gets, naming the domains, so the archive cannot launder
  injected text. The voice-discard banner links membro's eraser at the
  origin membro reports, so the link works from a phone on the tailnet.
  Before each handoff crossband asks membro how much of the chat it
  still holds, and winds its own watermark back when membro was
  restored from a backup, so the gap closes on its own. A saved fact's
  `event_date` is the owner's local calendar day. Every change is
  guarded by the field or route being present, so an older membro keeps
  today's behaviour.

- A refused voice clip now says so (#312). The acceptance gate could
  turn away every clip a person's capture offered while the screen
  showed only "still learning" at zero seconds, with no way to tell
  silence from refusal; that is exactly how a broken speech check ran
  unnoticed for a week. Each refusal now lands in the service log
  with the failing measure, and every person's entry in the people
  and health endpoints carries how many clips were refused in the
  last week and why the last one was. Times, counts and a fixed
  reason only; never audio.

- A thinned voice bank now refills itself from membro (#311). Membro
  keeps every uploaded clip; crossband keeps a small working set, and
  rotation, eviction or a since-fixed gate could leave a person's
  local bank far behind the archive. Each sync pass now also runs the
  push diff in reverse: anchors membro holds that this install does
  not are downloaded and offered back through the ordinary acceptance
  path, a few per pass, only while the bank wants more. A full,
  sufficient bank is left alone, a refused anchor is not fetched again
  and again, and restored clips keep membro's own content address, so
  corrections still reach the durable copy.

- Stored voice clips no longer carry their dead air (#310). Every
  capture starts with pre-roll and ends with the pause that ends a
  turn; that silence was stored, it diluted the voice embedding, and
  it counted toward the seconds a voice needs before identification
  is trusted. Clips are now trimmed to their voiced span at banking,
  with a small margin, and a noisy tail trims like a silent one. Gaps
  inside the speech are untouched. Clips restored from membro keep
  membro's own content address, so owner corrections and the sync
  keep speaking to the durable copy rather than minting variants.

- Real speech is no longer set aside as "not a voice" (#309). The
  speech check counted every frame of a clip against it, including
  silence, and every capture carries silence by construction: the
  pause that ends a turn, the pre-roll, ordinary word gaps. So short
  remarks were refused, stored clips of real voices sat quarantined
  as noise, and a still-learning person could sit at zero seconds
  while being heard daily. The check now judges only the frames loud
  enough to hear, so silence is neutral; static, hiss and clicks
  still fail, because their frames are loud and noise-shaped. Wrongly
  set-aside clips still on disk come back on their own at the next
  audit.

- The live-microphone list (the every-surface mic banner, and the new
  diagnostics dump) is read from a snapshot now, so reading it at the
  exact instant a capture session closes can no longer fail the request.

- Voice stalls can now be reported from the phone, evidence and all
  (#304). The app keeps a short in-memory log of what a voice session
  did - states, timings, round events and any red error text - never
  what anyone said. "Save voice diagnostics" in the voice settings
  writes that log to one server-side file, beside the live capture
  sessions, the chat's recent identity decisions, the parked-label
  outcomes and the latency summary. The health endpoint now also shows
  the last few identity decisions per chat (with the turn each decided)
  and whether a parked label was claimed or expired unclaimed, so a
  stalled turn's evidence survives the next turn overwriting the live
  readouts.

- The pre-v0.2 `MMC_` environment prefix is no longer read (#301), as
  every startup warning since v0.2 promised. An old-name variable whose
  new name is missing now stops the app at startup with the exact
  rename printed, so nothing changes silently; a stale line beside its
  migrated twin just asks to be deleted.

- Handing a chat to memory no longer stalls silently when membro gates
  its job routes (#298). The status poll now carries the same owner
  token the search call sends, and a refusal fails loudly at once
  instead of spinning for fifteen quiet minutes per chat.

- A room-size limit of 0 now means what it says (#295): no guest can be
  seated. It used to be silently treated as the default of 6. Your own
  tap-to-correct still seats a person regardless, because the owner's
  hand outranks the cap.

- The room switch's off now means off (#294). Switching room mode off
  in the voice drawer does everything spoken "solo mode" does: ambient
  listening goes quiet, everyone present is marked as left, and any open
  who-is-this question closes. A room you switch off can no longer
  re-arm itself from the next voice it hears.

- The reasoning-effort choices the seat editor offers, and the Claude
  models it greys out, are now checked against the backend's own rules
  by the cross-language contract test (#292). If the two sides drift,
  CI goes red instead of the editor quietly offering stale choices.

- The docs now follow their own style rules: the stale hardcoded prose
  figures left CONTRIBUTING.md, honesty-announcing phrases and issue
  numbers left the headings they sat in, VOICE_ID.md's duplicated tuning
  table now lives only in CONFIG.md, and two of ARCHITECTURE.md's four
  "X, not Y" headings were reworded.

- ARCHITECTURE.md's shape block now maps the whole backend: the routers,
  the round buffer, the Membro bridge and the four voice-identity modules
  each have a line. The docs index lists the eval harness READMEs, CI
  enforces that, and the documentation guards moved from the plist suite
  into tests/test_doc_style.py where a reader would look for them.

- A removal-only pass cleared the residue of three past retirements
  (#244). Dead helpers, four unread spend-summary fields, and the
  bare-ellipsis pass marker whose convention was replaced now go; a
  historic ellipsis reply renders as an ordinary small bubble. The
  contributor guide gains a retirement checklist so the next removal
  does not stop at the first green build.

- Rules that live in both Python and JavaScript are now guarded (#234).
  The Connections console renders the cost-provenance label and the
  onboarding gate the backend ships, instead of keeping its own copies;
  its seat badge and promote wording collapse into the one lifecycle
  module, resolving three quiet drifts in the visible text. A committed
  contract fixture lets the frontend suite assert backend constants, so
  a backend rename now fails a test instead of leaving the two sides
  politely disagreeing.

- The pass and echo guards inside a round are now one pure decision
  (#241). Four interacting flags across a retry loop were discoverable
  only by reading all of it; the judgement table is extracted and pinned
  by tests, the round's tail moved to its own function, and two dead
  writes that a rebuilt dict discarded are gone. The rest of the round
  recipe deliberately stays one documented function.

- Room and roster state now has one write path (#239). Six sites armed,
  disarmed or seated with hand-rolled ceremonies, in the subsystem
  whose drift minted the phantom people. backend/room_state.py owns the
  ceremony: durable commit first and in one statement, then the live
  mirror, then the roster steps, then the wake-up bell, which now also
  rings on the early-return flips that used to ring nothing. The roster
  cap derives from one place, so the number the page shows cannot fork
  from the number the seats enforce. A build guard fails any new direct
  writer outside the module.

- Token counts and dollar amounts now format one way everywhere (#236).
  Six token formatters and four money rules had drifted apart, so a
  multi-million-token figure could still render as a five-digit "k"
  number in four places, easy to misread by a factor of a thousand.
  Message usage arrows, the export picker, the Spend page and the
  header gauge all gain the M tier, and sub-ten-cent amounts show
  graded decimals instead of flattening to $0.00 or $0.05. The
  reasoning-effort rules moved to a tested module, and a stale effort
  value now resets to Default at save instead of failing the whole
  save. Message rendering also stops rescanning the transcript per row.

- Three shipping paths gained their first tests (#242). The attachment
  projection is pinned per kind on both provider sides, including the
  truncation cap and the framing both providers must share. The
  continue endpoint's round choreography is driven end to end,
  including the shared one-round lock with send. The frontend's fetch
  wrapper has a suite pinning that failures reject, a 401 raises the
  lock screen exactly once, and path segments stay encoded.

- Voice label payloads are now built in one place, the label passes
  share one delivery path, and the two drifted passes are repaired
  (#237). A solo turn confidently matched to
  you keeps its audio in memory briefly, so tap-to-correct and the
  is-it-really-you check work in solo chats. The turn that arms room
  mode now gets the same second-guess a fast-path label gets; that is
  one extra cheap-model call, visible on the Spend page.

- A reply that runs out of tool budget is now attribution-audited like
  any completed reply (#240). A long research reply is the shape most
  likely to misattribute, and it was the one shape the audit could not
  see. The diagnostics tool description also now names all five
  diagnostics; it said "exactly one of" and then listed four.

- Three chat routes (incremental messages, the guest-job snapshot, and
  voice-turn discard) no longer run their database reads on the event
  loop (#243). They run in the request threadpool, so a slow disk read
  cannot stutter live voice, and the incremental route runs on every
  new message.

- The service log now rotates at boot once it passes 10MB (#243). It
  grew without bound under the supervisor. One prior generation is kept
  beside it as service.log.1.

- Re-importing a provider export no longer scans every chat row per
  conversation (#243). The import idempotency lookups now ride partial
  indexes, measured 76 times faster at five thousand conversations.
  Rows that never came from an export cost nothing.

- A failed GitHub token probe is no longer cached until restart (#243).
  Only a found token is cached, so after `gh auth login` the next page
  load sees it, and the Connections page stops telling a logged-in
  owner to log in.

- Asking a deleted chat to distil now answers 404 like every other
  chat route (#243). It answered 200 with an ok flag the frontend never
  read, so the failure dissolved instead of surfacing.

- config.local.json now has exactly one writer (#235). The pricing API
  kept a private copy of the atomic write that config.py already owns,
  and the shared function's comment claimed a consolidation that had
  not happened. The copy is deleted, both pricing saves go through the
  one path, and the docstring now tells the truth.

- Two open PRs no longer conflict on the changelog (#277). Each change
  now ships its entry as one file under `changelog.d/`, and a release
  folds them into `CHANGELOG.md` newest first, above the entries already
  sitting under Unreleased. A test fails any PR that edits Unreleased
  directly and names the new home.

- Facts mined from web-touched rounds now reach the review hold they were
  built for (#268). The round's web stamp was written to the message row,
  but the query behind the memory handoff never selected the column, so
  the stamp never left the app and the memory service treated every
  web-informed reply as ordinary transcript. The stamp now rides the
  handoff, and an end-to-end test walks a stamped row all the way to the
  wire so the seam cannot reopen quietly.

- Voice corrections can no longer be eaten by a bad sync pass (#273). A
  move or delete of a learned clip is replayed into the memory service so
  the correction cannot resurrect through a rebuild. When the service
  answered the clip lookup with an error (a stale token's 401, a 500),
  the replay read that as "already converged" and consumed the
  correction permanently. An unreadable lookup now leaves the correction
  pending and the next pass retries it. The person-sync watermark also
  advances properly once the service reports change stamps, so each pass
  asks only for what changed.

- A crash loop can no longer destroy your restore points (#275). Every
  startup snapshots the database, retention keeps the newest 14 copies,
  and the service manager restarts a crashing app every few seconds - so
  about two minutes of crash loop used to evict every pre-crash snapshot
  at exactly the moment one was needed. A snapshot byte-identical to the
  newest one is now discarded, so restarts that change nothing keep the
  history intact.

- A chat that carried more than 20 files on one message can reach memory
  again (#271). The memory service caps attachments per message at 20
  and rejects the whole handoff past that, so one bulk file drop wedged
  its chat: the handoff retried the same rejected payload on every
  leave, and nothing after that message was ever ingested. Over-limit
  messages now ride the wire in chunks the service accepts, and every
  file still lands.

- The rate card now prices Claude Opus 5 and Claude Mythos 5, and corrects
  Claude Sonnet 5 to the $2/$10 the pricing page made standard when the
  scheduled September rise was cancelled (#262). An Opus 5 seat previously
  recorded every turn with unknown cost, and a Sonnet 5 seat's spend
  estimates read 50% high. Every Anthropic row was re-verified against the
  published page on 2026-08-28 and is stamped with that date.

- The Spend page now counts the background model work behind room mode
  and voice (#232). Five of the eight cheap-model calls the app makes
  were spending real money and appearing nowhere: the ones that read a
  turn for a spoken command, an introduction, a name correction, a
  reasoning-depth change, or a suspected wrong speaker. Only the title,
  summary and distillation calls were counted. Your utility total will
  step up as a result, and on a busy room-mode chat it may step up a
  lot. That is spend you were already paying, now visible.

- The running total in the chat header is now the same number the export
  picker shows (#231). It was worked out in the browser from the message
  list, so it could only ever see messages. Voice cost sat outside it and
  the background model work behind titles, summaries and room mode was
  missing entirely. The two screens could show different amounts for one
  chat. The header now reads the figure the server sends, so the token
  count and the dollar amount will both look higher, and the voice cost
  appears once rather than beside a total that excluded it. It refreshes
  when you open a chat and at the end of each round.

- Closed a gap in the voice relays. On an install that has a trusted
  host configured and has never enrolled an owner password, anything
  that could reach that host could open the two voice relays without
  passing the lock screen, and spend against your ElevenLabs key. Every
  other API route already refused those callers. The relays now refuse
  them too. Loopback is unchanged.

  Only the tailnet could reach it, and no released version is affected:
  the browser gate did not exist in 0.1.0, 0.1.1 or 0.2.0. If you have
  been running from main and have not enrolled a password, enrol one.
  That closes it on every surface and brings voice back on your phone.

- Chats holding a large PDF or several images reply faster (#229). The
  files were being re-read and re-encoded once for every model in the
  round, every round, which stalled everything else the app was doing
  at that moment. They are now read once and reused. On a three-model
  round over one 20MB PDF that is about half a second back per round.

- A rate card you save now prices the very next round (#230). Saved
  cards were reaching the pricing screen and nothing else, so rounds
  kept using whatever the app read at startup. Because the cost source
  is recorded at the moment a reply is written, those rows could not be
  corrected afterwards by fixing the card. Hand-edited prices in
  `config.local.json` reach a round now too.

- The Spend page no longer shows $0.00 for cache writes on a model you
  priced yourself (#230). It was reading the built-in price list rather
  than yours. Some historical cache-write figures will go up as a
  result, because they were understated rather than free.

- Documentation corrections (#233). The web research page said a
  rendered view shows text and not a screenshot, which stopped being
  true when the screenshot shipped, and it sat in the list of that
  feature's limits. Three documents said there is no component test
  infrastructure, while CI has been running a render smoke on every
  change. The setup guide's list of API keys left out Reddit and one of
  the two GitHub token names. The contributor guide gave one of the
  three frontend checks as though it were all of them.

- Startup now names a missing Reddit key like every other missing key
  (#233). It was the only capability the report could not see.

- A stored voice that outlives its human backing must earn your ear
  again (#221). Each automatically stored clip now records the match
  confidence it banked at. When rotation has replaced every recording a
  person stood behind, those scores decide: weak ones pause the voice
  from naming or seating anyone until you listen and confirm (people
  already seated in a live chat keep being identified), strong ones
  keep working with a note in Remembered voices. A voice with no human
  backing at all keeps its existing audition ask regardless of scores.

- The voice hygiene audit now also hears "not a voice" (#219). Each
  stored clip is checked for actual speech during the audit; a noise
  clip is set aside under its own reason, shown distinctly in
  Remembered voices ("not a voice" rather than "sounded like someone
  else"), and existing installs clean themselves on their next audit
  with no manual hunting.

- Non-speech audio can no longer be stored as anyone's voice sample
  (#218). The clip acceptance gate now includes the speech check, so no
  path into the voice store - accumulation, harvesting, cold start,
  introduction or correction - can bank noise, and loud static can no
  longer displace genuine quiet speech from a stored voice.

- A spoken self-introduction now overrules the voice guess on its own
  turn (#220). When someone introduces a remembered name but the turn's
  audio had already been confidently labelled as a different remembered
  person, the introduced name wins: the turn is relabelled, the wrong
  automated seat is withdrawn, the contested audio leaves the wrongly
  fed voice record, and a merge question tells you the two stored
  voices are colliding. Labels you corrected by hand, and seats placed
  by a person, are never unwound.

- Storing a voice sample now demands more certainty than naming a turn
  (#222). A borderline match keeps its label, but its audio is only
  added to the person's stored voice when the match also clears a new
  banking bar (`voice_id_banking_extra`, default 0.1 above the naming
  threshold). One wrong name can no longer feed the very voice record
  that produced it.

- Non-speech audio can no longer be named as a person (#217). Every
  utterance now passes a local speech check before the voice matcher may
  ask whose voice it is, so a static burst can no longer match a
  remembered voice, seat an absent person, bank itself as their sample,
  or start a cold-start bank. The identity pulse reports the new state
  as "not a voice".

- A cited source with nothing fetched behind it is flagged (#213). A
  "the docs say…" claim in a reply that ran no tools gets the same
  quiet chip as a misquote: the claim may still be right from training
  or memory, so the chip says unverified, never wrong. Any tool row in
  the reply skips the check, and it never retries or blocks.
  `CROSSBAND_CITATION_CHECK=false` turns it off.

- Misquotes of chat members now show up where they happen (#211). The
  attribution audit covers third-person claims too ("Claude said …",
  checked against Claude's own turns, with a they-to-I flip so faithful
  reports never flag), returns its findings to the round, and each
  flagged reply carries a quiet amber chip quoting the claim that has no
  word-for-word match. The chip is a prompt to check, never a misquote
  verdict: paraphrase and summary-folded history look the same. Audit
  log lines move to WARNING so a default deploy records them; they stay
  content-free, since the claim text lives only on your own message row.

- A reply that mostly restates what the chat already holds is dropped
  (#210). After a seat's reply completes, a word-overlap check compares
  it against the seat's own previous message and the replies already
  given this round. A near-copy is dropped and the seat re-runs once,
  told to add something new or pass; a copy on the retry is suppressed
  the way an insisted pass is, and a pass there is always accepted.
  Verbatim-leaning on purpose: quoting to answer, short agreements,
  repeat requests, tool-using replies and true paraphrase all stay
  untouched. Voice rounds only log, since the reply has been spoken by
  the time it can be judged. The scan runs in process after streaming
  ends, so reply latency is unchanged. `CROSSBAND_ECHO_GUARD=false`
  turns it off.

- Voice has one engine selector (#202). A new `voice_provider` setting
  names which engine serves speech. `auto` (the default) is exactly
  today's behaviour: ElevenLabs when its key is set, no voice when it is
  not. `elevenlabs` makes that choice explicit. `local` is reserved for
  a future local engine and, until one ships, selects nothing - even
  with a cloud key present, so choosing local can never quietly mean
  cloud. Every voice surface, the benchmark panel included, now resolves
  through this one choke point.

- Ollama seats can keep their model loaded between turns (#203). Ollama
  unloads a model five minutes after its last request, so a quiet chat
  costs a reload before the next first word. A new **Keep model loaded**
  setting on an Ollama seat names the window - `30m`, `1h`, `24h`, or `-1`
  for indefinitely - and Crossband now asks Ollama to hold the model in
  memory around each of that seat's requests. Left empty (the default)
  nothing changes, and Ollama's own five-minute unload still applies. It
  speaks Ollama's native keep-alive call, because the field cannot ride
  the OpenAI-compatible requests the seat already uses. See
  [docs/MODELS.md](docs/MODELS.md).

- Other apps' passkeys no longer crowd crossband's unlock sheet
  (#204). The fleet's apps share the browser's localhost passkey scope,
  so the sheet used to offer every app's key here. The gate now tells
  the browser exactly which keys are crossband's own. The trade, made
  deliberately: the lock screen now names those key ids to anyone who
  can reach it - an id is a serial number for the key, not a secret,
  and it cannot unlock anything.

- The page renderer is boxed in by the operating system too (#148). On
  macOS the render worker now runs inside an OS sandbox profile: no
  network except the vetting proxy's port, no writes outside its
  throwaway profile folder, and no reads of your data, `.env` or
  `~/.ssh`. It sits on top of the existing containment rather than
  replacing any of it, and a machine that refuses the profile renders
  exactly as before, saying so once. `scripts/sandbox_probe.py` proves
  the profile on a new machine.

- The seats can be raced on identical scripted cases (#94). A Benchmark
  panel on the Models page runs your chosen models through fixed cases
  and compares stage timings side by side: text replies, speech-to-text
  on a spoken fixture, each seat's own voice, and the full
  listen-think-speak pipeline. Synthetic by design - no microphone, no
  playback, results labelled as not-live-turn numbers - with generated
  audio saved for your own ears and deletable per run. See
  docs/BENCHMARK.md.

- "Just answer this one quickly" works as said (#105 slice 2). A spoken
  depth instruction scoped to the next reply ("think hard about just
  this next one") applies to exactly that reply and reverts by itself.
  It outranks the standing depth for that one call and posts no mode
  notice; the seat is told it is a one-off, so it never adopts it as a
  mode. A round that dies before the seat speaks keeps the override for
  the next round. Standing instructions behave exactly as before.

- A quiet voice can be turned UP (#163). Voice volume is now a relative
  weight (up to 300%): boosting one voice plays it at full volume and
  quietens the others proportionally, since a browser cannot push audio
  past full. With every seat at or below 100% nothing changes - the old
  turn-a-loud-voice-down behaviour is untouched. Set it once per voice
  on the Models page; the device volume sets the overall level.

- Rendered pages stop paying politeness per subresource, and a stuck
  render can no longer leak browsers (#153). The egress proxy paces
  connection BURSTS, not connections: a page's dozen same-host
  subresources no longer queue tens of seconds of spacing inside the
  render budget or delay the next fetch to that host. And the render
  worker now runs in its own process group, so the deadline kill takes
  Chromium and the Playwright driver with it instead of orphaning them
  at several hundred MB each.

- A rendered page load now has a whole-page transfer budget (#148,
  second half). The egress proxy grew a budgeted view listener: every
  connection a `view_page` render opens carries a per-view key, and all
  of them share one 30MB budget (`browse_page_budget_mb`), so a page
  cannot pull without bound through many small connections. Per-
  connection caps and the worker deadline stay as they were; plain
  fetches are untouched. The macOS worker sandbox half of #148 stays
  open - it needs the target machine.

- The view_page tool row carries a screenshot of what was viewed (#149).
  The rendered-viewing worker captures a viewport PNG; it lands in the
  ordinary attachment store keyed to the assistant message, and the tool
  chip shows it when expanded - so you can see exactly what the model
  saw, not just the extracted text. Capture is best-effort and bounded:
  a failed or oversized shot never costs the view its text. Feeding
  screenshots to vision models stays out of scope.

- A verification interstitial reads as one clean line (#150, first
  half). When a rendered page turns out to be a human-verification
  challenge ("Just a moment..."), view_page now says so in a single
  sentence pointing at the paste-into-chat path, instead of dumping the
  interstitial's own text into the transcript. None of the challenge's
  URLs can enter the seen-URL ledger. Completing challenges stays out
  of scope by design; the renderer-identity question stays open on the
  issue.

- Reasoning depth answers to your voice, per seat, per chat (#105 slice
  1). "Slow down and think harder", "take your time Claude", "quick
  answers from now on", "back to normal": a cheap prefilter plus the
  same utility-model confirmation room commands use turns natural
  phrasing into a persistent per-seat depth for that chat - no memorised
  incantation, no automatic de-escalation. A named seat moves alone; no
  name moves every seat. Each change lands a system notice stating the
  trade ("replies here will take longer"), and the seat itself is told
  its current depth so it answers honestly when asked. Model tier is
  deliberately not spoken-controllable yet.

- Promised deliverables arrive in the same reply, voice included (#80).
  A participant gets exactly one message per round, so "I'll put the
  full list in my next message" was a promise the app made impossible
  to keep - one morning produced four such promises and no list. Seats
  are now told that truth plainly. In voice, a reply can carry a spoken
  summary and then a full written deliverable below a [written] line:
  the written part lands in the transcript under a labelled divider and
  is never read aloud, so "too long to say" no longer means "defer".

- The Connections page shows repo access per surface (#86). One table
  says, per repo, whether the models' GitHub tools reach it (and which
  owner/repo that means) and whether the coding guest can open it in an
  isolated worktree, read-only or with writes. MCP servers are named as
  live-machine surfaces, kept apart from repo rows. The room asserted
  contradictory access facts for days because nothing displayed them.
  The guest's repo map now also re-reads from config per round, like the
  GitHub map already did, so edits apply without a restart.

- A guest resume whose session transcript is gone now retries fresh
  (#17). continue_last resumes Claude Code's previous visit by id; a
  cleaned ~/.claude or a new machine made that resume fail the whole
  guest turn. The visit now restarts fresh instead, saying so in its
  first line. Other launch failures still fail loudly.

- GitHub repo config edits apply without a restart (#24). The
  `github_repos` map was read once at boot, so after a repo rename the
  models' allowed-repo list and the integrations tile stayed stale
  until the server restarted. The map is now re-read from disk at each
  round start and status read, the same way pricing already reloads.

- The voice tick stops posing as a live verdict (#139). A green tick
  beside a person means their profile holds enough approved audio to
  recognise them; it said turns "are named automatically", which read
  as recognition of the turn being spoken even while that turn sat on
  "Identity pending". The tick's copy now scopes itself to the profile
  and points at each turn's own label for the live answer.

- A chosen participant voice now survives seat edits and voice starts
  (#161). Saving an unrelated seat edit re-sent the editor's stale
  blank voice selector, clearing an assignment made after the editor
  opened; the next voice start then re-rolled a different voice from
  the provider's floating list order. A save now carries the voice
  only when the selector actually changed, and the fallback pool is
  sorted, so identical accounts assign identical voices.

- A wedged seat can no longer hold a round hostage (#168). A seat that
  produces no stream event at all for three minutes - no text, no tool
  event, no liveness check-in - is errored and the round moves on, with
  any partial reply kept. Provider SDK defaults allowed about ten
  silent minutes, during which /send returned 409 and the voice gate
  stayed armed. Every stream event resets the bound, so slow healthy
  replies and long tool runs are never cut.

- Voice stalls now leave server-side evidence (#171). The client posts
  a content-free beacon when the round guard force-clears a dead round,
  or when speech strands for ten seconds behind a gated microphone. The
  server logs it at WARNING, so data/service.log shows the stall at the
  default log level, diagnosable from a phone. Round streams also time
  out after 90 seconds without bytes and recover through the normal
  reattach path, instead of pending forever on a half-open connection.

- Seats carry three conduct rules from the field (#172). Never claim a
  dispatch without its tool result in the same reply. Never promise a
  merge: Claude Code opens pull requests, and the user reviews and
  merges. Voice attribution heads are metadata, never a topic to raise
  unprompted or to return to after being told to drop it.

- A summoned Claude Code visit that cannot start now says so in the
  chat (#170). If Claude Code was switched off after the summons, or
  another visit was still running, the summons was dropped in silence
  while a seat had already promised the guest was coming. The drop now
  lands as a system notice, so nobody keeps waiting for it.

- The "two microphones live" banner stops accusing your own reconnect
  (#167). A phone reconnect registers a fresh capture session while the
  dead one lingers server-side for up to 40 seconds, so the banner
  counted the same microphone twice. The client now ends its own
  previous session the moment it is handed a new id, and dead sockets
  are reaped within about 20 seconds. A real second device still shows
  the banner exactly as before.

- Seats told to stand back while Claude Code works now pass properly
  (#169). The delegation note said to pass with a bare "…", a token
  nothing recognises, so obedient seats' ellipsis replies landed as
  real messages and voice rendered them as dead air. The note now
  names the real [pass] token, pinned to the engine's own constant.

- Voice says "Thinking…" while the models work, and a dead round can no
  longer trap the microphone (#165). The generation wait used to render
  as "Listening…", which invited speech the gated mic then discarded or
  turned into an accidental round-kill; a new working state names that
  wait honestly on every surface. A round silent for 60 seconds with
  nothing playing now force-clears the turn gate, ending voice and
  starting again begins from a clean gate, and Stop can abort the
  detached-round replay. A send refused because a round is still
  running is held and retried instead of dropped.

- Local thinking models can be told to skip the reasoning trace (#159).
  A Qwen3-family seat on an OpenAI-compatible server emitted a hidden
  reasoning block before its first visible token, and no setting reached
  it: reasoning effort only speaks Anthropic's and OpenAI's own dialects.
  Seats with their own base URL now carry a Thinking control on the
  Models page. It names the mechanism the server documents, so nothing
  is guessed from a model id, and the `/no_think` prompt hack is offered
  as an explicit last resort rather than injected. Default stays empty
  and sends no new field. An endpoint that rejects the choice fails the
  turn by name instead of silently ignoring it.

- The speaker model is named and its swap path documented (#154).
  VOICE_ID.md now says what the matcher runs (NVIDIA's TitaNet-Small
  via sherpa-onnx), that the thresholds are calibrated to it, and that
  stored voices survive a model swap because fingerprints are rebuilt
  from the kept clips. `GET /api/voice/health` reports the live model's
  file, hash prefix, and whether the built-in pin was overridden.

- Responses-route discovery survives the 404 arriving as a connection
  reset (#151). Servers that close without draining the request body
  reset transcript-sized requests every time, so the #144 fallback never
  engaged in real chats. A connection-level failure on an unclassified
  custom endpoint now earns a one-token chat ping: alive means classify
  and fall back, dead means the original error stays loud.

- The web research surface is documented (#145): docs/WEB_RESEARCH.md
  covers the tools, the containment model, the one-line install for
  rendered viewing, and the limits - including that human-verification
  challenges stay closed and pasting is the path for gated sources.
  SECURITY.md gains the outbound story, ARCHITECTURE.md maps the new
  modules, and the docs index and README feature line catch up.

- OpenAI-compatible servers without the Responses API now work (#144).
  The first reply on such a seat discovers the missing route and replays
  through classic chat completions in the same turn; later replies skip
  straight there. This covers mlx_lm.server, LM Studio, vLLM and
  llama.cpp, whose baseline is chat completions. The default OpenAI
  endpoint never falls back, so a real 404 there stays loud.

- Web content now carries its provenance everywhere it goes (#138,
  fourth slice). Fetched and rendered pages arrive marked as untrusted
  quoted data naming their domain, so every model in the room can see
  what it is reading. A round that read the web stamps its replies with
  the source domains; the stamp rides into the memory service (contract
  1.3), which holds facts born from those turns for your review - a
  public page cannot write memory by phrasing a sentence well, and an
  explicit save cannot slip past the same hold. On an older memory
  service the stamp is ignored (the previous baseline) and one log line
  says so.

- Models can view rendered pages (#138, third slice): a new `view_page`
  tool runs the page in a real browser and returns the visible text and
  its links, numbered - for app-style sites `fetch_page` reads as thin
  or empty. The render is contained: a separate worker process holding
  no keys or tokens, every request (subresources included) forced
  through the vetting egress proxy, WebRTC's proxy-bypass disabled,
  downloads refused, a fresh throwaway profile per view, and a hard
  deadline that kills the worker. Requires Playwright plus a one-time
  `playwright install chromium`; without them the tool is not offered
  and nothing else changes.

- A model can no longer invent the URL it fetches (#138, second slice).
  `fetch_page` and `transcribe_audio_url` now accept only URLs that
  already appeared in this chat from a non-model source: your messages,
  search results, links inside already-fetched pages, transcripts, text
  attachments, machine notices. This closes the channel where a hostile
  page instructs a model to smuggle private context out inside a URL it
  composes: models choose among URLs that exist, they never author one.
  A blocked fetch says so plainly and points at web_search.

- Every URL a model chooses to fetch now leaves the machine through a
  local vetting egress proxy (#138, first slice). The proxy resolves a
  host once, connects only to publicly routable addresses, and caps
  transfer size, so DNS rebinding between check and fetch reaches
  nothing local. `fetch_page` gains a decoded-size cap and reports the
  final URL after redirects; Reddit fetches refuse redirects that
  leave reddit.com.

- A live microphone anywhere is now visible everywhere (#134). If a
  capture session is running in another window or device, every
  surface shows a banner naming it - louder when it is a second mic in
  the same chat, which doubles every utterance - with an End button
  that stops it remotely: tracks off, no reconnect, ever. This closes
  the field case where voice was turned off in one window while an
  orphaned session elsewhere kept hearing the room.

- Replies no longer stall after a still-learning guest speaks (#133).
  The voice-identity background work (clip banking and the bank-hygiene
  audit) ran on the same thread pool that dispatches your messages, so
  a guest whose voice was still being learnt could starve the round and
  leave the room stuck "listening". That work now runs on its own
  bounded pool, and the audit runs at most once per 20-second window -
  deferred, never dropped.

- Guest turns now tell memory who spoke, not just a name string
  (membro#33 final slice, contract 1.2). A confidently attributed
  guest turn carries the person record, the matcher's real score, and
  how the identity was established (introduced, voice-matched,
  by-elimination, or your correction) - so facts a guest states link
  to the right person automatically when the identity is strong, and
  never on a weak guess. The matcher's per-turn score is now stamped
  into the label it has always written.

- Your voice corrections now reach the durable home (membro#33,
  slice 3). Moving a clip to the right person, deleting one, or
  merging duplicate people is recorded and replayed against membro on
  the next sync - so a fixed mis-attribution can never come back from
  backup, and a correction made while membro is down just waits for
  the next pass.

- The message list now has a render test (follow-through on today's
  blank-chat fix): the real chat view renders against realistic
  fixture messages in the test suite and CI, so a crash-on-render can
  never again reach production unseen. Verified to catch today's bug.

- Fixed: every existing chat rendered as a blank screen (a regression
  shipped earlier today with the discard affordance - two undefined
  names in the message list crashed the whole app the moment a chat
  with messages rendered). The frontend now lints for exactly this
  (no-undef, gating npm test and CI), so a free identifier can never
  reach a build again.

- Learned voices now have a durable home (membro#33, slice 2). A
  background sync uploads accepted clips to membro's person records
  (the first pass after this deploy backfills everyone), rebuilds
  people a fresh install doesn't hold, and obeys forget marks - one
  forget, in either app, deletes the stored audio in both. Membro
  down or unconfigured changes nothing: identification is local and
  never waits.

- Voices is a page of its own (#91), linked from the sidebar. It holds
  everything the app knows by voice and every control over it, with the
  room a full page gives it: listen to the stored clips, fix names and
  spellings, move a recording to the right person, confirm an auditioned
  bank, or forget someone. The models menu keeps a one-line
  pointer where the panel used to live. Voice sessions keep running
  while the page is open (the #69 strip covers it like any page).

- A model can honourably stay silent (#98). A reply of exactly [pass]
  is removed entirely - nothing shown, nothing spoken, nothing
  remembered - so "if you have nothing to add, pass" is finally a rule
  the app can mean. The guard: the first responder to your direct
  question, and any seat you addressed by name, may not pass - a pass
  there is refused and the seat answers on a single retry (and if it
  insists, it is suppressed and the other seats still run). Voice
  holds text-to-speech until a reply provably is not a pass, so a pass
  is never read aloud.

- Voice survives the models menu (#69). Opening any full page (models,
  connections, spend) no longer reads as the end of the call: capture
  and playback continue, and a compact strip keeps mute, end and the
  way back in reach at every width.
- A real mute at desktop width (#67). One unmistakable control in the
  voice dock: the mic track is disabled at the source, the models keep
  talking, and the muted state reads at a glance (amber, on every
  voice surface - dock, call screen and strip agree).
- Numbers stay digits (#66). The voice-mode brevity instruction was
  read as "spell numbers as words", which leaked into the persistent
  transcript ("issue sixty-three"). One rule now, no voice
  special-case: #61, PR 57, port 8902 - text-to-speech reads digits
  naturally, and the transcript must match what is spoken.
- Voice failures say why (#21). The realtime-transcription fallback
  banner names its cause (relay error, socket error) and logs it; a
  reply cut off mid-playback says so instead of printing the iPhone
  silent-switch checklist on a desktop Mac, and the generic playback
  failure names your platform's own hardware.

- A voice bank nobody vouched for must earn your ear (#83). The #65
  phantom banks were internally consistent and passed every automated
  check; their one common shape was crossing the sufficiency line with
  no introduction and no correction. Such a bank now asks you to listen
  and confirm in the remembered-voices panel, and a new one is paused -
  it can neither name nor seat anyone until you confirm (or someone
  introduces themselves, which vouches it). Banks that were already
  sufficient keep working while flagged.

- The discard banner now hands you the erase link (#111). When a
  discarded voice turn already reached memory, the banner links
  straight to Membro's danger zone, prefilled with that exact message
  and a preview - instead of telling you a copy exists and leaving you
  to find it. Same-host link, so it works from the phone.

- A captured voice turn can be discarded by its owner (#106). Live
  capture can transcribe audio never meant for the chat; hovering your
  own voice turn now offers a discard that removes it from the chat
  and from everything the models see from then on. The confirm copy
  states what cannot be undone: replies that already exist stay, and a
  copy that already reached memory stays there, since the ledger is
  append-only and membro-side deletion is its own owner surface. The
  audit line is content-free.

- No control with a mic icon can leave the microphone running (#108).
  The header chip that shapes reply style stops dressing as a voice
  kill switch: it is now "concise replies", icon-free, and says
  plainly that it does not touch the microphone. While a voice
  session is live, the header always shows a red End voice control
  that stops capture completely - tracks, socket, context, timers -
  without opening the dock. Found live: the owner clicked the
  mic-iconed chip, left the tab, and the browser kept recording.

- A monologue stays one message, and no utterance can send twice
  (#104, #85). The #60 noise caps were ending the whole TURN at 12/20
  seconds, chopping genuine long speech into separate messages - and
  those oversized commits regularly outran the flat 5s transcription
  patience, falling to the slow batch path (the ~57s stalls) whose
  result could then race a late realtime transcript into a doubled
  turn. Now a cap ends only the SEGMENT: the audio commits, the text
  buffers, and the turn stays open until a real silence gap - bounded
  at 60 seconds total, so the zero-gap noise case #60 was built for
  still always sends. Transcription patience scales with the audio
  committed, and a per-commit ledger - with the server stamping each
  transcript with the commit it answers - makes exactly one
  transcription win per utterance, whichever path delivers it.

- The lock screen tells the truth about passkeys, and passkeys get
  names (#87, #88). "Never enrolled" and "enrolled at a different
  address" both used to render as a silently missing passkey button,
  which reads as broken - the field case was days of "I thought I set
  it up" over a store that was simply empty. The lock screen now says
  which it is, naming the address that does hold one. And each passkey
  takes an owner-editable label ("MacBook Touch ID", "iPhone") with
  enrolled and last-used dates, so the mobile and desktop credential
  stop being indistinguishable twins.

- Learned voices are backed up (#33). voice_anchors/ was in no backup
  at all: losing the data directory forgot every learned voice while
  the chats survived. Every snapshot cycle now writes a
  voices-<stamp>.tar beside the database copy - owner-only, same
  retention, same optional mirror - and restoring is untarring it back
  into data/.

- Who-said-what survives compression, enforced (#22). The rolling
  summary that replaces old turns must keep the [Speaker] tags the fold
  demands: a summary that drops them is refused outright and the
  original turns stay in context (costlier, never wrong), so one agent
  can no longer read another's point back as its own after a fold. The
  live transcript each agent receives was already fully labelled; the
  fold was the one door provenance could quietly die through.

- An explicit introduction outranks an implicit voice match (#81). Two
  changes from the first real contamination. A confident voice match may
  re-identify an introduction silently only when the introduced name is
  a plausible spelling of the matched person's; otherwise the new person
  is seated under their own name and the resemblance becomes a merge
  question. And while anyone in the room is still unlearnt, the naming
  bar rises (`voice_id_pending_extra`), so a borderline resemblance to a
  remembered person defers instead of stealing the unlearnt person's
  turns. That is what lets their own voice bank its first clip. This deliberately reverses the fourteenth field test's silent
  collapse for dissimilar names; variant spellings keep collapsing
  silently.

- The machine-producer contract is documented (#59): docs/PRODUCERS.md
  is the complete public interface for a deploy watcher, scheduler or
  any local tooling that consumes slash commands and reports back -
  trust rules, the acknowledgement contract, notice conventions, the
  bearer credential, and the operational expectations the fleet's own
  outages taught. No behaviour change.

- Claude Code's findings are spoken when it returns (#64). The hand-back
  narrator's round finishes with the very message that announces it, so
  the voice client always arrived after the round was gone and the
  narration landed as silent text - the "you'll have to repeat yourself"
  gap from the first field days. The last round's buffer now stays
  discoverable until the next round starts, and a voice-active client
  replays it aloud exactly once. The replay can only ever be triggered
  by a participant's own message: notices, guest job output and external
  events can never resurrect an old round out loud.

- Fix a contaminated voice without deleting anything (#90). The first
  real audition found recordings of one person filed under another. The
  panel can now create a person by name, who starts unlearnt with no
  voice needed. It can move a recording to the person it actually
  belongs to, leaving the audio untouched, clearing a stale set-aside,
  and re-learning both voices from what they hold. And it can record
  another spelling of a name beside the display name, whether a
  misspelling worth keeping or the phonetic form. Owner-reassigned recordings say "reassigned by you" in the
  list. The AI-participant boundary (#77) holds at every new door.

- Hear what a remembered voice was built from (#68). Each person in the
  remembered-voices panel now lists their stored recordings - how each
  was earned in plain English, when, how long, whether the hygiene audit
  set it aside - with play and per-recording delete. Elimination-earned
  clips carry a "listen closely" flag, because that capture path is the
  one that has banked the wrong person before. Playback never changes
  anchor state; deleting a recording re-derives the voice from what
  remains, and deleting the last one leaves the person known but
  unlearnt. Recordings are served only to the authenticated owner.

- A stopped deploy watcher stops looking identical to a queued deploy
  (#58). Machine tooling now acknowledges each slash command it reads
  (the notice route gains `ack_command_id`); a command nobody acks
  within `slash_ack_timeout_s` (default 2 minutes, 0 = off) gets one
  system line saying nothing picked it up, and a restart inside the
  window re-arms the timer. Crossband still assigns no meaning to any
  command - it only learns whether SOMETHING read it.

- An AI participant can never be seated as a person in the room (#65).
  Agents are addressed by name in nearly every spoken sentence, and one
  introduction-shaped mishearing ("This is Claude...") could seat the
  agent on the roster - after which by-elimination learning banked HUMAN
  voices under the agent's name until the phantom was a remembered voice,
  and forgetting it just let the still-pending seat mint a fresh one.
  Participant names now get the same spelt-by-ear protection the owner's
  name has (variants like "Clyde" for "Claude" included), the seating
  chokepoint refuses the exact names whatever path asked, and
  `scripts/repair_participant_voices.py` repairs the phantom voices and
  seats an affected install already has.

- Deploy notices reach the chat again on a password-protected install
  (#62). The browser gate locked out the machine side-channel the moment
  a password was enrolled: the deploy watcher's progress notices - and
  any `/api/ingest` producer - got 401s, so a working deploy was
  indistinguishable from a dead one. The existing `ingest_token` is now
  the machine credential for both routes: a valid bearer passes the gate
  on exactly those two paths, an invalid or missing one is still
  rejected, and browser-session protection is unchanged everywhere else.

- Stable guest names on the memory wire, and the owner's name harder to
  mishear into a guest (#56). Two fixes from the first real multi-human
  sessions. A guest's name now crosses to the memory service as the one
  stable identity name, never the cosmetic preferred spelling, which
  stays a display concern. So renaming how a name is shown can no longer
  split one person's history into two guests in the ledger. And a
  transcription of the owner's own name up to two letters off is now
  recognised as the owner everywhere a name could join the roster,
  instead of minting a phantom guest.

- Auto (hands-free) voice turns no longer wait forever for silence that
  never comes (#60). Sustained background noise - road noise, wind, a
  fan - could keep the mic reading "still speaking" indefinitely, so a
  turn was never sent. A turn now bounds itself: past 12s it finalizes
  at the next real gap in the audio (even a brief one), and past 20s it
  finalizes unconditionally either way. Ordinary quiet-room pauses,
  barge-in, and push-to-talk are unchanged - the cap only ever fires
  after the normal silence timeout would already have.

- The room button in the voice tray is now an indicator (#28). The room
  switches itself on whenever it is needed - a voice it recognises or
  cannot place, a spoken introduction, a "group mode" command - so a
  button there suggested a press was required when it never was. The
  tray now simply shows the state: "room on · N", "listening" or
  "solo", each with a plain one-line explanation. The manual switches
  live on in the voice settings drawer ("switch on now" and "switch off
  for this chat") for first sessions, for when voice recognition is
  unavailable, and for anyone who cannot speak a command. Nothing
  changed underneath: the same durable switch, the same spoken
  commands.

- A remembered voice is now recognised the moment they speak, even in a
  room that is already listening for several people (#28). Before this,
  turning room mode on quietly narrowed recognition to the people
  already listed as present - so a household member the app knew
  perfectly well could talk all evening and never be named, because
  they were never on the list and could not get on it without being
  named. Recognition now checks everyone the app remembers, and a
  recognised person joins the room on their first turn. Everything
  stays on this device and nothing new runs while you are speaking.

- A brand-new guest can now be learnt while you are in the room (#28).
  Learning a new voice by elimination used to require them to be the
  only person present. Now it is enough that everyone else in the room
  is already recognisable: a clear turn that matches nobody known is
  put towards the one person still being learnt, labelled with their
  name and marked "learning this voice". The same cautions hold - never
  when voices overlap, never over a confident match, and poor audio is
  still rejected.

- An introduction under an unfamiliar spelling now sticks to the right
  person (#28). If someone the app remembers is introduced under a
  spelling too different for any spelling rule ("Samantha" for a
  remembered "Sam" is fine; a wholly different rendering was not), the
  app now checks the introduction's own voice. That includes when room
  mode is already on, where it previously judged leftover audio from
  before the room opened, and could even bank a guest's words as the
  owner's voice. That door is closed. The new spelling is kept on the
  person's record, so it is transcribed and resolved correctly from
  then on. And saying "Matteo is the spelling but it's pronounced
  Mateo" now records both forms on one person without overriding any
  name you have set yourself.

- The voice panel now answers "is it still learning?" and "why wasn't
  that turn named?" (#28). Each remembered voice shows whether it is
  still growing or refreshing in place, how many clips it holds and when
  it last learned something - numbers that previously existed only in a
  file on disk. And a turn that goes unnamed says which kind of unnamed:
  too short to judge, heard but not recognised, or too close between two
  known voices. Both are read from values the app already had, so
  nothing new runs while you are speaking.

- The AI seats now see who is speaking on the turn they are answering
  (#28). The voice check finished in time, but its label could only be
  written after your message row existed - and the reply starts rendering
  in that same instant, so the models read "identity pending" on the very
  turn your screen already showed as confirmed. They were reading a frozen
  copy while the browser got a live update. The finished result now rides
  the message as it is saved, so the name is there before anything reads
  it. Nothing waits on it: if the check has not finished, behaviour is
  exactly as before.

- A forgotten voice can now be re-learnt just by talking (#28). If you
  cleared your voice records, every way back in needed something you no
  longer had: being recognised needs stored clips, the introduction flow
  needs an introduction-shaped sentence, and correcting a name needs a
  name on the turn to correct. So the app deferred on every turn, learnt
  nothing, and told the AI seats "identity pending" over and over. Now,
  when room mode is on and you are the only person in the room, a turn
  the app cannot place is worked out by elimination - there is nobody
  else it could be - so it is banked towards learning your voice and the
  turn is labelled with your name, marked "learning this voice". After a
  few turns your voice is remembered again and ordinary recognition
  takes over. It is deliberately narrow: never with two people in the
  room, never when two voices overlap on one turn, and never with room
  mode off. The label is still one tap from being corrected, and no
  cloud call is involved.
- The voice dock is one panel with two rows, not four floating layers
  (#28). The status line used to be an ever-growing run of text that
  spilled out of its tray as soon as a second voice existed. It is now
  one chip per person: a green tick beside the name once their voice is
  remembered, "learning 4s" while it is still being learnt, and plain
  while nothing has been heard. The chips wrap, collapsing into a "+2"
  past four, with the live speed reading kept small on the right. The controls you touch per turn (microphone mode, the
  room toggle, send, stop, end) stay on one row; the pause and speed
  sliders, which you set once, move behind a settings button along with
  the matcher and mode readouts. Nothing was removed. On a phone the
  whole thing is a single line - "Listening · Alex ✓ +1" - that opens on
  a tap.
- Fix the cause of "identity pending" on the owner's own voice (#28).
  Two faults compounded. A tap-correction naming you could mint a SECOND
  person holding copies of your own voice clips, so the matcher found two
  perfect matches for one voice and honestly refused to choose - which
  read as "it never recognises me". And the hygiene audit that exists to
  catch exactly that duplication spent its one attempt per bank shape
  while the model was still warming up, so it never actually ran. Now: a
  correction that names you (by spelling OR by voice match) feeds your
  existing record instead of creating a twin, and the audit waits for the
  matcher to be ready before counting its attempt.

- Your own voice is now shown as recognised, not hidden (#28). The app
  has always quietly checked spoken turns against your remembered voice
  - it is how a solo chat stays solo - but it kept the result to
  itself, so the AI seats would say "identity pending" about the one
  person already identified. When the on-device check is confident it
  is you, the turn now carries your name with a "voice confirmed"
  marker: the seats see it in any mode (room on, ambient, or solo) and
  can answer "who is speaking?", and the turn shows a small tick as
  quiet reassurance. Nothing else changes - your voice still never
  switches room mode on, turns with no label render exactly as they
  always have, and uncertain turns stay honestly uncertain.
- Fix a voice-identification deadlock (#28). A two-part "enough voice
  learnt" rule shipped one build earlier also demanded a quota of short
  clips, which instantly marked every already-learnt voice as not-yet-
  learnt - and because an unlearnt voice is never matched, and only a
  match banks more voice, nothing could recover. Learning is back to the
  seconds bar; short-clip readiness is a separate progress hint, and a
  confident match on a longer turn quietly banks a short slice of its own
  audio so quick interjections become recognisable without any ceremony.
- The cloud no longer guesses who is speaking (#28). Voice
  identification is now local or honestly uncertain, full stop: the
  on-device matcher names a turn, or the turn stays unnamed - no cloud
  pass ever assigns a name, so the field-tested failure where a solo
  speaker was mis-named by the cloud fallback is structurally
  impossible. The only cloud transcription left in room mode runs when
  people talk over each other, to untangle who said what - so room
  mode's cloud voice spend is now just that, instead of a second listen
  per uncertain turn. If the local matcher is unavailable, turns simply
  go unnamed and room mode switches on only by hand (introduction,
  spoken command, or the toggle): degraded means manual, never wrong.
- Quick interjections become recognisable (#28). A voice now counts as
  learned only once its stored clips include a couple of SHORT ones
  (a word or two), kept in their own best-N pool so long sentences can
  no longer crowd them out - and the learning progress shown under
  Remembered voices says which half is still missing. Matching also
  accepts shorter utterances than before, where the audio is clearly
  voiced.
- Stored voices now audit themselves (#28). Whenever a voice bank
  changes, every clip is checked against every person. A clip that
  sounds more like someone else is set aside: kept on disk, shown as
  "clips set aside", and excluded from matching. Two people whose stored
  voices sit close together are flagged in the voice health strip
  ("Alex and Sam sound close - matching is stricter"), and the matcher
  automatically demands a wider winning margin between exactly those
  two. This is the guard against the field-tested
  cross-contamination that once let one person's turns be confidently
  labelled as another.
- Names now arrive with the words, not after them (#28). The app
  starts identifying a speaker the moment they pause - before the
  transcript is even final - so in the common case the name is attached
  by the time the turn appears, instead of a beat later. The head start
  is a content-free hint; nothing extra is recorded or sent anywhere,
  and a hint that turns out stale (the speaker kept going) is simply
  discarded.
- Voice identification's limits are now documented for strangers
  (docs/VOICE_ID.md): where the trigger phrases were grown, the English
  bias and what degrades, every tuning knob (threshold, margin, the
  two-part sufficiency bar - all configurable now), similar-sounding
  households, and the scale bounds (roster cap 6 by default,
  single-owner by design).
- Naming is law (#28). A name you set - by renaming a remembered voice
  or just saying it ("her name is spelt Samantha") - is now locked and
  wins everywhere a name appears: the labels on spoken turns, the "In
  the room" chip, what the AIs are told, what memory records, and the
  transcriber's spelling hints. No automatic step can change it back.
  Spelling variants of a name you already know are recognised as the
  same person instead of creating a duplicate with a blank voice
  memory; when the app is not sure, it asks ("Is Sal the same person as
  Sam?") instead of guessing. Renaming one person onto another's name
  offers to merge them - their stored voices combine, the best clips
  are kept, and both names keep working. Forgetting someone still
  sticks: the same name heard later starts fresh.
- The voice health strip (#28). The voice dock and the mobile call
  screen now show a compact readout of what voice identification is
  doing. Four things: whether the on-device matcher is ready, fetching
  its model, or falling back to the cloud; whether the room is on, solo,
  or ambient-listening; each remembered voice's learning progress; and
  how the last spoken turn was identified ("local · 227ms",
  "cloud · 1.9s"). The readout is fed by a new content-free endpoint - states,
  counts and milliseconds only, never names or words - and costs the
  live voice path nothing.
- The room-mode toggle is now durable and honest (#28). Switching it on
  acts exactly like saying "group mode": it sticks to the chat, puts you
  on the roster so voices are identified by the fast on-device matcher,
  and the models are told the true state. Previously the toggle only
  affected the current session, silently skipped the fast matcher, and
  the models would say room mode was off right after you turned it on.
- Room mode is now ambient - no trigger needed (#28). In a voice
  session, every turn gets a quiet on-device voice check: your own voice
  changes nothing, a remembered voice switches room mode on and is named
  automatically, and a clear voice the app cannot place asks who is
  speaking. Because known voices are identified locally at no extra cost,
  the second transcription (and its doubled voice spend) now runs only
  for overlapping speech or a voice the local matcher cannot place. Say
  "solo mode" to keep a session private - that preference sticks until
  you turn room mode back on.

- Room mode obeys spoken and typed commands (#28). Saying "group mode,
  please" (or "room mode on", "multi-user mode") now actually switches
  room mode on, and "solo mode" / "room mode off" / "just me now"
  switches it off - previously the AIs would verbally agree while the
  app did nothing. Detection rides the same background check as spoken
  introductions, so live voice latency is untouched, and a cheap
  confirmation step means talking ABOUT the mode ("is group mode on?")
  changes nothing. Switching off also clears the "In the room" chip and
  ends the doubled transcription in a live session. The AIs are now
  told the current room-mode state and roster each round, so "is group
  mode on?" gets a true answer instead of a guess.
- Room mode identifies known voices locally, part 2 (#28). When more
  than one person is in the room, a known voice is now recognised
  on-device in a fraction of a second - so the models see the right name
  on the turn itself, instead of waiting a second or two for the second
  listen and often reading the turn as you in the meantime. The common
  case where one known person is speaking no longer needs a second
  transcription at all; the second listen still runs whenever two voices
  are present or the match is uncertain, so crosstalk and unknown-voice
  handling are unchanged. The small speaker model (~38MB) is fetched once
  to the data directory, verified, and then runs fully offline - nothing
  about a voice ever leaves the machine. Purely additive: with the model
  or its library absent, or with `CROSSBAND_VOICE_ID_ENABLED=false`, room
  mode behaves exactly as before. Live voice latency is untouched -
  everything here happens on the background pass.
- Room mode label latency, part 1 (#28, night test 4). The name check
  runs a second or two behind the words, so the first AI to answer used
  to read a fresh spoken turn before its name existed and assume it was
  you. A turn whose name is still on the way now reads honestly as
  "Identity pending (in the room)" - the models are told the name is
  still being worked out and not to guess - and any AI speaking later in
  the same round picks up the resolved name the moment it lands. The
  check itself lost its avoidable delays too: labels attach the instant
  the second listen returns instead of on a half-second polling step,
  spend bookkeeping happens after the labels rather than in front of
  them, and the remembered-voice samples that preface every check are
  cached instead of re-read from disk on every spoken turn. Live voice
  latency is untouched - everything here happens on the background pass.
- Room mode arming fixes from the third field test (#28). Two spoken
  triggers that silently did nothing now work. A handover with no name
  ("I'm going to hand over to a guest") switches room mode on and asks
  who the guest is, never inventing a name. A guest introducing
  themselves ("I'm Samantha, Alex's wife, also known as Sam") switches
  it on and adds them under their proper name, keeping the short form
  ("also known as", "call me") as their preferred display name. Every
  introduction check now leaves one plain log line saying what it
  decided, so a silent failure can no longer be mistaken for the check
  not running. And remembered voices can now switch room mode on by
  themselves. When a session starts with room mode off in a household
  with remembered voices, the first couple of spoken turns are also
  transcribed a second time, listening for a known voice or a second
  speaker. Those sessions therefore transcribe their first couple of
  utterances twice. A recognised voice switches room mode on and joins
  the roster with no introduction needed.
- Room mode, phase 4 (#28): honesty about people talking over each
  other, and three fixes from the second field test. When two voices
  land in one spoken turn, the turn now says so: "Two voices at once -
  some words may be missing". On a single microphone the quieter
  person's overlapped words are often simply gone. The models see the
  same note, so they can ask the quieter person to repeat, and such
  turns are never saved to memory as any one person's words. When
  the two voices took turns cleanly rather than overlapping, a
  best-effort split shows who said which words (shown only when the
  second listen agrees with the live transcript; your message text is
  never rewritten). The models are also told plainly what a voice label
  is - text produced by a second listen, not audio they can hear - so a
  seat can no longer claim it "can tell from the voice". Introductions
  stop storing relationship words as names: "this is me, Sam, Shawn's
  wife" now yields a person named Sam, never "Wife" - a
  relationship-only introduction matches a remembered person if the
  sentence names one, and otherwise the app just asks who it is. A
  still-learning voice now shows its progress (seconds heard toward the
  bar) in the remembered-voices panel and the room chip, so waiting is
  an informed choice. And room-mode sessions now capture the mic with
  the browser's single-voice noise tuning switched off (it can muffle
  the second speaker); solo sessions are untouched, and each session's
  capture profile is logged so the experiment can be judged on field
  data.
- Room mode, phase 3 (#28): attribution lands everywhere it matters.
  Voice labels now attach to exactly the turn that was spoken (a quick
  interjection can no longer be labelled onto a neighbouring turn), and
  the models finally SEE the labels. A turn confidently matched to a
  named person reads as that person "(in the room)" in every model's
  view of the chat. An uncertain turn reads as an unidentified speaker:
  never guessed, never silently credited to you. Names stop
  drifting: your own name always comes from the `user_name` setting
  (never from what the transcriber heard), each remembered voice gets
  an editable preferred spelling (Models -> Remembered voices, pencil
  icon), and everyone's names are fed to the live transcriber so it
  spells them consistently. When a chat is saved to memory, guests'
  statements are recorded as that guest - and membro quarantines them
  for review - while anything the app is not sure about is marked as an
  unknown guest rather than being filed as a fact about you.
- Room mode, phase 2 (#28): voices get names, and the introduction is
  the trigger. Saying "my wife Alex is here" (no toggle needed) flips
  room mode on for the chat, adds Alex to the roster, and starts
  learning her voice; "Alex has left" removes her. Voices are
  remembered: a few seconds of each person's clear speech is stored on
  this computer (owner-only files, deletable from Models -> Remembered
  voices with a Forget button that deletes the audio), so a known
  person is recognised in later sessions with no introduction. Turns
  are labelled with names, and below the learning bar a label stays
  marked uncertain. An unrecognised voice raises a "someone new is
  speaking - who?" prompt you answer by just saying the name. A
  background cross-check can flag a turn whose content reads like
  someone else, but it never changes the label. Tap the name on a turn
  to correct it, which also teaches the right voice. An "In the room" chip shows who
  the app is telling apart, which is also the cue that multi-voice
  processing (double transcription spend) is on. The live conversation
  still waits on none of this. Roster size is capped (default 6,
  `CROSSBAND_ROOM_ROSTER_MAX`).
- Room mode, phase 1 (#28): a per-session toggle in the voice controls
  for when more than one person is in the room. While on, each spoken
  turn also goes through a second, diarising transcription pass in the
  background; turns where another voice appears get small unnamed
  "Voice 1" / "Voice 2" chips a moment later. The live conversation is
  untouched - nothing waits on the pass, and with the toggle off the
  voice pipeline is exactly what it was. Stated plainly: telling voices
  apart transcribes the audio twice, so voice minutes roughly double
  while the toggle is on. Labels are best effort for now; naming the
  voices is the next phase.
- A browser gate (#25): an owner password (scrypt verifier, recovery
  secret for enrolment/reset, opaque revocable sessions) now protects
  the UI and API. Enrolment-activated: nothing changes until you set a
  password from the app; after that, every surface asks for it and a
  tailnet caller only ever sees the lock screen. Set
  `CROSSBAND_RECOVERY_SECRET` in `.env` so enrolment and reset work
  without terminal access.
- Passkey unlock (#25): enrol a Touch ID / Face ID passkey from the
  Integrations console and the lock screen offers it first, password
  one click behind. Passkeys are per web address (`localhost` and the
  tailnet name enrol separately; an IP address cannot hold one, so
  `127.0.0.1` keeps the password form), and the tailnet passkey syncs
  to your other devices via your keychain.

## v0.2.0 (2026-08-07)

The rename release: the Sideband-era identifiers are retired.

**Migrating an existing install:** rename the `MMC_` lines in your
`.env` to `CROSSBAND_` (until v0.3 the old names still work and every
use logs the exact rename at startup), and rerun
`bash ops/install-supervisor.sh` if you use the supervisor - it boots
out the old `dev.sideband.server` label and installs
`dev.crossband.server` itself.

- Environment variables moved from `MMC_*` to `CROSSBAND_*`, with a
  one-release fallback and per-variable startup warnings.
- The launchd label is `dev.crossband.server`; the installer migrates a
  pre-rename install automatically.
- The guest diagnostics MCP is `crossband-diag`, so the tool id in the
  tool-activity strip reads `mcp__crossband-diag__get_diagnostic`.
- The app's loggers moved from `mmc.*` to `crossband.*`; if you grep
  `service.log` by logger name, update the pattern.
- Guest worktrees and temporary git refs now use crossband-guest
  namespaces. Old-namespace leftovers are still reclaimed until v0.3:
  stale worktree directories are swept (registered with git or not) and
  orphaned refs/mmc-guest refs are deleted at the next visit to that
  repo. One limit: a guest session begun before the upgrade cannot be
  resumed with continue_last, because its transcript is keyed by the
  old working directory; summon a fresh visit instead.
- The source tag sent to Membro stays `multi-model-chat`, now <!-- secret-scan: allow (historical wire value) -->
  documented as deliberately permanent: Membro keys conversation
  identity on it, and renaming it would fork every open chat's memory
  history.

## v0.1.1 (2026-08-07)

- A chat whose only seat is a trial (unverified-cost) model can now be
  spoken to: explicit addressing reaches trial seats even when it
  covers the whole roster, in both typed and spoken forms. Previously
  such a chat completed rounds with no speakers and no error.
- Source comments and config examples now tell the truth about the
  guest's two modes, and every code_mcp example carries the required
  env key (copying the old example produced a mount that died at
  spawn with nothing telling you why).
- UI and error copy now uses plain punctuation instead of em-dashes,
  matching the docs. Placeholder glyphs (a bare em-dash standing for an
  empty value) are unchanged.
- `./start.sh` now serves the app on a first run. It creates `.env` from
  the example, warns about whichever keys are missing, and starts the
  server, instead of exiting and asking you to rerun it after the venv
  and the frontend build were already done.

## v0.1.0 (2026-08-06)

First public release, under the name Crossband.

- Group chat with several AI models in one shared transcript: each
  provider sees the conversation projected into its own two-party
  format, so seats can address and disagree with each other.
- Detached rounds: generation runs as a background task writing to a
  replayable buffer, so a dropped connection never kills a reply.
- Prompt-cache-aware prompt assembly, with stable content before the
  breakpoint and per-round content after it.
- Shared tools every seat can call: web search, page and Reddit and
  YouTube fetch, GitHub issues and PRs, memory recall and save, and
  self-diagnostics. Results persist and are visible to every
  participant.
- Claude Code as a summonable guest: its own git worktree, read-only by
  default, opt-in implement mode that branches, tests, pushes and opens
  a PR but can never merge.
- Voice in and out through the backend, so the key stays server-side,
  with content-free latency instrumentation.
- Cost accounting with provenance: metered, subscription-equivalent and
  unknown are never summed, and pricing fails closed rather than
  guessing a rate for an unknown model.
- Optional memory through the Membro HTTP contract; absent, the app
  works and forgets.
- Local models via Ollama and LM Studio presets, with no key required.
