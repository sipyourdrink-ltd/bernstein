import * as Dialog from '@radix-ui/react-dialog';
import { useIsMutating, useMutation } from '@tanstack/react-query';
import { useLayoutEffect, useRef, useState, useSyncExternalStore, type FormEvent } from 'react';
import { ApiError, apiGet, apiPost } from '@/lib/api';

const fieldClass = 'w-full rounded-md border border-meta-foreground bg-background px-3 py-2 text-body text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card';
const buttonClass = 'rounded-md border px-3 py-2 text-body-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card disabled:cursor-not-allowed disabled:bg-secondary disabled:text-muted-foreground aria-disabled:cursor-not-allowed aria-disabled:bg-secondary aria-disabled:text-muted-foreground';

const recoveryKey = 'bernstein.unconfirmed-task-id';
const createMutationKey = ['tasks', 'create-from-web'] as const;
const lookupMutationKey = ['tasks', 'verify-web-create'] as const;

type TaskInput = { id: string; title: string; description: string; role?: string };

const outcomeUnknownMessage = 'Task creation could not be confirmed. Check the task before retrying.';
let inMemoryRecoveryId: string | null = null;
let recoveryDraft: TaskInput | null = null;
let recoveryRejection: string | undefined;
let confirmedRecoveryId: string | null = null;
const recoveryListeners = new Set<() => void>();

function subscribeToRecovery(listener: () => void) {
  recoveryListeners.add(listener);
  return () => { recoveryListeners.delete(listener); };
}

function readRecoveryId(): string | null {
  try {
    const id = window.sessionStorage.getItem(recoveryKey);
    return id && /^[a-f0-9]{32}$/.test(id) ? id : inMemoryRecoveryId;
  } catch {
    return inMemoryRecoveryId;
  }
}

function rememberRecoveryId(id: string | null, rejection?: string) {
  inMemoryRecoveryId = id;
  recoveryRejection = rejection;
  if (!id && !rejection) recoveryDraft = null;
  try {
    if (id) window.sessionStorage.setItem(recoveryKey, id);
    else window.sessionStorage.removeItem(recoveryKey);
  } catch {
    // Keep recovery available while the page is mounted if storage is blocked.
  }
  for (const listener of recoveryListeners) listener();
}

export function useConfirmedTaskCreation() {
  return useSyncExternalStore(subscribeToRecovery, () => confirmedRecoveryId);
}

export function consumeConfirmedTaskCreation(taskId: string): boolean {
  if (confirmedRecoveryId !== taskId) return false;
  confirmedRecoveryId = null;
  for (const listener of recoveryListeners) listener();
  return true;
}

function taskCreationError(error: Error | null): string | undefined {
  if (error instanceof ApiError && error.status === 400) {
    const body = error.body;
    if (body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string') {
      return body.detail;
    }
  }
  return error?.message;
}

