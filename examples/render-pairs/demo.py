"""Compare constructed render receipts for a title under two environments."""

import json
from dataclasses import replace

from bernstein.core.evidence.render_receipt import (
    ComputedStyle,
    EnvironmentDescriptor,
    RenderReceipt,
    Viewport,
    render_delta,
)


def main() -> None:
    environment = EnvironmentDescriptor(engine_build_identity="demo/1", viewport=Viewport(800, 600), locale="en-US")
    base = RenderReceipt(
        route="/report",
        viewport=environment.viewport,
        declared_state="ready",
        environment=environment,
        computed_styles=(ComputedStyle("main.title", {"color": "navy"}),),
        unstable_properties={"cssSubgrid": "enabled"},
    )
    head = replace(base, computed_styles=(ComputedStyle("main.title", {"color": "teal"}),))
    other_locale = replace(
        head,
        environment=replace(environment, locale="fr-FR"),
        unstable_properties={"cssSubgrid": "disabled"},
    )

    pairs = {
        "changed": render_delta(base, head).to_dict(),
        "incomparable": render_delta(base, other_locale).to_dict(),
    }
    print(json.dumps(pairs, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
