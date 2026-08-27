## The goose adapter drives more of the CLI it wraps

The adapter reached for two flags of the goose CLI and could not set the model
it was told to use, so a role bound to a specific model silently ran on
whatever the CLI defaulted to. It now passes the model through, parses the
stream the CLI emits, and reports its usage like the other adapters (#3679).
