import { describe, expect, it } from 'vitest';
import {
  activateTab,
  allSessionIds,
  closeSession,
  dockToTab,
  emptyLayout,
  floatSession,
  fromSaved,
  leaves,
  minimizeFloat,
  openSession,
  placementOf,
  resizeSplit,
  restoreFloat,
  savedConnectionIds,
  splitWith,
  toSaved,
  visibleSessionIds,
} from './model';

const rect = { x: 10, y: 10, w: 400, h: 300 };

describe('workspace layout model', () => {
  it('opens the first session full-size and each further session in a new tab', () => {
    let l = openSession(emptyLayout(), 's1');
    expect(l.tabs).toHaveLength(1);
    expect(l.tabs[0].root).toEqual({ kind: 'leaf', sessionId: 's1' });
    l = openSession(l, 's2');
    expect(l.tabs).toHaveLength(2);
    expect(l.activeTabId).toBe(l.tabs[1].id);
    expect(visibleSessionIds(l)).toEqual(new Set(['s2']));
    l = activateTab(l, l.tabs[0].id);
    expect(visibleSessionIds(l)).toEqual(new Set(['s1']));
    expect(allSessionIds(l).sort()).toEqual(['s1', 's2']); // switching tabs keeps sessions
  });

  it('floats a tab and docks it back', () => {
    let l = openSession(openSession(emptyLayout(), 's1'), 's2');
    l = floatSession(l, 's2', rect);
    expect(l.tabs).toHaveLength(1);
    expect(placementOf(l, 's2')).toMatchObject({ where: 'float', minimized: false });
    expect(visibleSessionIds(l)).toEqual(new Set(['s1', 's2']));
    const fid = l.floats[0].id;
    l = minimizeFloat(l, fid);
    expect(visibleSessionIds(l)).toEqual(new Set(['s1']));
    l = restoreFloat(l, fid);
    expect(l.floats[0].minimized).toBe(false);
    l = dockToTab(l, 's2');
    expect(l.floats).toHaveLength(0);
    expect(l.tabs).toHaveLength(2);
  });

  it('builds split views and collapses them when sessions leave', () => {
    let l = openSession(openSession(openSession(emptyLayout(), 's1'), 's2'), 's3');
    l = splitWith(l, 's1', 's2', 'right');
    expect(l.tabs).toHaveLength(2);
    const tab = l.tabs.find((t) => leaves(t.root).includes('s1'))!;
    expect(tab.root).toMatchObject({ kind: 'split', dir: 'row', sizes: [0.5, 0.5] });
    expect(leaves(tab.root)).toEqual(['s1', 's2']);
    l = splitWith(l, 's2', 's3', 'bottom');
    const t2 = l.tabs.find((t) => leaves(t.root).includes('s1'))!;
    expect(leaves(t2.root)).toEqual(['s1', 's2', 's3']);
    expect(l.tabs).toHaveLength(1);
    // resize the outer split
    if (t2.root.kind !== 'split') throw new Error('expected split');
    l = resizeSplit(l, t2.root.id, [3, 1]);
    const resized = l.tabs[0].root;
    expect(resized.kind === 'split' && resized.sizes).toEqual([0.75, 0.25]);
    // pull one out into its own tab, then close another: the split collapses to a single leaf
    l = dockToTab(l, 's3');
    expect(l.tabs).toHaveLength(2);
    l = closeSession(l, 's2');
    expect(l.tabs[0].root).toEqual({ kind: 'leaf', sessionId: 's1' });
  });

  it('flattens same-direction splits', () => {
    let l = openSession(openSession(openSession(emptyLayout(), 'a'), 'b'), 'c');
    l = splitWith(l, 'a', 'b', 'right');
    l = splitWith(l, 'b', 'c', 'right');
    const root = l.tabs[0].root;
    expect(root.kind).toBe('split');
    if (root.kind === 'split') {
      expect(root.children.every((c) => c.kind === 'leaf')).toBe(true);
      expect(root.sizes.reduce((x, y) => x + y, 0)).toBeCloseTo(1);
    }
  });

  it('closing the active tab activates a neighbour', () => {
    let l = openSession(openSession(openSession(emptyLayout(), 's1'), 's2'), 's3');
    l = activateTab(l, l.tabs[1].id);
    l = closeSession(l, 's2');
    expect(l.tabs.map((t) => leaves(t.root)[0])).toEqual(['s1', 's3']);
    expect(leaves(l.tabs.find((t) => t.id === l.activeTabId)!.root)).toEqual(['s3']);
    l = closeSession(closeSession(l, 's3'), 's1');
    expect(l.activeTabId).toBeNull();
  });

  it('saves and restores layouts by connection id', () => {
    let l = openSession(openSession(openSession(emptyLayout(), 's1'), 's2'), 's3');
    l = splitWith(l, 's1', 's2', 'right');
    l = floatSession(l, 's3', rect);
    const conn: Record<string, string> = { s1: 'c1', s2: 'c2', s3: 'c3' };
    const saved = toSaved(l, (s) => conn[s])!;
    expect(savedConnectionIds(saved)).toEqual(['c1', 'c2', 'c3']);
    const reopened: Record<string, string> = { c1: 'n1', c2: 'n2', c3: 'n3' };
    const restored = fromSaved(JSON.parse(JSON.stringify(saved)), (c) => reopened[c]);
    expect(leaves(restored.tabs[0].root)).toEqual(['n1', 'n2']);
    expect(restored.floats[0]).toMatchObject({ sessionId: 'n3', w: 400, h: 300 });
    // sessions that could not be reopened are skipped
    const partial = fromSaved(saved, (c) => (c === 'c2' ? undefined : reopened[c]));
    expect(partial.tabs[0].root).toEqual({ kind: 'leaf', sessionId: 'n1' });
  });

  it('moves the focus on when the focused session is closed', () => {
    // split neighbour first
    let l = openSession(emptyLayout(), 's1');
    l = openSession(l, 's2');
    l = splitWith(l, 's1', 's3', 'right');
    l = { ...l, focusedSessionId: 's3' };
    expect(closeSession(l, 's3').focusedSessionId).toBe('s1');
    // a closed tab hands the focus to the tab shown next
    l = { ...openSession(openSession(emptyLayout(), 'a'), 'b') };
    expect(l.focusedSessionId).toBe('b');
    const afterTab = closeSession(l, 'b');
    expect(afterTab.focusedSessionId).toBe(leaves(afterTab.tabs.find((t) => t.id === afterTab.activeTabId)!.root)[0]);
    // a closed floating window hands the focus to the window below it
    l = openSession(emptyLayout(), 'tab');
    l = floatSession(openSession(l, 'f1'), 'f1', rect);
    l = floatSession(openSession(l, 'f2'), 'f2', rect);
    expect(l.focusedSessionId).toBe('f2');
    expect(closeSession(l, 'f2').focusedSessionId).toBe('f1');
    // closing a session that is not focused leaves the focus alone
    expect(closeSession(l, 'tab').focusedSessionId).toBe('f2');
  });
});
