- A failed GitHub token probe is no longer cached until restart (#243).
  Only a found token is cached, so after `gh auth login` the next page
  load sees it, and the Connections page stops telling a logged-in
  owner to log in.
