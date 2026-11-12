import * as Dialog from '@radix-ui/react-dialog';
import { useMutation } from '@tanstack/react-query';
import { useRef, useState, type FormEvent } from 'react';
import { apiPost } from '@/lib/api';

const fieldClass = 'w-full rounded-md border border-meta-foreground bg-background px-3 py-2 text-body text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card';
const buttonClass = 'rounded-md border px-3 py-2 text-body-md focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-card disabled:cursor-not-allowed disabled:bg-secondary disabled:text-muted-foreground';

type TaskInput = { title: string; description: string; role?: string };

export function CreateTaskDialog({
  initialFocus,
  trigger,
  onClose,
  onCreated,
}: {
  initialFocus: 'title' | 'prompt';
  trigger: HTMLElement | null;
  onClose: () => void;
  onCreated: (taskId: string) => void;
}) {
  const titleInput = useRef<HTMLInputElement>(null);
  const promptInput = useRef<HTMLTextAreaElement>(null);
  const submitting = useRef(false);
  const created = useRef(false);
  const [validationError, setValidationError] = useState('');
  const create = useMutation({
    mutationFn: async (task: TaskInput) => {
      const response = await apiPost<{ id: string }>('/tasks', task);
      if (!response?.id) throw new Error('Task creation returned no task ID. Check the queue before retrying.');
      return response;
    },
    onSuccess: (task) => {
      created.current = true;
      onCreated(task.id);
    },
    onSettled: () => { submitting.current = false; },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
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

  const error = validationError || create.error?.message;
  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open && !submitting.current) onClose(); }}>
      <Dialog.Portal>
        <div
          aria-hidden="true"
          className="pointer-events-auto fixed inset-0 z-50 bg-background/75"
          onClick={() => { if (!submitting.current) onClose(); }}
        />
        <Dialog.Content
          className="fixed left-1/2 top-1/2 z-50 max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-auto overscroll-contain rounded-md border border-border bg-card p-5 text-foreground"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            (initialFocus === 'prompt' ? promptInput : titleInput).current?.focus();
          }}
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            if (!created.current) trigger?.focus();
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
              <button type="submit" disabled={create.isPending} className={`${buttonClass} border-primary bg-primary text-primary-foreground hover:bg-primary/90`}>
                Create task
              </button>
            </div>
          </form>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
