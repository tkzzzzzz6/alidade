// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

/**
 * Geometry of the dispatch diagram: main agent → dispatch → one column of
 * cards per round (a "merge, then dispatch again" joint between rounds) →
 * merge, and the dotted line back to the main agent.
 *
 * Fixed design units (the panel scales the whole diagram to its width).
 * Horizontal links always leave / enter a box at the middle of its left or
 * right side; the return line uses the middle of the bottom sides.
 */

export interface Rect { x: number; y: number; w: number; h: number }
export interface Wire { d: string; from: [number, number]; to: [number, number] }

export interface ColumnLayout {
    cards: Rect[];
    trunk: Wire;           // dispatch (or the joint before) → the split point
    toStep: Wire[];        // split point → card i
    fromStep: Wire[];      // card i → the joint after (or merge)
}

export interface DispatchLayout {
    width: number;
    height: number;
    main: Rect;
    dispatch: Rect;
    merge: Rect;
    columns: ColumnLayout[];
    /** between column k and k+1: merge that round, dispatch the next */
    joints: Rect[];
    /** every column's cards / wires, one after the other */
    cards: Rect[];
    toMain: Wire;          // main → dispatch
    trunk: Wire;           // the first column's trunk
    toStep: Wire[];
    fromStep: Wire[];
    back: Wire;            // merge → main (bottom sides)
    backLabelY: number;
}

export const LAYOUT = {
    width: 966,
    main: { x: 0, w: 210, minH: 250 },
    dispatch: { x: 266, w: 130, h: 48 },
    splitX: 422,
    card: { x: 456, w: 300, h: 76, gap: 10 },
    joinX: 788,
    merge: { x: 816, w: 150, h: 48 },
    joint: { w: 108, h: 48 },
    /** source node → split point, split → cards, cards → join, join → next node */
    gaps: { split: 26, cards: 34, join: 32, node: 60 },
    /** more than one round: everything narrower so two columns still fit a canvas */
    multi: {
        main: 160, mainMinH: 290, dispatch: { x: 196, w: 104 }, card: 200, merge: 120,
        gaps: { split: 22, cards: 28, join: 24, node: 44 },
    },
    backGap: 22,
} as const;

const R = 6;

function elbow(x0: number, y0: number, xTurn: number, y1: number, x1: number): string {
    const dy = y1 - y0;
    if (Math.abs(dy) < 0.5) return `M${x0},${y0} H${x1}`;
    const r = Math.min(R, Math.abs(dy) / 2);
    const s = dy > 0 ? 1 : -1;
    return `M${x0},${y0} H${xTurn - r} Q${xTurn},${y0} ${xTurn},${y0 + s * r} V${y1 - s * r} Q${xTurn},${y1} ${xTurn + r},${y1} H${x1}`;
}

/** `columns`: how many cards each round has (a plain number = one round). */
export function dispatchLayout(columns: number | number[]): DispatchLayout {
    const L = LAYOUT;
    const counts = (Array.isArray(columns) ? columns : [columns]).map(n => Math.max(1, n));
    if (counts.length === 0) counts.push(1);
    const multi = counts.length > 1;
    const cardW = multi ? L.multi.card : L.card.w;
    const gaps = multi ? L.multi.gaps : L.gaps;
    const mainW = multi ? L.multi.main : L.main.w;
    const disp = multi ? L.multi.dispatch : L.dispatch;
    const mergeW = multi ? L.multi.merge : L.merge.w;
    const colH = (n: number) => n * L.card.h + (n - 1) * L.card.gap;
    const mainH = Math.max(multi ? L.multi.mainMinH : L.main.minH, ...counts.map(colH));
    const total = mainH;
    const c = total / 2;

    const main: Rect = { x: L.main.x, y: (total - mainH) / 2, w: mainW, h: mainH };
    const dispatch: Rect = { x: disp.x, y: c - L.dispatch.h / 2, w: disp.w, h: L.dispatch.h };
    const mainRight = main.x + main.w;
    const toMain: Wire = { d: `M${mainRight},${c} H${dispatch.x}`, from: [mainRight, c], to: [dispatch.x, c] };

    const out: ColumnLayout[] = [];
    const joints: Rect[] = [];
    let source = dispatch.x + dispatch.w;   // right side of the node feeding this column
    let merge: Rect = { x: 0, y: 0, w: 0, h: 0 };
    counts.forEach((n, k) => {
        const splitX = source + gaps.split;
        const cardX = splitX + gaps.cards;
        const column = colH(n);
        const cards: Rect[] = Array.from({ length: n }, (_, i) => ({
            x: cardX, y: (total - column) / 2 + i * (L.card.h + L.card.gap), w: cardW, h: L.card.h,
        }));
        const right = cardX + cardW;
        const joinX = right + gaps.join;
        const last = k === counts.length - 1;
        const node: Rect = last
            ? { x: right + gaps.node, y: c - L.merge.h / 2, w: mergeW, h: L.merge.h }
            : { x: right + gaps.node, y: c - L.joint.h / 2, w: L.joint.w, h: L.joint.h };
        if (last) merge = node; else joints.push(node);

        const trunk: Wire = { d: `M${source},${c} H${splitX}`, from: [source, c], to: [splitX, c] };
        const toStep = cards.map(card => {
            const y = card.y + card.h / 2;
            // starts at the split point (the trunk carries the first stretch)
            const r = Math.min(R, Math.abs(y - c) / 2);
            const d = Math.abs(y - c) < 0.5 ? `M${splitX},${c} H${card.x}`
                : `M${splitX},${c} V${y - Math.sign(y - c) * r} Q${splitX},${y} ${splitX + r},${y} H${card.x}`;
            return { d, from: [splitX, c] as [number, number], to: [card.x, y] as [number, number] };
        });
        const fromStep = cards.map(card => {
            const y = card.y + card.h / 2;
            const cr = card.x + card.w;
            return { d: elbow(cr, y, joinX, c, node.x), from: [cr, y] as [number, number], to: [node.x, c] as [number, number] };
        });
        out.push({ cards, trunk, toStep, fromStep });
        source = node.x + node.w;
    });

    const backY = total + L.backGap;
    const mx = merge.x + merge.w / 2;
    const ax = main.x + main.w / 2;
    const mainBottom = main.y + main.h;
    const back: Wire = {
        d: `M${mx},${merge.y + merge.h} V${backY - R} Q${mx},${backY} ${mx - R},${backY} H${ax + R} Q${ax},${backY} ${ax},${backY - R} V${mainBottom + 4}`,
        from: [mx, merge.y + merge.h], to: [ax, mainBottom],
    };
    return {
        width: merge.x + merge.w, height: backY + 12, main, dispatch, merge, columns: out, joints,
        cards: out.flatMap(col => col.cards), toMain, trunk: out[0].trunk,
        toStep: out.flatMap(col => col.toStep), fromStep: out.flatMap(col => col.fromStep),
        back, backLabelY: backY,
    };
}

/** Middle of a rect's left / right / top / bottom side. */
export function sideMid(r: Rect, side: 'left' | 'right' | 'top' | 'bottom'): [number, number] {
    if (side === 'left') return [r.x, r.y + r.h / 2];
    if (side === 'right') return [r.x + r.w, r.y + r.h / 2];
    if (side === 'top') return [r.x + r.w / 2, r.y];
    return [r.x + r.w / 2, r.y + r.h];
}
