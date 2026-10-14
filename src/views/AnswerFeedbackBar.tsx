// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * 👍 / 👎 under an agent's answer. The rating goes to the run's LangSmith
 * trace (see `app/answerFeedback.ts`) and is kept on the turn, so it shows
 * again when the answer is reopened. Clicking the chosen thumb withdraws the
 * rating; after a 👎 the user can say what was wrong (optional).
 */

import React, { FC, ReactNode, useState } from 'react';
import { useDispatch } from 'react-redux';
import { useTranslation } from 'react-i18next';
import { Box, Button, IconButton, TextField, Tooltip, Typography } from '@mui/material';
import ThumbUpAltOutlinedIcon from '@mui/icons-material/ThumbUpAltOutlined';
import ThumbUpAltIcon from '@mui/icons-material/ThumbUpAlt';
import ThumbDownAltOutlinedIcon from '@mui/icons-material/ThumbDownAltOutlined';
import ThumbDownAltIcon from '@mui/icons-material/ThumbDownAlt';

import { AnswerFeedback } from '../components/ComponentType';
import { dfActions } from '../app/dfSlice';
import { nextRating, Rating, sendAnswerFeedback } from '../app/answerFeedback';
import { textVar } from '../app/layout';

export const AnswerFeedbackBar: FC<{ turnId: string; feedback: AnswerFeedback; extra?: ReactNode }> = ({ turnId, feedback, extra }) => {
    const dispatch = useDispatch();
    const { t } = useTranslation();
    const [busy, setBusy] = useState(false);
    const [failed, setFailed] = useState(false);
    const [comment, setComment] = useState(feedback.comment || '');
    const [commentSent, setCommentSent] = useState(!!feedback.comment);
    const score = feedback.score ?? null;

    const save = (patch: Partial<AnswerFeedback>) =>
        dispatch(dfActions.updateTextTurn({ id: turnId, feedback: { ...feedback, ...patch } }));

    const send = async (next: Rating, text?: string) => {
        const before = { score: feedback.score ?? null, comment: feedback.comment };
        save({ score: next, ...(text !== undefined ? { comment: text } : {}) });   // optimistic
        setBusy(true);
        setFailed(false);
        try {
            await sendAnswerFeedback(feedback, next, text);
            return true;
        } catch {
            save(before);
            setFailed(true);
            return false;
        } finally {
            setBusy(false);
        }
    };

    const rate = (clicked: 0 | 1) => {
        const next = nextRating(score, clicked);
        setCommentSent(false);
        if (next !== 0) setComment('');
        void send(next, next === null ? '' : undefined);
    };

    const sendComment = async () => {
        if (await send(0, comment.trim())) setCommentSent(true);
    };

    const thumb = (value: 0 | 1) => {
        const on = score === value;
        const Icon = value === 1
            ? (on ? ThumbUpAltIcon : ThumbUpAltOutlinedIcon)
            : (on ? ThumbDownAltIcon : ThumbDownAltOutlinedIcon);
        const label = t(value === 1 ? 'chartRec.feedbackGood' : 'chartRec.feedbackBad');
        return (
            <Tooltip title={label}>
                <span>
                    <IconButton size="small" aria-label={label} aria-pressed={on} disabled={busy}
                        onClick={(e) => { e.stopPropagation(); rate(value); }}
                        sx={{ p: 0.25, color: on ? (value === 1 ? 'success.main' : 'error.main') : 'text.disabled' }}>
                        <Icon sx={{ fontSize: 16 }} />
                    </IconButton>
                </span>
            </Tooltip>
        );
    };

    return (
        <Box data-testid="answer-feedback" sx={{ pl: '20px', pr: '8px', pb: '6px' }}
             onClick={(e) => e.stopPropagation()}>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
                {thumb(1)}
                {thumb(0)}
                {score !== null && !failed && (
                    <Typography sx={{ fontSize: textVar.xs, color: 'text.secondary', ml: 0.5 }}>
                        {t('chartRec.feedbackThanks')}
                    </Typography>
                )}
                {failed && (
                    <Typography role="alert" sx={{ fontSize: textVar.xs, color: 'error.main', ml: 0.5 }}>
                        {t('chartRec.feedbackFailed')}
                    </Typography>
                )}
                {extra && <Box sx={{ ml: 'auto' }}>{extra}</Box>}
            </Box>
            {score === 0 && !commentSent && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, mt: 0.5 }}>
                    <TextField size="small" fullWidth value={comment} disabled={busy}
                        placeholder={t('chartRec.feedbackWhatWasWrong')}
                        inputProps={{ maxLength: 2000, 'aria-label': t('chartRec.feedbackWhatWasWrong') }}
                        onChange={(e) => setComment(e.target.value)}
                        onKeyDown={(e) => { if (e.key === 'Enter' && comment.trim()) void sendComment(); }}
                        sx={{ '& .MuiInputBase-input': { fontSize: textVar.xs, py: '4px' } }} />
                    <Button size="small" disabled={busy || !comment.trim()} onClick={() => void sendComment()}
                        sx={{ fontSize: textVar.xs, minWidth: 0, textTransform: 'none' }}>
                        {t('chartRec.feedbackSend')}
                    </Button>
                </Box>
            )}
        </Box>
    );
};
