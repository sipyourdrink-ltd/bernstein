## Type remote status and cancellation observations explicitly

RuntimeBridge now offers optional typed remote status and cancellation
capabilities that refuse unsupported calls without invoking legacy methods.
Frozen DTOs distinguish unknown and waiting states from observed terminal
lifecycle, and unconfirmed cancellation from confirmed cancellation. This
partial component preserves existing bridges; runtime integration and the
observed-event journal remain separate work (#6992).
