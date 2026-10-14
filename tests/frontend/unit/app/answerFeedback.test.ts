import { describe, expect, it, vi } from 'vitest';

vi.mock('../../../../src/app/apiClient', () => ({ apiRequest: vi.fn(async () => ({ data: { recorded: true } })) }));

import { apiRequest } from '../../../../src/app/apiClient';
import { feedbackFromEvent, nextRating, sendAnswerFeedback } from '../../../../src/app/answerFeedback';

describe('answer feedback', () => {
  it('toggles: clicking the chosen thumb withdraws it', () => {
    expect(nextRating(undefined, 1)).toBe(1);
    expect(nextRating(null, 0)).toBe(0);
    expect(nextRating(1, 1)).toBeNull();
    expect(nextRating(1, 0)).toBe(0);
  });

  it('reads the trace from a completion event, only when there is one', () => {
    expect(feedbackFromEvent({ type: 'completion', trace_id: 't1', feedback_token: 'k' }))
      .toEqual({ traceId: 't1', token: 'k' });
    expect(feedbackFromEvent({ type: 'completion' })).toBeUndefined();
    expect(feedbackFromEvent({ type: 'completion', trace_id: 't1' })).toBeUndefined();
  });

  it('posts the rating with its token', async () => {
    expect(await sendAnswerFeedback({ traceId: 't1', token: 'k' }, 0, 'wrong year')).toBe(true);
    const [url, init] = (apiRequest as any).mock.calls[0];
    expect(url).toContain('/api/agent/feedback');
    expect(JSON.parse(init.body)).toEqual({ trace_id: 't1', feedback_token: 'k', score: 0, comment: 'wrong year' });
  });
});
