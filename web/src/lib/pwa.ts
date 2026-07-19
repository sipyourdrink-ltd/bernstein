/**
 * PWA registration helpers.
 *
 * Boots the service worker registered at ``/sw.js`` so the SPA gains
 * offline-first behaviour for the read-only API endpoints documented in
 * the SW source.  Also exposes:
 *
 *   - {@link onlineSignal}        - subscribe to navigator online/offline
 *   - {@link captureInstallPrompt} - capture the beforeinstallprompt event
 *                                     so a custom "Install app" button can
 *                                     trigger the OS-native install flow
 *
 * All helpers are dependency-free so they unit-test cleanly under jsdom
 * + vitest. Each function exits early when `window` or `navigator` is
 * undefined (SSR / non-browser test environments) instead of throwing.
 */

export interface BeforeInstallPromptEvent extends Event {
  readonly platforms: ReadonlyArray<string>;
  readonly userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
  prompt(): Promise<void>;
}

let _deferredPrompt: BeforeInstallPromptEvent | null = null;

/** Result of a service worker registration attempt. */
export interface RegistrationResult {
  registered: boolean;
  reason?: string;
}

/**
 * Register the service worker if the runtime supports it.
 *
 * The registration is deferred until ``window.load`` to avoid contending
 * with the SPA's initial paint. The returned promise resolves to a
 * {@link RegistrationResult} so callers (and tests) can introspect the
 * outcome without listening for global events.
 *
 * @param scriptUrl path to the SW script, defaults to ``/sw.js``.
 * @param scope     control scope (defaults to ``/``).
 */
export async function registerServiceWorker(
  scriptUrl: string = '/sw.js',
  scope: string = '/',
): Promise<RegistrationResult> {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') {
    return { registered: false, reason: 'no-window' };
  }
  if (!('serviceWorker' in navigator)) {
    return { registered: false, reason: 'unsupported' };
  }
  try {
    await navigator.serviceWorker.register(scriptUrl, { scope });
    return { registered: true };
  } catch (err) {
    return { registered: false, reason: (err as Error)?.message ?? 'register-failed' };
  }
}

/**
 * Hook ``beforeinstallprompt`` so the SPA can present a custom install
 * button.  The captured event is stashed on a module-local variable; call
 * {@link triggerInstallPrompt} to surface the OS prompt later.
 */
export function captureInstallPrompt(): void {
  if (typeof window === 'undefined') return;
  window.addEventListener('beforeinstallprompt', (event) => {
    event.preventDefault();
    _deferredPrompt = event as BeforeInstallPromptEvent;
  });
  window.addEventListener('appinstalled', () => {
    _deferredPrompt = null;
  });
}

/**
 * Show the deferred install prompt if one was captured.
 *
 * @returns The user's choice or ``null`` if no prompt was ever captured.
 */
export async function triggerInstallPrompt(): Promise<
  { outcome: 'accepted' | 'dismissed'; platform: string } | null
> {
  if (!_deferredPrompt) return null;
  const ev = _deferredPrompt;
  _deferredPrompt = null;
  await ev.prompt();
  return ev.userChoice;
}

/** Whether a deferred install prompt is currently available. */
export function hasInstallPrompt(): boolean {
  return _deferredPrompt !== null;
}

/**
 * Subscribe to network connectivity changes.
 *
 * Calls ``listener(true)`` on online, ``listener(false)`` on offline. The
 * listener is also invoked once synchronously with the current value so
 * callers do not need to read ``navigator.onLine`` separately.
 *
 * @returns A teardown function that detaches the listeners.
 */
export function onlineSignal(listener: (online: boolean) => void): () => void {
  if (typeof window === 'undefined' || typeof navigator === 'undefined') {
    return () => {};
  }
  listener(navigator.onLine);
  const on = () => listener(true);
  const off = () => listener(false);
  window.addEventListener('online', on);
  window.addEventListener('offline', off);
  return () => {
    window.removeEventListener('online', on);
    window.removeEventListener('offline', off);
  };
}

/**
 * Extract the auth token from the URL fragment (``#t=<token>``) and stash
 * it in ``localStorage``.  The fragment is then scrubbed from the URL so
 * the token never appears in shared screenshots or browser history.
 *
 * The default ``storageKey`` MUST match ``TOKEN_KEY`` in ``lib/api.ts``
 * (`'bernstein_token'`): the onboarding fragment is dead weight if the
 * capture writes to a key the API layer never reads. This bit the local
 * ``bernstein gui serve`` seed and the ``--tunnel`` onboarding flow - the
 * token was captured into a key nothing consumed, so every ``/api/v1`` call
 * still 401'd.
 *
 * Returns the token that was captured, or ``null`` if no token was found.
 */
export function captureAuthTokenFromFragment(storageKey: string = 'bernstein_token'): string | null {
  if (typeof window === 'undefined') return null;
  const hash = window.location.hash;
  if (!hash || !hash.startsWith('#t=')) return null;
  const token = hash.slice(3);
  if (!token) return null;
  try {
    window.localStorage.setItem(storageKey, token);
  } catch {
    // localStorage may be unavailable in private-browsing modes; swallow
    // the error and still scrub the fragment so the token does not linger
    // in the URL bar.
  }
  const url = new URL(window.location.href);
  url.hash = '';
  window.history.replaceState(null, '', url.toString());
  return token;
}
