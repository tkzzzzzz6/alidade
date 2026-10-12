import React from 'react';
import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

const { listDefinitions, deleteDefinition } = vi.hoisted(() => ({
    listDefinitions: vi.fn(),
    deleteDefinition: vi.fn(),
}));

vi.mock('../../../../src/api/knowledgeApi', () => ({ listDefinitions, deleteDefinition }));
vi.mock('react-i18next', () => ({
    useTranslation: () => ({
        t: (key: string, params?: Record<string, any>) => {
            if (key === 'knowledge.definitionTables') return `tables: ${params?.tables}`;
            if (key === 'knowledge.definitionUses') return `used ${params?.count}x`;
            return key;
        },
    }),
}));

import { SavedDefinitionsSection } from '../../../../src/views/SavedDefinitionsSection';

describe('SavedDefinitionsSection', () => {
    it('lists saved definitions and deletes one', async () => {
        listDefinitions.mockResolvedValue([
            { id: 'a1', term: '活跃客户', meaning: '过去 90 天下过单', tables: ['orders'], uses: 3 },
            { id: 'b2', term: 'big deal', meaning: 'contract ≥ 1M', tables: [], uses: 0 },
        ]);
        deleteDefinition.mockResolvedValue(undefined);
        render(<SavedDefinitionsSection />);
        await waitFor(() => expect(screen.getAllByTestId('saved-definition')).toHaveLength(2));
        expect(screen.getByText('tables: orders · used 3x')).toBeInTheDocument();
        expect(screen.getByText(/knowledge.definitionAllTables/)).toBeInTheDocument();
        fireEvent.click(screen.getAllByLabelText('knowledge.definitionDelete')[0]);
        await waitFor(() => expect(screen.getAllByTestId('saved-definition')).toHaveLength(1));
        expect(deleteDefinition).toHaveBeenCalledWith('a1');
    });

    it('says when nothing is saved', async () => {
        listDefinitions.mockResolvedValue([]);
        render(<SavedDefinitionsSection />);
        await waitFor(() => expect(screen.getByText('knowledge.definitionsEmpty')).toBeInTheDocument());
    });
});
