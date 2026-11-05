## SSE slots remain occupied until streams close

The shared concurrency cap for `/events`, `/events/cost` and `/events/thread`
now holds each slot for the streaming body's lifetime. Requests above the
cap receive 429 while existing streams remain open; completion, disconnect,
cancellation and stream errors release the slot (#6866).
