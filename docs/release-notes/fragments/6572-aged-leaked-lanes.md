## Doctor reports aged leaked lanes with their evidence (#6572)

The existing read-only leak check now includes each confirmed lane's recorded
owner statuses and resource-age source in text and structured JSON. Both sweep
kinds use one canonical task replay snapshot. The existing 24-hour environment
default and strict older-than comparison remain; unusable ages warn explicitly.
Unreadable evidence or an invalid threshold fails the doctor check instead of
being displayed as a successful check. This adds no removal authority or
provider probe.
