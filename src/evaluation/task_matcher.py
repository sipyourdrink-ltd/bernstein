def resolve_task_identity(session: dict) -> str:
    """Resolve task identity from stable metadata instead of prompt text."""
    return session.get("task_id") or session.get("lineage_id")