// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * RunTraceDialog — what each analyst run did, as a timeline.
 *
 * Lists the user's recent runs (newest first) and, for the selected one, shows
 * totals (time, model calls by model and routing tier, tokens and cache hits,
 * tools, charts, chart-check retries, sub-agents, questions) and a waterfall
 * of every model call, tool call and chart action, with sub-agent steps in
 * their own lanes. Click a row to see the recorded event. Data comes from
 * `/api/traces` (recorded by py-src/alidade/analyst/engine/trace.py).
 */

import React, { FC, useCallback, useEffect, useMemo, useState } from 'react';
import {
    Box, Chip, CircularProgress, Dialog, DialogContent, DialogTitle, IconButton,
    List, ListItemButton, Tooltip, Typography, useTheme,
} from '@mui/material';
import { alpha } from '@mui/material/styles';
import TimelineOutlinedIcon from '@mui/icons-material/TimelineOutlined';
import RefreshIcon from '@mui/icons-material/Refresh';
import CloseIcon from '@mui/icons-material/Close';
import { useTranslation } from 'react-i18next';

import { getUrls } from '../app/utils';
import { apiRequest } from '../app/apiClient';
import { textVar } from '../app/layout';
import {
    buildSpans, cacheShare, DEFAULT_LABELS, formatMs, formatTokens, lanesOf, Labeler, MAIN_LANE,
    RunTraceData, SpanTone, TraceListItem, TraceSegment, TraceSpan,
} from './traceTimeline';

function useToneColor(): (tone: SpanTone) => string {
    const theme = useTheme();
    return (tone: SpanTone) => ({
        strong: theme.palette.primary.main,
        fast: theme.palette.success.main,
        main: theme.palette.primary.light,
        tool: theme.palette.grey[500],
        chart: theme.palette.secondary.main,
        error: theme.palette.error.main,
        ask: theme.palette.warning.main,
        info: theme.palette.text.disabled,
        done: theme.palette.success.dark,
    } as Record<SpanTone, string>)[tone];
}

const Stat: FC<{ label: string; value: React.ReactNode; hint?: string }> = ({ label, value, hint }) => (
    <Box sx={{ minWidth: 92 }}>
        <Typography sx={{ fontSize: textVar.xxs, color: 'text.secondary' }}>{label}</Typography>
        <Typography sx={{ fontSize: textVar.md, fontWeight: 600, fontVariantNumeric: 'tabular-nums' }}>{value}</Typography>
        {hint && <Typography sx={{ fontSize: textVar.xxs, color: 'text.disabled' }}>{hint}</Typography>}
    </Box>
);

