// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Follow-up message queue for the analyst chat.
 *
 * While the agent is running, the chat box stays editable: pressing Enter
 * queues the message instead of interrupting the run. When the run ends, the
 * queue decides what happens next based on HOW it ended:
 *
 *   completed  → send the next queued message automatically
 *   paused     → the agent asked the user a question; wait (the queued text is
 *                never mistaken for the answer). Sending resumes once the user
 *                has answered and that run completes.
 *   error /
 *   cancelled  → hold the queue; the user resumes it explicitly
 *
 * Everything here is pure so it can be unit-tested; the component owns state.
 */

export interface QueuedMessage {
    id: string;
    text: string;
}

export type RunOutcome = 'running' | 'completed' | 'paused' | 'error' | 'cancelled';

export type QueueAction =
    | { kind: 'idle' }          // nothing to do
    | { kind: 'send'; message: QueuedMessage; rest: QueuedMessage[] }
    | { kind: 'wait-answer' }   // run paused on a question
    | { kind: 'hold' };         // run failed / was stopped; user must resume

let counter = 0;
export function newQueuedMessage(text: string): QueuedMessage {
    counter += 1;
    return { id: `queued-${Date.now()}-${counter}`, text };
}

export function enqueue(queue: QueuedMessage[], text: string): QueuedMessage[] {
    const trimmed = text.trim();
    if (!trimmed) return queue;
    return [...queue, newQueuedMessage(trimmed)];
}

/** Edit a queued message; an emptied message is removed. */
export function updateQueued(queue: QueuedMessage[], id: string, text: string): QueuedMessage[] {
    const trimmed = text.trim();
    if (!trimmed) return removeQueued(queue, id);
    return queue.map(m => (m.id === id ? { ...m, text: trimmed } : m));
}

export function removeQueued(queue: QueuedMessage[], id: string): QueuedMessage[] {
    return queue.filter(m => m.id !== id);
}

/**
 * What to do with the queue now that the chat is idle.
 *
 * @param held   the user has not yet resumed a queue held after an error/stop
 * @param canStart whether a run can start right now (e.g. a table is focused)
 */
export function nextQueueAction(
    queue: QueuedMessage[],
    lastOutcome: RunOutcome | null,
    held: boolean,
    canStart: boolean,
): QueueAction {
    if (queue.length === 0) return { kind: 'idle' };
    if (lastOutcome === 'running') return { kind: 'idle' };
    if (lastOutcome === 'paused') return { kind: 'wait-answer' };
    if (held || lastOutcome === 'error' || lastOutcome === 'cancelled' || !canStart) {
        return { kind: 'hold' };
    }
    const [message, ...rest] = queue;
    return { kind: 'send', message, rest };
}
