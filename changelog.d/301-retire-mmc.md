- The pre-v0.2 `MMC_` environment prefix is no longer read (#301), as
  every startup warning since v0.2 promised. An old-name variable whose
  new name is missing now stops the app at startup with the exact
  rename printed, so nothing changes silently; a stale line beside its
  migrated twin just asks to be deleted.