const Waterfall: FC<{ segment: TraceSegment; tr: Labeler; laneName: (lane: string) => string }> = ({ segment, tr, laneName }) => {
    const color = useToneColor();
    const total = Math.max(1, segment.duration_ms || Math.max(1, ...segment.events.map(e => e.t_ms)));
    const spans = useMemo(() => buildSpans(segment.events, total, tr), [segment, total, tr]);
    const lanes = lanesOf(spans);
    const [open, setOpen] = useState<number | null>(null);

    return (
        <Box sx={{ fontSize: textVar.xs }}>
            {spans.map((s: TraceSpan, i: number) => {
                const left = (s.start / total) * 100;
                const width = Math.max(0.4, ((s.end - s.start) / total) * 100);
                const laneIdx = lanes.indexOf(s.lane);
                return (
                    <Box key={i}>
                        <Box
                            role="button"
                            data-testid="trace-row"
                            onClick={() => setOpen(open === i ? null : i)}
                            sx={{
                                display: 'grid', gridTemplateColumns: '58px 64px minmax(0, 1fr) 38%', gap: 1,
                                alignItems: 'center', px: 1, py: '3px', cursor: 'pointer', borderRadius: 1,
                                '&:hover': { bgcolor: 'action.hover' },
                                bgcolor: open === i ? 'action.selected' : undefined,
                            }}
                        >
                            <Typography sx={{ fontSize: textVar.xxs, color: 'text.disabled', fontVariantNumeric: 'tabular-nums' }}>
                                {formatMs(s.start)}
                            </Typography>
                            <Typography noWrap sx={{ fontSize: textVar.xxs, color: laneIdx === 0 ? 'text.secondary' : 'secondary.main' }}>
                                {laneName(s.lane)}
                            </Typography>
                            <Typography noWrap title={s.label} sx={{ fontSize: textVar.xs, color: s.tone === 'error' ? 'error.main' : 'text.primary' }}>
                                {s.label}
                            </Typography>
                            <Box sx={{ position: 'relative', height: 10, bgcolor: (t) => alpha(t.palette.text.primary, 0.04), borderRadius: 0.5 }}>
                                {s.kind === 'mark' ? (
                                    <Box sx={{
                                        position: 'absolute', left: `calc(${left}% - 4px)`, top: 1, width: 8, height: 8,
                                        borderRadius: '50%', bgcolor: color(s.tone),
                                    }} />
                                ) : (
                                    <Tooltip title={formatMs(s.end - s.start)}>
                                        <Box sx={{
                                            position: 'absolute', left: `${left}%`, width: `${width}%`, minWidth: 2, top: 1, height: 8,
                                            borderRadius: 0.5, bgcolor: color(s.tone),
                                        }} />
                                    </Tooltip>
                                )}
                            </Box>
                        </Box>
                        {open === i && (
                            <Box component="pre" sx={{
                                m: 0, mx: 1, mb: 0.5, p: 1, maxHeight: 280, overflow: 'auto', fontSize: textVar.xxs,
                                bgcolor: (t) => alpha(t.palette.text.primary, 0.04), borderRadius: 1, whiteSpace: 'pre-wrap',
                                wordBreak: 'break-word',
                            }}>
                                {JSON.stringify(s.events, null, 2)}
                            </Box>
                        )}
                    </Box>
                );
            })}
        </Box>
    );
};

