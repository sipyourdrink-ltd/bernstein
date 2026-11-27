# Optional render-probe driver capability

`BrowserDriver` remains the basic activity protocol. The structural
`RenderProbeDriver` capability adds one bounded `render_probe` method on
`PlaywrightBrowserDriver` without requiring unrelated or recorded backends to
invent engine-computed values. `RenderProbeResult` contains either the complete
declared environment and computed-style rows or an explicit unavailable reason.

The capture owner supplies stable element-path/selector pairs and a complete
frozen environment. The driver evaluates a fixed property vocabulary through
`getComputedStyle`, checks the actual engine pin and observable environment,
and refuses partial element/property sets. Font bundle content and installed
animation/caret controls are caller-supplied evidence, not inferred from CSS.
The vocabulary identifier changes if the fixed property list changes.

This boundary does not select routes, create base/head worktrees, install
capture controls, anchor receipts, or implement merge policy. Existing render
receipt projection and delta algebra remain the consumer boundary.

Verification uses an in-process page/context fake and existing browser-driver
conformance tests. A local Chromium smoke capture with a repository font and
frozen clock exercises the JavaScript separately from the fake.
