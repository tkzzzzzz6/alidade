// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * A chart the agent referred to in its prose as `![caption](chart://<id>)`.
 *
 * The id is the chart's own id in the thread (the frontend adopts the id the
 * backend forwards with each chart), so the picture is the chart's rendered
 * image from the render cache. Charts render asynchronously, so the cache is
 * checked again for a few seconds; until then — or if the chart is gone — a
 * small "📊 caption" chip is shown instead of a broken image. Clicking it
 * focuses the chart in the thread.
 */

import React, { useEffect, useState } from 'react';
import { useDispatch, useSelector } from 'react-redux';
import { Box, ButtonBase, Typography } from '@mui/material';
import { getCachedChart } from '../app/chartCache';
import { AlidadeState, dfActions } from '../app/dfSlice';

export const CHART_REF_PREFIX = 'chart://';
const RETRY_MS = 500;
const MAX_TRIES = 20;

/** The chart id of a `chart://<id>` URL, or null for any other URL. */
export function chartRefId(src: string | undefined | null): string | null {
    if (!src || !src.startsWith(CHART_REF_PREFIX)) return null;
    const id = src.slice(CHART_REF_PREFIX.length).trim();
    return id || null;
}

function cachedImage(id: string): string | undefined {
    const entry = getCachedChart(id);
    return entry?.fullPngDataUrl || entry?.thumbnailDataUrl || undefined;
}

export const ChartRefImage: React.FC<{ chartId: string; caption?: string }> = ({ chartId, caption }) => {
    const dispatch = useDispatch();
    const exists = useSelector((state: AlidadeState) => state.charts.some(c => c.id === chartId));
    const [url, setUrl] = useState<string | undefined>(() => cachedImage(chartId));

    useEffect(() => {
        if (url || !exists) return;
        let tries = 0;
        const timer = setInterval(() => {
            const found = cachedImage(chartId);
            tries += 1;
            if (found) setUrl(found);
            if (found || tries >= MAX_TRIES) clearInterval(timer);
        }, RETRY_MS);
        return () => clearInterval(timer);
    }, [chartId, url, exists]);

    const label = caption || chartId;
    const focus = exists ? () => dispatch(dfActions.setFocused({ type: 'chart', chartId })) : undefined;

    if (url && exists) {
        return (
            <ButtonBase
                onClick={focus}
                aria-label={label}
                data-testid="chart-ref-image"
                sx={{ display: 'block', my: 0.5, borderRadius: '4px', overflow: 'hidden', textAlign: 'left' }}
            >
                <Box component="img" src={url} alt={label}
                     sx={{ display: 'block', maxWidth: '100%', maxHeight: 220, objectFit: 'contain' }} />
            </ButtonBase>
        );
    }
    return (
        <ButtonBase
            onClick={focus}
            disabled={!focus}
            data-testid="chart-ref-chip"
            sx={{
                display: 'inline-flex', alignItems: 'center', gap: 0.5, my: 0.25, px: 0.75, py: '1px',
                borderRadius: '4px', bgcolor: 'rgba(0,0,0,0.04)', verticalAlign: 'middle',
            }}
        >
            <Typography component="span" sx={{ fontSize: 'inherit', color: 'text.secondary' }}>📊 {label}</Typography>
        </ButtonBase>
    );
};