export const RunTraceDialog: FC<{ hideTrigger?: boolean; open?: boolean; onOpenChange?: (open: boolean) => void }> = ({
    hideTrigger = false, open: openProp, onOpenChange,
}) => {
    const { t } = useTranslation();
    const color = useToneColor();
    const [openState, setOpenState] = useState(false);
    const open = openProp ?? openState;
    const setOpen = useCallback((v: boolean) => { setOpenState(v); onOpenChange?.(v); }, [onOpenChange]);
    const [items, setItems] = useState<TraceListItem[]>([]);
    const [selected, setSelected] = useState<string | null>(null);
    const [detail, setDetail] = useState<RunTraceData | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const tr: Labeler = useCallback((key, params = {}) =>
        t(`trace.labels.${key}`, { ...params, defaultValue: DEFAULT_LABELS[key] ?? key }), [t]);
    const laneName = useCallback((lane: string) =>
        lane === MAIN_LANE ? t('trace.mainLane') : t('trace.stepLane', { step: lane.slice('step:'.length) }), [t]);

    const loadList = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            const { data } = await apiRequest<{ traces: TraceListItem[] }>(`${getUrls().TRACES}?limit=50`);
            setItems(data.traces || []);
            setSelected(prev => prev ?? data.traces?.[0]?.run_id ?? null);
        } catch (e: any) {
            setError(e?.message || String(e));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { if (open) loadList(); }, [open, loadList]);

    useEffect(() => {
        if (!open || !selected) { setDetail(null); return; }
        let cancelled = false;
        apiRequest<RunTraceData>(`${getUrls().TRACES}/${encodeURIComponent(selected)}`)
            .then(({ data }) => { if (!cancelled) setDetail(data); })
            .catch((e: any) => { if (!cancelled) setError(e?.message || String(e)); });
        return () => { cancelled = true; };
    }, [open, selected]);

    const s = detail?.summary;
    const share = s ? cacheShare(s) : null;
    const tierText = s ? Object.entries(s.tiers || {}).map(([k, v]) => `${t(`trace.tier.${k}`, { defaultValue: k })} ${v}`).join(' · ') : '';

    return (
        <>
            {!hideTrigger && (
                <Tooltip title={t('trace.open')}>
                    <IconButton
                        size="small"
                        aria-label={t('trace.open')}
                        onClick={() => setOpen(true)}
                        sx={{ p: 0.5, color: 'text.secondary', '&:hover': { color: 'text.primary' } }}
                    >
                        <TimelineOutlinedIcon fontSize="small" />
                    </IconButton>
                </Tooltip>
            )}
            <Dialog open={open} onClose={() => setOpen(false)} maxWidth="xl" fullWidth>
                <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 0.5, px: 2, py: 1.25 }}>
                    <Typography component="span" sx={{ fontSize: textVar.xl, fontWeight: 500, flexGrow: 1 }}>
                        {t('trace.title')}
                    </Typography>
                    <Tooltip title={t('trace.refresh')}>
                        <span>
                            <IconButton size="small" onClick={loadList} disabled={loading} aria-label={t('trace.refresh')}>
                                <RefreshIcon fontSize="small" />
                            </IconButton>
                        </span>
                    </Tooltip>
                    <IconButton size="small" onClick={() => setOpen(false)} aria-label={t('trace.close')}>
                        <CloseIcon fontSize="small" />
                    </IconButton>
                </DialogTitle>
                <DialogContent dividers sx={{ p: 0, display: 'flex', height: '75vh' }}>
                    <Box sx={{ width: 300, flexShrink: 0, borderRight: 1, borderColor: 'divider', overflow: 'auto' }}>
                        {loading && items.length === 0 && <Box sx={{ p: 2 }}><CircularProgress size={18} /></Box>}
                        {!loading && items.length === 0 && (
                            <Typography sx={{ p: 2, fontSize: textVar.sm, color: 'text.secondary' }}>{t('trace.empty')}</Typography>
                        )}
                        <List dense disablePadding>
                            {items.map(item => (
                                <ListItemButton
                                    key={item.run_id}
                                    data-testid="trace-item"
                                    selected={item.run_id === selected}
                                    onClick={() => setSelected(item.run_id)}
                                    sx={{ display: 'block', py: 1, borderBottom: 1, borderColor: 'divider' }}
                                >
                                    <Typography sx={{
                                        fontSize: textVar.sm, display: '-webkit-box', WebkitLineClamp: 2,
                                        WebkitBoxOrient: 'vertical', overflow: 'hidden',
                                    }}>
                                        {item.question || item.run_id}
                                    </Typography>
                                    <Typography sx={{ fontSize: textVar.xxs, color: 'text.secondary', mt: 0.25 }}>
                                        {(item.started_at || '').replace('T', ' ')} · {formatMs(item.summary?.duration_ms)} ·{' '}
                                        {t('trace.callsShort', { n: item.summary?.llm_calls ?? 0 })} ·{' '}
                                        {formatTokens((item.summary?.prompt_tokens ?? 0) + (item.summary?.completion_tokens ?? 0))}
                                        {item.summary?.status && item.summary.status !== 'success' && (
                                            <Box component="span" sx={{ color: 'warning.main' }}> · {t(`trace.status.${item.summary.status}`, { defaultValue: item.summary.status })}</Box>
                                        )}
                                    </Typography>
                                </ListItemButton>
                            ))}
                        </List>
                    </Box>
                    <Box sx={{ flex: 1, minWidth: 0, overflow: 'auto', p: 2 }}>
                        {error && <Typography sx={{ color: 'error.main', fontSize: textVar.sm, mb: 1 }}>{t('trace.loadError', { message: error })}</Typography>}
                        {detail && s && (
                            <>
                                <Typography sx={{ fontSize: textVar.md, fontWeight: 600, mb: 1 }}>{detail.question}</Typography>
                                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 2, mb: 1.5 }}>
                                    <Stat label={t('trace.duration')} value={formatMs(s.duration_ms)}
                                          hint={t('trace.llmTime', { time: formatMs(s.llm_ms) })} />
                                    <Stat label={t('trace.llmCalls')} value={s.llm_calls} hint={tierText} />
                                    <Stat label={t('trace.tokensIn')} value={formatTokens(s.prompt_tokens)}
                                          hint={share == null ? undefined : t('trace.cacheHit', { pct: Math.round(share * 100) })} />
                                    <Stat label={t('trace.tokensOut')} value={formatTokens(s.completion_tokens)} />
                                    <Stat label={t('trace.tools')} value={s.tool_calls} />
                                    <Stat label={t('trace.charts')} value={s.charts}
                                          hint={s.chart_checks ? t('trace.chartChecks', { n: s.chart_checks }) : undefined} />
                                    {s.subagents > 0 && <Stat label={t('trace.subagents')} value={s.subagents} />}
                                    {s.asks > 0 && <Stat label={t('trace.asks')} value={s.asks} />}
                                </Box>
                                {Object.keys(s.by_model || {}).length > 0 && (
                                    <Box component="table" sx={{
                                        borderCollapse: 'collapse', mb: 2, fontSize: textVar.xs, fontFamily: 'inherit',
                                        '& th': { fontWeight: 500, color: 'text.secondary' },
                                        '& td, & th': { borderBottom: 1, borderColor: 'divider', px: 1, py: 0.25, textAlign: 'right',
                                                         fontFamily: (t: any) => t.typography.fontFamily },
                                        '& td:first-of-type, & th:first-of-type': { textAlign: 'left' },
                                    }}>
                                        <thead><tr>
                                            <th>{t('trace.model')}</th><th>{t('trace.calls')}</th><th>{t('trace.tokensIn')}</th>
                                            <th>{t('trace.cached')}</th><th>{t('trace.tokensOut')}</th>
                                        </tr></thead>
                                        <tbody>
                                            {Object.entries(s.by_model).map(([m, u]) => (
                                                <tr key={m}>
                                                    <td>{m}</td><td>{u.calls}</td><td>{formatTokens(u.prompt)}</td>
                                                    <td>{formatTokens(u.cached)}</td><td>{formatTokens(u.completion)}</td>
                                                </tr>
                                            ))}
                                        </tbody>
                                    </Box>
                                )}
                                <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mb: 1.5 }}>
                                    {(['strong', 'fast', 'main', 'tool', 'chart', 'error', 'ask'] as SpanTone[]).map(tone => (
                                        <Chip key={tone} size="small" variant="outlined" label={t(`trace.legend.${tone}`)}
                                              sx={{ fontSize: textVar.xxs, height: 20, '& .MuiChip-label': { px: 0.75 } }}
                                              icon={<Box sx={{ width: 8, height: 8, borderRadius: '2px', bgcolor: color(tone), ml: '6px !important' }} />} />
                                    ))}
                                </Box>
                                {detail.segments.map((seg, i) => (
                                    <Box key={i} sx={{ mb: 2 }}>
                                        <Typography sx={{ fontSize: textVar.sm, fontWeight: 600, mb: 0.5 }}>
                                            {seg.kind === 'resume'
                                                ? t('trace.segmentResume', { n: i + 1, input: seg.input })
                                                : t('trace.segmentStart', { n: i + 1 })}
                                            <Box component="span" sx={{ fontWeight: 400, color: 'text.secondary', ml: 1, fontSize: textVar.xs }}>
                                                {formatMs(seg.duration_ms)} · {t(`trace.status.${seg.status}`, { defaultValue: seg.status || '' })}
                                            </Box>
                                        </Typography>
                                        <Waterfall segment={seg} tr={tr} laneName={laneName} />
                                    </Box>
                                ))}
                            </>
                        )}
                    </Box>
                </DialogContent>
            </Dialog>
        </>
    );
};

export default RunTraceDialog;
