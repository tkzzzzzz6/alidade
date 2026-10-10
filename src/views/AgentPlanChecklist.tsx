// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * The analyst's plan as a compact checklist (plan-then-execute).
 *
 * Rendered live above the thinking steps while a run is in progress, and on
 * the run's closing / question turn so the thread keeps a record of what the
 * agent set out to do and what it finished.
 */

import React from 'react';
import { useTranslation } from 'react-i18next';
import { Box, Typography, useTheme } from '@mui/material';
import { alpha } from '@mui/material/styles';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import RemoveCircleOutlineIcon from '@mui/icons-material/RemoveCircleOutline';
import AutorenewIcon from '@mui/icons-material/Autorenew';
import { AgentPlanStep } from '../components/ComponentType';
import { textVar } from '../app/layout';

export function planProgress(steps: AgentPlanStep[]): { done: number; total: number } {
    const counted = steps.filter(s => s.status !== 'skipped');
    return { done: counted.filter(s => s.status === 'done').length, total: counted.length };
}

const StepIcon: React.FC<{ status: AgentPlanStep['status']; active: boolean }> = ({ status, active }) => {
    const theme = useTheme();
    const sx = { width: 12, height: 12, flexShrink: 0, mt: '2px' };
    if (status === 'done') return <CheckCircleIcon sx={{ ...sx, color: theme.palette.success.main }} />;
    if (status === 'skipped') return <RemoveCircleOutlineIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
    if (status === 'in_progress') {
        return (
            <AutorenewIcon sx={{
                ...sx,
                color: theme.palette.primary.main,
                ...(active ? {
                    animation: 'df-plan-spin 1.6s linear infinite',
                    '@keyframes df-plan-spin': { from: { transform: 'rotate(0deg)' }, to: { transform: 'rotate(360deg)' } },
                } : {}),
            }} />
        );
    }
    return <RadioButtonUncheckedIcon sx={{ ...sx, color: theme.palette.text.disabled }} />;
};

export const AgentPlanChecklist: React.FC<{
    steps: AgentPlanStep[];
    /** True while the run is still working (animates the current step). */
    active?: boolean;
    /** One-line "Plan 2/4" summary instead of the full list. */
    compact?: boolean;
}> = ({ steps, active = false, compact = false }) => {
    const { t } = useTranslation();
    const theme = useTheme();
    if (!steps || steps.length === 0) return null;
    const { done, total } = planProgress(steps);

    if (compact) {
        return (
            <Typography
                data-testid="agent-plan-compact"
                sx={{ fontSize: textVar.xs, color: 'text.secondary', lineHeight: 1.4 }}
            >
                {t('dataThread.planProgress', { done, total })}
            </Typography>
        );
    }

    return (
        <Box
            data-testid="agent-plan"
            sx={{
                border: `1px solid ${alpha(theme.palette.primary.main, 0.18)}`,
                bgcolor: alpha(theme.palette.primary.main, 0.03),
                borderRadius: '6px',
                px: 1, py: 0.75,
            }}
        >
            <Typography sx={{
                fontSize: textVar.xs, fontWeight: 600, color: 'text.secondary',
                mb: 0.5, display: 'flex', justifyContent: 'space-between',
            }}>
                <span>{t('dataThread.planTitle')}</span>
                <span>{t('dataThread.planProgress', { done, total })}</span>
            </Typography>
            <Box component="ol" sx={{ listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column', gap: '3px' }}>
                {steps.map(step => (
                    <Box
                        component="li"
                        key={step.id}
                        data-status={step.status}
                        sx={{ display: 'flex', alignItems: 'flex-start', gap: 0.75 }}
                    >
                        <StepIcon status={step.status} active={active} />
                        <Typography sx={{
                            fontSize: textVar.xs,
                            lineHeight: 1.45,
                            wordBreak: 'break-word',
                            color: step.status === 'in_progress' ? 'text.primary' : 'text.secondary',
                            fontWeight: step.status === 'in_progress' ? 500 : 400,
                            textDecoration: step.status === 'skipped' ? 'line-through' : 'none',
                        }}>
                            {step.title}
                        </Typography>
                    </Box>
                ))}
            </Box>
        </Box>
    );
};
