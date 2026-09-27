- A line about a seat's model now says what the next reply runs on
  (#516). A move to a stronger model waits for any reply already on its
  way, so the reply after the line is the first on the new model. A move
  found for a seat whose model changed meanwhile, or after "back to
  normal", is dropped without a line. Changing a seat's model on the
  Models page ends a chat's step-up, and one line before the seat's next
  reply there names the model it's on now.
