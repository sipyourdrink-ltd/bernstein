"""Backward-compat shim: module moved to bernstein.core.quality.review_checklist."""

from bernstein.core._shim import install_shim

install_shim(__name__, "bernstein.core.quality.review_checklist")
