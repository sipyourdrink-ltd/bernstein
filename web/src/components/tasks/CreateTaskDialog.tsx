import * as Dialog from '@radix-ui/react-dialog';
import { useMutation } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { ApiError, apiPost } from '@/lib/api';

const fieldClass = 'w-full rounded-md border border-meta-foreground bg-background px-3 py-2 text-body text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card';
const buttonClass = 'rounded-md border px-3 py-2 text-body-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card disabled:cursor-not-allowed disabled:bg-secondary disabled:text-muted-foreground';

type TaskInput = { title: string; description: string; role?: string };

class TaskCreationOutcomeUnknown extends Error {}

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
  onRestoreFocus: (outcomeUnknown: boolean) => void;
  onClose: (outcomeUnknown: boolean) => void;
  onCreated: (taskId: string) => void;
}) {
  const titleInput = useRef<HTMLInputElement>(null);
  const promptInput = useRef<HTMLTextAreaElement>(null);
  const submitting = useRef(false);
  const created = useRef(false);
  const [validationError, setValidationError] = useState('');
  const create = useMutation({
    networkMode: 'always',
    retry: false,
    mutationFn: async (task: TaskInput) => {
      if (!navigator.onLine) throw new Error('You are offline. Reconnect and retry.');
      try {
        const response = await apiPost<{ id?: unknown }>('/tasks', task, { signal: AbortSignal.timeout(15_000) });
        const id = response?.id;
        if (typeof id === 'string' && id.length > 0 && id === id.trim()) return id;
      } catch (error) {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500 && error.status !== 408) {
          throw error;
        }
      }
      throw new TaskCreationOutcomeUnknown('Task creation could not be confirmed. Check the task list before retrying.');
    },
    onSuccess: (taskId) => {
      created.current = true;
      onCreated(taskId);
    },
    onSettled: () => { submitting.current = false; },
  });

  const outcomeUnknown = create.error instanceof TaskCreationOutcomeUnknown;

  function close() {
    if (!submitting.current) onClose(outcomeUnknown);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current || outcomeUnknown) return;
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
    submitting.current = true;
    create.mutate({ title, description, ...(role ? { role } : {}) });
  }

  const error = validationError || taskCreationError(create.error);
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) close(); }}>
      <Dialog.Portal>
        <div
          aria-hidden="true"
          className="pointer-events-auto fixed inset-0 z-50 bg-background/75"
          onClick={close}
        />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-auto overscroll-contain rounded-md border border-border bg-card p-5 text-foreground"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (initialFocus === 'prompt' ? promptInput : titleInput).current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (!created.current) onRestoreFocus(outcomeUnknown);
          }}
        >
          <Dialog.Title className="text-h2">Create task</Dialog.Title>
          <Dialog.Description className="mt-1 text-body text-muted-foreground">
            Add a task to this project's queue.
          </Dialog.Description>
          <form onSubmit={submit} className="mt-4 space-y-4" aria-busy={create.isPending}>
            <fieldset disabled={create.isPending} className="space-y-3">
              <label className="block space-y-1 text-body-md">
                <span>Title</span>
                <input ref={titleInput} name="title" required maxLength={500} className={fieldClass} />
              </label>
              <label className="block space-y-1 text-body-md">
                <span>Prompt</span>
                <textarea ref={promptInput} name="prompt" required maxLength={100_000} rows={5} className={fieldClass} />
              </label>
              <label className="block space-y-1 text-body-md">
                <span>Role (optional)</span>
                <input name="role" maxLength={1_000} className={fieldClass} />
              </label>
            </fieldset>
            {error && <p role="alert" className="text-body text-destructive">{error}</p>}
            <div className="flex flex-wrap items-center justify-end gap-2">
              {create.isPending && <span role="status" className="mr-auto text-body text-muted-foreground">Creating task...</span>}
              <Dialog.Close asChild>
                <button type="button" disabled={create.isPending} className={`${buttonClass} border-meta-foreground bg-card hover:bg-secondary`}>
                  Cancel
                </button>
              </Dialog.Close>
              <button type="submit" disabled={create.isPending || outcomeUnknown} className={`${buttonClass} border-primary bg-primary text-primary-foreground hover:bg-primary/90`}>
                Create task
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
