# Voice control

`bernstein listen` is an experimental voice front-end: capture audio
from your microphone, transcribe it locally with `faster-whisper`,
match the result against a small grammar plus a user-defined alias
file, and either print or run the resulting Bernstein CLI command.

The [feature matrix](../reference/FEATURE_MATRIX.md) flags this as **experimental**. The
parser only knows a handful of phrases, the audio capture loop is a
single thread, and there is no wake-word - once started, every
utterance above the silence threshold is transcribed and matched. Plan
accordingly.

The CLI lives at `cli/commands/voice_cmd.py:437` (`@click.command("listen")`).

---

## What `bernstein listen` does

The command is a thin loop (`cli/commands/voice_cmd.py:378-429`):

1. Wait for audio above the RMS silence threshold.
2. Record until the speaker pauses (`max_silence_chunks` of silence).
3. Pass the audio to a `faster_whisper.WhisperModel` and read back
   text (`voice_cmd.py:244-280`).
4. Look up the text in `~/.bernstein/voice.yaml` (exact, then prefix
   match) (`voice_cmd.py:175-201`).
5. If no alias matched, walk the built-in regex grammar
   (`voice_cmd.py:70-124`).
6. With `--dry-run`, print the parsed CLI command. Otherwise, exec it
   via `subprocess.run(shlex.split(cmd), check=False)`
   (`voice_cmd.py:419-427`).

The base command used for built-in patterns is
`{sys.executable} -m bernstein` (`voice_cmd.py:166-172`). Alias
strings are passed through verbatim.

---

## Setup

```
pip install faster-whisper sounddevice numpy
```

Requirements (`voice_cmd.py:283-296`):

- `sounddevice` - microphone capture (PortAudio under the hood).
- `numpy` - audio buffers.
- `faster-whisper` - local STT. The first run downloads the chosen
  model (`tiny` ≈ 39 MB, `base` ≈ 150 MB, `small` ≈ 460 MB; larger
  models scale up). The model is cached under
  `~/.cache/huggingface/`.

Platform notes:

- Linux: install PortAudio dev headers (`libportaudio2` on Debian /
  Ubuntu, `portaudio` on Arch).
- macOS: `brew install portaudio` is enough.
- Windows: `pip install sounddevice` ships prebuilt wheels.

The CLI exits with a clear install hint if any of the three deps are
missing (`voice_cmd.py:271-296`).

---

## Commands recognized

The default grammar (`voice_cmd.py:70-124`) maps phrases to CLI
invocations:

| Said                                       | Runs                             |
| ------------------------------------------ | -------------------------------- |
| `run three agents on the auth refactor`    | `bernstein -g "the auth refactor" -j 3` |
| `run agents on add tests for parser`       | `bernstein -g "add tests for parser"`   |
| `run deploy to production`                 | `bernstein -g "deploy to production"`   |
| `status` / `show status`                   | `bernstein status`               |
| `stop` / `stop agents` / `stop all`        | `bernstein stop`                 |
| `list agents` / `show agents`              | `bernstein agents list`          |
| `recap` / `show recap` / `show results`    | `bernstein recap`                |
| `logs` / `show logs`                       | `bernstein logs`                 |
| `cost` / `show cost`                       | `bernstein cost`                 |
| `plan` / `show plan`                       | `bernstein plan`                 |
| `help`                                     | `bernstein --help`               |

Number words `one` through `ten` are accepted in the worker count
(`voice_cmd.py:33-58`) - "run three agents" and "run 3 agents" both
parse to `-j 3`.

### Custom aliases

Drop a YAML file at `~/.bernstein/voice.yaml` (override with
`--alias-file`):

```yaml
deploy prod: bernstein -g "deploy to production"
check tests: bernstein -g "run full test suite"
```

The lookup is done in lowercase. Exact matches win first; otherwise
the parser walks the alias keys looking for a prefix match against the
utterance (`voice_cmd.py:189-198`). If your alias starts with the same
words as a built-in phrase, the alias wins.

The alias file is parsed with `yaml.safe_load` and entries with
non-string values are silently ignored (`voice_cmd.py:148-158`). A
malformed file prints a yellow warning and the daemon falls back to
no aliases.

### CLI flags

```
bernstein listen [--dry-run]
                 [--model tiny|base|small|medium|large-v2]
                 [--alias-file PATH]
                 [--threshold 0.01]
                 [--min-duration 0.5]
```

- `--dry-run` - print the parsed command without executing
  (`voice_cmd.py:438-444`).