export function CreateTaskDialog({
  initialFocus,
  onRestoreFocus,
  onClose,
  onCreated,
}: {
  initialFocus: 'title' | 'prompt';
  onRestoreFocus: () => void;
  onClose: (outcomeUnknown: boolean) => void;
  onCreated: (taskId: string) => void;
}) {
  const formRef = useRef<HTMLFormElement>(null);
  const titleInput = useRef<HTMLInputElement>(null);
  const promptInput = useRef<HTMLTextAreaElement>(null);
  const checkButton = useRef<HTMLButtonElement>(null);
  const submitButton = useRef<HTMLButtonElement>(null);
  const checking = useRef(false);
  const submitting = useRef(false);
  const created = useRef(false);
  const mounted = useRef(false);
  const [validationError, setValidationError] = useState('');
  const storedRecoveryId = useSyncExternalStore(subscribeToRecovery, readRecoveryId);
  const rejection = useSyncExternalStore(subscribeToRecovery, () => recoveryRejection);
  const confirmedId = useConfirmedTaskCreation();
  const [initialRecoveryId] = useState(readRecoveryId);
  const [initialDraft] = useState(() =>
    recoveryDraft && (recoveryDraft.id === storedRecoveryId || rejection) ? recoveryDraft : null,
  );
  const ownCreateId = useRef<string | null>(null);
  const recoveryId = ownCreateId.current === storedRecoveryId && submitting.current ? null : storedRecoveryId;
  const createInFlight = useIsMutating({ mutationKey: createMutationKey }) > 0;
  const lookupInFlight = useIsMutating({ mutationKey: lookupMutationKey }) > 0;
  const busy = createInFlight || lookupInFlight;

  useLayoutEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  function confirm(taskId: string) {
    if (readRecoveryId() !== taskId) return;
    if (!mounted.current) {
      confirmedRecoveryId = taskId;
      rememberRecoveryId(null);
      return;
    }
    created.current = true;
    rememberRecoveryId(null);
    onCreated(taskId);
  }

  useLayoutEffect(() => {
    if (busy || !confirmedId || confirmedId !== initialRecoveryId || !consumeConfirmedTaskCreation(confirmedId)) {
      return;
    }
    created.current = true;
    onCreated(confirmedId);
  }, [busy, confirmedId, initialRecoveryId, onCreated]);

  const create = useMutation({
    mutationKey: createMutationKey,
    networkMode: 'always',
    retry: false,
    mutationFn: async (task: TaskInput) => {
      if (!navigator.onLine) throw new Error('You are offline. Reconnect and retry.');
      recoveryDraft = task;
      rememberRecoveryId(task.id);
      try {
        const response = await apiPost<{ id?: unknown }>('/tasks', task, { signal: AbortSignal.timeout(15_000) });
        if (response?.id === task.id) return task.id;
      } catch (error) {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
          if (readRecoveryId() === task.id) rememberRecoveryId(null, taskCreationError(error));
          throw error;
        }
      }
      throw new Error(outcomeUnknownMessage);
    },
    onSuccess: confirm,
    onSettled: () => { submitting.current = false; },
  });

  const verify = useMutation({
    mutationKey: lookupMutationKey,
    networkMode: 'always',
    retry: false,
    mutationFn: async (taskId: string) => {
      try {
        const task = await apiGet<{ id?: unknown }>(`/tasks/${taskId}`, { signal: AbortSignal.timeout(15_000) });
        if (task?.id === taskId) return taskId;
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) {
          throw new Error('The task is not visible yet. Check again before retrying.');
        }
      }
      throw new Error('The task could not be checked. Try checking again.');
    },
    onSuccess: confirm,
    onSettled: () => { checking.current = false; },
  });

  useLayoutEffect(() => {
    if (recoveryId) checkButton.current?.focus();
    else if (rejection && !busy) titleInput.current?.focus();
  }, [recoveryId, rejection, busy]);

  function close() {
    if (!busy && !submitting.current && !checking.current) onClose(recoveryId !== null);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || submitting.current || recoveryId || readRecoveryId()) return;
    const form = new FormData(event.currentTarget);
    const title = String(form.get('title') ?? '').trim();
    const description = String(form.get('prompt') ?? '').trim();
    const role = String(form.get('role') ?? '').trim();
    if (!title || !description) {
      setValidationError('Enter a title and a prompt.');
      (!title ? titleInput : promptInput).current?.focus();
      return;
    }
    setValidationError('');
    const random = crypto.getRandomValues(new Uint8Array(16));
    const id = Array.from(random, (byte) => byte.toString(16).padStart(2, '0')).join('');
    submitButton.current?.focus();
    submitting.current = true;
    ownCreateId.current = id;
    create.mutate({ id, title, description, ...(role ? { role } : {}) });
  }

  const error = validationError || taskCreationError(verify.error ?? create.error)
    || rejection || (recoveryId ? outcomeUnknownMessage : undefined);
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) close(); }}>
      <Dialog.Portal>
        <div
          aria-hidden="true"
          className="pointer-events-auto fixed inset-0 z-50 bg-background/75"
          onPointerDown={(event) => event.preventDefault()}
          onClick={close}
        />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-auto overscroll-contain rounded-md border border-border bg-card p-5 text-foreground"
          onKeyDown={(event) => {
            if (event.key.toLowerCase() === 'k' && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              event.stopPropagation();
            }
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (recoveryId ? checkButton : initialFocus === 'prompt' ? promptInput : titleInput).current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (!created.current) onRestoreFocus();
          }}
        >
          <Dialog.Title className="text-h2">Create task</Dialog.Title>
          <Dialog.Description className="mt-1 text-body text-muted-foreground">
            Add a task to this project's queue.
          </Dialog.Description>
          <form ref={formRef} onSubmit={submit} className="mt-4 space-y-4" aria-busy={busy}>
            <fieldset disabled={busy || recoveryId !== null} className="space-y-3">
              <label className="block space-y-1 text-body-md">
                <span>Title</span>
                <input ref={titleInput} name="title" defaultValue={initialDraft?.title} required maxLength={500} className={fieldClass} />
              </label>
              <label className="block space-y-1 text-body-md">
                <span>Prompt</span>
                <textarea ref={promptInput} name="prompt" defaultValue={initialDraft?.description} required maxLength={100_000} rows={5} className={fieldClass} />
              </label>
              <label className="block space-y-1 text-body-md">
                <span>Role (optional)</span>
                <input name="role" defaultValue={initialDraft?.role} maxLength={1_000} className={fieldClass} />
              </label>
            </fieldset>
            {error && <p role="alert" className="text-body text-destructive">{error}</p>}
            <div className="flex flex-wrap items-center justify-end gap-2">
              {busy && (
                <span role="status" className="mr-auto text-body text-muted-foreground">
                  {createInFlight ? 'Creating task...' : 'Checking task...'}
                </span>
              )}
              {recoveryId && (
                <>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      if (busy || checking.current) return;
                      if (readRecoveryId() === recoveryId) rememberRecoveryId(null);
                      formRef.current?.reset();
                      if (titleInput.current) titleInput.current.value = '';
                      if (promptInput.current) promptInput.current.value = '';
                      const role = formRef.current?.elements.namedItem('role');
                      if (role instanceof HTMLInputElement) role.value = '';
                      create.reset();
                      verify.reset();
                      requestAnimationFrame(() => titleInput.current?.focus());
                    }}
                    className={`${buttonClass} border-meta-foreground bg-card hover:bg-secondary`}
                  >
                    Start a new task instead
                  </button>
                  <button
                    ref={checkButton}
                    type="button"
                    aria-disabled={busy || undefined}
                    onClick={() => {
                      if (busy || checking.current) return;
                      checkButton.current?.focus();
                      checking.current = true;
                      verify.mutate(recoveryId);
                    }}
                    className={`${buttonClass} border-meta-foreground bg-card hover:bg-secondary`}
                  >
                    Check task
                  </button>
                </>
              )}
              <Dialog.Close asChild>
                <button type="button" disabled={busy} className={`${buttonClass} border-meta-foreground bg-card hover:bg-secondary`}>
                  Cancel
                </button>
              </Dialog.Close>
              <button ref={submitButton} type="submit" disabled={recoveryId !== null} aria-disabled={busy || undefined} className={`${buttonClass} border-primary bg-primary text-primary-foreground hover:bg-primary/90`}>
                Create task
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
