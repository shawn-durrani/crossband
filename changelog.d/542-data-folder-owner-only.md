- The data folder is private to your own account on the Mac (#542).
  The chat database, its backups, attachments, voice diagnostic dumps
  and the service log could be read by any other account on the machine.
  At startup the app now takes that access away from everything already
  in the folder, and every file it makes after that is private from the
  start. The stored-pass repair script does the same for its safety
  copy and journal.
