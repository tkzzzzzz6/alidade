// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Which finished answer's dispatch is being replayed on the canvas.
 *
 * Deliberately not in the Redux store: the store is saved to the browser
 * database on every change, and a replay is a passing view, not workspace
 * state. Opening one is a single update here; the replay clock itself lives in
 * the panel (see `DispatchPanel`).
 */

import { useSyncExternalStore } from 'react';
import { AgentPlanStep } from '../components/ComponentType';

export interface ReplayTarget {
    /** the answer (text turn) the plan belongs to */
    turnId: string;
    plan: AgentPlanStep[];
}

let current: ReplayTarget | null = null;
const listeners = new Set<() => void>();

const emit = () => listeners.forEach(l => l());

export function openReplay(target: ReplayTarget): void {
    current = target;
    emit();
}

export function closeReplay(): void {
    if (current === null) return;
    current = null;
    emit();
}

export function getReplay(): ReplayTarget | null {
    return current;
}

function subscribe(listener: () => void): () => void {
    listeners.add(listener);
    return () => { listeners.delete(listener); };
}

export function useReplay(): ReplayTarget | null {
    return useSyncExternalStore(subscribe, getReplay, getReplay);
}
