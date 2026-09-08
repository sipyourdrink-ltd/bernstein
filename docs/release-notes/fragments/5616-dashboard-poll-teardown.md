## Dashboard polls stop updating once app shutdown begins

A background dashboard poll could finish while Textual was dismantling the
screen tree and dispatch its success message before the app message queue
closed. Depending on timing, the handler then queried a removed tasks table or
read focus after the screen stack was empty, raising `NoMatches` or
`ScreenStackError` during shutdown. Poll results are now ignored as soon as the
app stops running.

(#5616)
