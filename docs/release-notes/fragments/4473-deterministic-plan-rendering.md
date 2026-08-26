Plans render deterministically and carry a SHA-256 hash of the rendered form, so two operators rendering the same plan get byte-identical output and a hash they can compare (#3839).
