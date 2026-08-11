"""Journal event name for the injected-skills audit trail (issue #3383).

Kept in its own module, mirroring context_attachments.CONTEXT_FILES_ATTACHED_EVENT,
so orchestrator.py's import list stays consistent between the two features.
"""

SKILLS_INJECTED_EVENT = "skills.injected"
