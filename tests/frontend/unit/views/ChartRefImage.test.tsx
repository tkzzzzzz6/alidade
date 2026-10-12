import React from 'react';
import '@testing-library/jest-dom/vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { Provider } from 'react-redux';
import { configureStore } from '@reduxjs/toolkit';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { clearCache, setCachedChart } from '../../../../src/app/chartCache';
import { ChartRefImage, chartRefId } from '../../../../src/views/ChartRefImage';
import { CompactMarkdown } from '../../../../src/views/InteractionEntryCard';

function storeWith(chartIds: string[], seen: any[] = []) {
  return configureStore({
    reducer: (state: any = { charts: chartIds.map(id => ({ id })) }, action: any) => { seen.push(action); return state; },
  });
}

const entry = (url: string) => ({ svg: '', thumbnailDataUrl: url, fullPngDataUrl: url, specKey: 'k', naturalWidth: 10, naturalHeight: 10 });

describe('chart references in agent prose', () => {
  afterEach(() => { clearCache(); vi.useRealTimers(); });

  it('parses chart:// urls only', () => {
    expect(chartRefId('chart://abc')).toBe('abc');
    expect(chartRefId('chart://')).toBeNull();
    expect(chartRefId('https://x/y.png')).toBeNull();
    expect(chartRefId(undefined)).toBeNull();
  });

  it('shows the rendered chart, and clicking it focuses the chart', () => {
    setCachedChart('c1', entry('data:image/png;base64,AAA'));
    const seen: any[] = [];
    render(<Provider store={storeWith(['c1'], seen)}><ChartRefImage chartId="c1" caption="Gross by genre" /></Provider>);
    const img = screen.getByRole('img', { name: 'Gross by genre' });
    expect(img).toHaveAttribute('src', 'data:image/png;base64,AAA');
    fireEvent.click(screen.getByTestId('chart-ref-image'));
    expect(seen.some(a => a.type?.endsWith('setFocused') && a.payload?.chartId === 'c1')).toBe(true);
  });

  it('waits for a chart that is still rendering, with a chip meanwhile', () => {
    vi.useFakeTimers();
    render(<Provider store={storeWith(['c2'])}><ChartRefImage chartId="c2" caption="Films per year" /></Provider>);
    expect(screen.getByTestId('chart-ref-chip')).toHaveTextContent('📊 Films per year');
    setCachedChart('c2', entry('data:image/png;base64,BBB'));
    act(() => { vi.advanceTimersByTime(600); });
    expect(screen.getByRole('img', { name: 'Films per year' })).toBeInTheDocument();
  });

  it('a chart that no longer exists is a chip, not a broken image', () => {
    render(<Provider store={storeWith([])}><ChartRefImage chartId="gone" caption="Old chart" /></Provider>);
    expect(screen.queryByRole('img')).toBeNull();
    expect(screen.getByTestId('chart-ref-chip')).toHaveTextContent('Old chart');
  });

  it('agent markdown keeps chart:// and still sanitises other urls', () => {
    setCachedChart('c3', entry('data:image/png;base64,CCC'));
    render(
      <Provider store={storeWith(['c3'])}>
        <CompactMarkdown color="#000" content={'Result:\n\n![IMDB by rating](chart://c3)\n\n[bad](javascript:alert(1))'} />
      </Provider>,
    );
    expect(screen.getByRole('img', { name: 'IMDB by rating' })).toHaveAttribute('src', 'data:image/png;base64,CCC');
    expect(screen.getByText('bad').getAttribute('href') || '').not.toContain('javascript');
  });
});
