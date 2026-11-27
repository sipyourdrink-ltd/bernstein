# Compare two render receipts

Call `render_delta(base, head)` with two `RenderReceipt` objects. Both receipts
must declare an environment with the same descriptor hash and agree on the
receipt version, route, viewport, declared state, and property vocabulary
version. Conflicting values for one element property within either receipt
also prevent comparison. [`render_delta` source][compare]

| Result | How to recognize it | What to read |
| --- | --- | --- |
| Clean | `result.is_clean` | The receipts agree on every observed property. |
| Changed | `result.deltas` is nonempty | Each entry gives the element, property, and its `before` and `after` values. A missing side is `None`. |
| Incomparable | `result.is_incomparable` | `result.incomparable_reason` names the failed check. The delta tuple is empty. |

Changes are sorted lexicographically by element path and then by namespaced
property. Receipt-wide unstable properties have an empty element path.

    >>> from dataclasses import replace
    >>> from bernstein.core.evidence.render_receipt import EnvironmentDescriptor, RenderReceipt, render_delta
    >>> base = RenderReceipt(route="/cart", environment=EnvironmentDescriptor(engine_build_identity="example"))
    >>> render_delta(base, base).is_clean
    True
    >>> head = replace(base, unstable_properties={"font-fallback": "serif"})
    >>> result = render_delta(base, head)
    >>> [(d.element_path, d.property, d.before, d.after) for d in result.deltas]
    [('', 'unstable.font-fallback', None, 'serif')]
    >>> render_delta(base, replace(head, environment=None)).incomparable_reason
    'environment not declared on both receipts'

`base_unstable_properties` and `head_unstable_properties` hold each side's
complete, sorted tuple of property/value pairs, including unchanged values.
Both snapshots are available for all three results. `RenderDelta.to_dict()`
writes each nonempty snapshot as an object and omits each empty field.
`RenderDelta.from_dict()` reads an absent field in a legacy result as an empty
tuple. [`RenderDelta` source][result]

    >>> result.to_dict()["head_unstable_properties"]
    {'font-fallback': 'serif'}
    >>> "base_unstable_properties" in result.to_dict()
    False

## Find the first difference in flattened mappings

`first_property_difference(base, head)` takes mappings from
`(element_path, property)` to captured values. It returns the first changed,
added, or removed key in the same lexicographic order, or `None` for equal
mappings. The helper performs no capture-environment validation. Use
`render_delta` when you need that check. [`first_property_difference` source][first]

    >>> from bernstein.core.evidence.render_receipt import first_property_difference
    >>> before = {("#price", "style.color"): "black", ("#cart", "layout.paint_order"): "1"}
    >>> first_property_difference(before, {("#price", "style.color"): "navy"})
    ('#cart', 'layout.paint_order')

[compare]: https://github.com/sipyourdrink-ltd/bernstein/blob/780d77fdce93ab9693f135d980fb2b28abb8df92/src/bernstein/core/evidence/render_receipt.py#L743-L805
[result]: https://github.com/sipyourdrink-ltd/bernstein/blob/780d77fdce93ab9693f135d980fb2b28abb8df92/src/bernstein/core/evidence/render_receipt.py#L643-L713
[first]: https://github.com/sipyourdrink-ltd/bernstein/blob/780d77fdce93ab9693f135d980fb2b28abb8df92/src/bernstein/core/evidence/render_receipt.py#L571-L588
