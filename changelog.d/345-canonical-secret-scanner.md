- The leak scanner is now the fleet's canonical copy (#345). membro and
  spendglass carry `scripts/secret-scan.sh` byte for byte and fail their
  own build when the copy differs, so a pattern fix lands here first and
  is then copied across. Nothing crossband-specific stays in the script:
  the exclusions only this repo needs live in `.secret-scan-exclude`,
  and spendglass's banking-key shape joins the pattern list.
