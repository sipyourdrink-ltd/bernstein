### Fixed

- Audit view no longer renders "None" for missing downgrade reason. JSON `null` in `downgrade_reason` now projects to empty string (no downgrade shown). Non-string values now raise a validation error instead of being rendered as Python repr. ([#4918](https://github.com/sipyourdrink-ltd/bernstein/issues/4918))
