## Operator-controlled source-to-sensitivity map

An operator-controlled source-to-sensitivity map (`templates/provenance/sensitivity_sources.yaml`, overridable per project) says which class a source's results carry. An unlisted source fails closed to the highest class, and an unrecognised class token is dropped rather than coerced (#5042).
