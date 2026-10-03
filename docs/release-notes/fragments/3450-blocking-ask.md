## Tasks can block on authenticated mailbox replies

A task can now ask a sibling task a question and cooperatively suspend until
the mailbox records a validated reply and close entry. The authenticated
worker identity and referenced chain entries authorize each cross-task step;
unrelated cross-task endpoints and message kinds remain forbidden. The worker
stays live while waiting, and the mailbox rendezvous pair—not process-local
polling state—is the durable record of the wait and answer (#3450).
