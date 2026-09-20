## The conformance score runs every vector module

`scripts/auditor_conformance.py score` ran a hand-written list of two vector
modules. Four existed: the attribution and authority vectors were never run
there, so the questions they answer were reported as unanswered and a
regression in either file could not fail the conformance build. The script now
takes the vector modules from the directory listing, and a test asserts that
what is on disk is what is scored, so adding a vector file needs no second
edit in a second place.
