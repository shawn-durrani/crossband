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
