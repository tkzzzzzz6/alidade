// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * 👍 / 👎 on an agent's answer, sent to LangSmith as feedback on the run's
 * trace (`POST /api/agent/feedback`). Answers only carry a trace when the
 * server runs with ALIDADE_LANGSMITH=1, so the buttons only appear then.
 */

import { AnswerFeedback } from '../components/ComponentType';
import { apiRequest } from './apiClient';
import { getUrls } from './utils';

export type Rating = 0 | 1 | null;

/** The rating after clicking `clicked`: clicking the current one withdraws it. */
export function nextRating(current: Rating | undefined, clicked: 0 | 1): Rating {
    return current === clicked ? null : clicked;
}

/** The feedback a completion event offers, if any. */
export function feedbackFromEvent(event: any): AnswerFeedback | undefined {
    if (!event?.trace_id || !event?.feedback_token) return undefined;
    return { traceId: String(event.trace_id), token: String(event.feedback_token) };
}

export async function sendAnswerFeedback(fb: AnswerFeedback, score: Rating, comment?: string): Promise<boolean> {
    const { data } = await apiRequest<{ recorded: boolean }>(getUrls().AGENT_FEEDBACK, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            trace_id: fb.traceId, feedback_token: fb.token, score,
            ...(comment ? { comment } : {}),
        }),
    });
    return !!data?.recorded;
}
