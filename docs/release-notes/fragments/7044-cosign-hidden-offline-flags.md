## Preserve offline wheelhouse signing with cosign 3.1.4

Probe offline compatibility flags by parsing them with `--help`, so flags
hidden from the displayed help still apply. The signing script and real
airgap tests retain the existing detached signatures and offline behavior
when upgrading cosign to 3.1.4 (#7044).
