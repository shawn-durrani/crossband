- The service log now rotates at boot once it passes 10MB (#243). It
  grew without bound under the supervisor. One prior generation is kept
  beside it as service.log.1.
