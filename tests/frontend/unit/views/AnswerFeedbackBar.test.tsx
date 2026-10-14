import React from 'react';
import '@testing-library/jest-dom/vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('react-i18next', () => ({
  initReactI18next: { type: '3rdParty', init: () => {} },
  useTranslation: () => ({ t: (key: string) => key }),
}));
const api = vi.fn();
vi.mock('../../../../src/app/apiClient', () => ({ apiRequest: (...args: any[]) => api(...args) }));

/** The last rating posted: [score, comment]. */
const lastPost = () => {
  const body = JSON.parse(api.mock.calls[api.mock.calls.length - 1][1].body);
  return [body.score, body.comment];
};

import { AnswerFeedbackBar } from '../../../../src/views/AnswerFeedbackBar';
import { AnswerFeedback } from '../../../../src/components/ComponentType';

/** A store holding one text turn, updated by updateTextTurn like the real slice. */
function setup(feedback: AnswerFeedback) {
  const store = configureStore({
    reducer: (state: any = { feedback }, action: any) =>
      action.type?.endsWith('updateTextTurn') ? { feedback: action.payload.feedback } : state,
  });
  const Bar = () => {
    const [fb, setFb] = React.useState(store.getState().feedback);
    React.useEffect(() => store.subscribe(() => setFb(store.getState().feedback)), []);
    return <AnswerFeedbackBar turnId="turn1" feedback={fb} />;
  };
  render(<Provider store={store}><Bar /></Provider>);
  return store;
}

const fb: AnswerFeedback = { traceId: 't1', token: 'k' };

describe('AnswerFeedbackBar', () => {
  // braces: a function returned from beforeEach would run as a cleanup hook
  beforeEach(() => { api.mockReset().mockResolvedValue({ data: { recorded: true } }); });

  it('rates 👍, and a second click withdraws it', async () => {
    const store = setup(fb);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chartRec.feedbackGood' })); });
    expect(lastPost()).toEqual([1, undefined]);
    expect(store.getState().feedback.score).toBe(1);
    expect(screen.getByRole('button', { name: 'chartRec.feedbackGood' })).toHaveAttribute('aria-pressed', 'true');
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chartRec.feedbackGood' })); });
    expect(lastPost()).toEqual([null, undefined]);
    expect(store.getState().feedback.score).toBeNull();
  });

  it('👎 asks what was wrong and sends the comment', async () => {
    const store = setup(fb);
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chartRec.feedbackBad' })); });
    expect(lastPost()).toEqual([0, undefined]);
    fireEvent.change(screen.getByLabelText('chartRec.feedbackWhatWasWrong'), { target: { value: ' used 2023 ' } });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chartRec.feedbackSend' })); });
    expect(lastPost()).toEqual([0, 'used 2023']);
    expect(store.getState().feedback).toMatchObject({ score: 0, comment: 'used 2023' });
    expect(screen.queryByLabelText('chartRec.feedbackWhatWasWrong')).toBeNull();
  });

  it('puts the rating back when it could not be sent', async () => {
    api.mockRejectedValue(new Error('503'));
    const store = setup({ ...fb, score: 1 });
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'chartRec.feedbackBad' })); });
    expect(await screen.findByRole('alert')).toHaveTextContent('chartRec.feedbackFailed');
    await waitFor(() => expect(store.getState().feedback.score).toBe(1));
  });
});
