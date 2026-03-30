# F93 — Voice Command Layer

**Priority:** P5
**Scope:** medium (15 min for skeleton/foundation)
**Wave:** 5 — Future-Proofing 2030-2035

## Problem
Users must type CLI commands manually, which is slower and less accessible than speaking natural language instructions, especially during hands-busy workflows.

## Solution
- Implement `bernstein listen` command that starts a speech-to-text session using whisper.cpp running locally
- Continuously capture audio from default microphone, transcribe in real-time
- Map recognized utterances to CLI actions via a command grammar (e.g., "Run three agents on the auth refactor" maps to `bernstein run -g "auth refactor" -j 3`)
- Support core commands: run, status, stop, list agents, show results
- Provide audio feedback (terminal bell or TTS) on command recognition and completion
- Allow custom voice command aliases in `~/.bernstein/voice.yaml`
- Include a `--dry-run` mode that shows the parsed command without executing

## Acceptance
- [ ] `bernstein listen` starts local speech-to-text via whisper.cpp
- [ ] Natural language mapped to CLI commands via command grammar
- [ ] Core commands supported: run, status, stop, list, show
- [ ] Custom voice aliases configurable in `~/.bernstein/voice.yaml`
- [ ] `--dry-run` mode displays parsed command without execution
- [ ] Audio feedback on command recognition
- [ ] Works offline with no cloud dependency for transcription