- `--model` - whisper model size. Smaller is faster, larger is more
  accurate. Default `base` (`voice_cmd.py:445-452`).
- `--alias-file` - alternate path to `voice.yaml`. Default
  `~/.bernstein/voice.yaml` (`voice_cmd.py:27`,
  `voice_cmd.py:453-460`).
- `--threshold` - RMS amplitude threshold for speech vs silence.
  Higher = need to speak louder (`voice_cmd.py:462-467`).
- `--min-duration` - minimum utterance length in seconds before
  transcription is attempted (`voice_cmd.py:469-475`). Avoids
  transcribing single noises.

---

## Privacy: local-vs-cloud STT

`bernstein listen` is **local-only**. `faster-whisper` ships an offline
inference engine; no audio is sent anywhere. The model weights download
once on first use and live under `~/.cache/huggingface/`. This is
deliberate (`voice_cmd.py:1-8`):

- No transcripts of operator commands hit a cloud STT vendor.
- No corporate codebase names, ticket IDs, or CI tokens (heard in the
  background of the mic) are exfiltrated.
- The transcription quality is still good enough for the short imperative
  phrases the parser knows about.

There is no telemetry of utterances or matched commands; the only
output is the rendered command on stdout and (without `--dry-run`) the
spawned `bernstein` subprocess.

---

## Limitations

This subsystem is **experimental** and has known gaps:

- **No wake word.** Once started, every utterance above the silence
  threshold is transcribed and dispatched. Run inside a quiet room or
  use `--dry-run` while you tune.
- **Tiny grammar.** Only the eleven patterns above plus your alias file.
  Anything outside the grammar prints `No matching command. Try:
  run/status/stop/list agents/recap.` (`voice_cmd.py:412-414`) and is
  ignored.
- **English only.** Whisper supports more languages, but the grammar
  patterns are English-only and the transcribe call hard-codes
  `language="en"` (`voice_cmd.py:254`).
- **Single mic, default device.** The `sd.InputStream` uses whatever
  PortAudio considers the default input. There is no flag to pick a
  device.
- **No live partials.** Each utterance is recorded fully, then
  transcribed end-to-end. There is a perceptible pause between speech
  and command dispatch.
- **Subprocess shell is your default `bernstein`.** Voice does not
  share state with an existing `bernstein run` process; it just spawns
  a new CLI invocation per utterance. If you say "stop", that is a
  fresh `bernstein stop` against the current working directory.
- **No safe-mode for destructive commands.** `bernstein listen` will
  happily fire `bernstein stop` from a noisy meeting if the word "stop"
  is heard. Use `--dry-run` until you are sure of the environment.
- **Alias matching is naïve.** Lowercased exact-or-prefix only. There
  is no fuzzy matching, no synonyms, no diacritic folding.
- **No daemon mode.** The command runs in the foreground until
  `Ctrl-C`. Wrap in `tmux` / `nohup` / a launchd or systemd unit if you
  want it to survive a terminal close.

If voice control becomes load-bearing for your workflow, consider
contributing additional grammar entries and / or a wake-word stage.
The architecture in `voice_cmd.py` is small enough to extend without a
larger refactor.

---

## Code pointers

- `cli/commands/voice_cmd.py:437` - `@click.command("listen")` entry
  point.
- `cli/commands/voice_cmd.py:27` - `_DEFAULT_ALIAS_FILE` =
  `~/.bernstein/voice.yaml`.
- `cli/commands/voice_cmd.py:33-58` - number-word table and worker
  count parsing.
- `cli/commands/voice_cmd.py:70-124` - built-in grammar
  (`(regex, command_template)` pairs).
- `cli/commands/voice_cmd.py:131-158` - `_load_aliases` (`yaml.safe_load`).
- `cli/commands/voice_cmd.py:166-172` - `_base_command()` =
  `{sys.executable} -m bernstein`.
- `cli/commands/voice_cmd.py:175-225` - `parse_utterance` (alias →
  grammar fallback).
- `cli/commands/voice_cmd.py:244-280` - whisper transcription helpers,
  `_load_whisper_model`.
- `cli/commands/voice_cmd.py:283-296` - sounddevice / numpy import +
  install hint.
- `cli/commands/voice_cmd.py:299-370` - record-until-silence loop and
  `_capture_and_transcribe`.
- `cli/commands/voice_cmd.py:378-429` - main `_listen_loop` with
  `--dry-run` and subprocess dispatch.
- `docs/reference/FEATURE_MATRIX.md` - flags `bernstein listen` as
  Voice commands (experimental).
