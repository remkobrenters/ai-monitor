import assert from 'node:assert/strict';
import test from 'node:test';
import {AlertTracker, SoundReminders, attention, budget, cacheShare, COLORS, counts, tokenTotal, sessionStatus} from '../extension/logic.js';

test('subagent activity is work, while approvals retain their input-needed state', () => {
    const row = {tool: 'codex', state: 'working', subagentCount: 3, attention: false};
    assert.equal(sessionStatus(row), 'Working · 3 subagents');
    assert.equal(sessionStatus({...row, waitingForSubagents: true}), 'Waiting for 3 subagents to finish');
    assert.equal(sessionStatus({...row, subagentCount: 1, waitingForSubagents: true}), 'Waiting for 1 subagent to finish');
    assert.equal(sessionStatus({...row, state: 'approval', waitingForSubagents: true}), 'Needs approval');
    assert.equal(counts([row], 'codex').waiting, 0);
    assert.equal(counts([row], 'codex').working, 1);
});

test('quota uses the most consumed window and marks stale data', () => {
    const limits = {fetchedAt: 1000, windows: [{usedPercent: 20}, {usedPercent: 91}]};
    assert.equal(budget(limits, 1100).percent, 91);
    assert.equal(budget(limits, 1100).color, COLORS.warning);
    assert.equal(budget(limits, 1100).stale, false);
    assert.equal(budget(limits, 2500).stale, true);
    assert.equal(budget({...limits, error: 'offline'}, 1100).stale, true);
    assert.equal(budget({windows: [{usedPercent: NaN}]}).percent, null);
    assert.equal(budget({...limits, windows: [{usedPercent: 20, resetsAt: 1050}]}, 1100).stale, true);
});

test('circle color follows whichever allowance window prevents working', () => {
    const limits = windows => ({fetchedAt: 1000, windows});
    assert.equal(budget(limits([{usedPercent: 20}, {usedPercent: 65}]), 1100).color, COLORS.normal);
    assert.equal(budget(limits([{usedPercent: 98}, {usedPercent: 20}]), 1100).color, COLORS.warning);
    assert.equal(budget(limits([{usedPercent: 100}, {usedPercent: 20}]), 1100).color, COLORS.danger);
    assert.equal(budget(limits([{usedPercent: 20}, {usedPercent: 100}]), 1100).color, COLORS.danger);
    assert.equal(budget({...limits([{usedPercent: 99}]), limitReached: true}, 1100).color, COLORS.danger);
});

test('cached input is counted once and cache share is weighted by input', () => {
    const values = [{input: 10, cacheRead: 90, cacheWrite: 0, output: 5},
        {input: 900, cacheRead: 0, cacheWrite: 0, output: 10}];
    assert.equal(tokenTotal(values[0]), 105);
    assert.equal(cacheShare(values), 9);
    assert.equal(cacheShare([]), null);
});

test('alerts fire once per transition and never replay after lock', () => {
    const tracker = new AlertTracker();
    let row = {id: 'a', tool: 'claude', state: 'working', attention: false, revision: 1};
    assert.deepEqual(tracker.update([row]), []);
    row = {...row, state: 'approval', attention: true, revision: 2};
    assert.equal(tracker.update([row]).length, 1);
    assert.equal(tracker.update([row]).length, 0);
    row = {...row, state: 'ready', revision: 3};
    assert.equal(tracker.update([row], {locked: true}).length, 0);
    assert.equal(tracker.update([row]).length, 0);
});

test('working count excludes waiting and acknowledged idle sessions', () => {
    const rows = [{tool: 'claude', state: 'working'}, {tool: 'claude', state: 'ready', attention: true},
        {tool: 'claude', state: 'approval', attention: false}, {tool: 'codex', state: 'working'}];
    assert.deepEqual(counts(rows, 'claude'), {working: 1, waiting: 1, total: 3});
    assert.equal(counts(rows, 'claude', false).waiting, 0);
    assert.equal(attention(rows[1], false), false);
});

function reminders() {
    const time = {seconds: 0};
    const sound = new SoundReminders(() => time.seconds);
    const row = {id: 'a', state: 'approval', attention: true, revision: 1};
    sound.update([]);
    return {time, sound, row};
}

test('Once plays on a new wait, without periodic reminders or startup replay', () => {
    const {time, sound, row} = reminders();
    assert.equal(sound.update([row]), true);
    time.seconds = 10000;
    assert.equal(sound.update([row]), false);
    assert.equal(new SoundReminders().update([row]), false);
});

for (const interval of [60, 300]) {
    test(`waiting sound repeats every ${interval} seconds and stops after acknowledgement, work or exit`, () => {
        const {time, sound, row} = reminders();
        assert.equal(sound.update([row], {interval}), true);
        time.seconds = interval - 1;
        assert.equal(sound.update([row], {interval}), false);
        time.seconds = interval;
        assert.equal(sound.update([row], {interval}), true);
        assert.equal(sound.update([row], {interval}), false);
        sound.update([{...row, attention: false}], {interval});
        time.seconds += interval;
        assert.equal(sound.update([{...row, attention: false}], {interval}), false);
        assert.equal(sound.update([{...row, revision: 2}], {interval}), true);
        sound.update([{...row, state: 'working', attention: false}], {interval});
        time.seconds += interval;
        assert.equal(sound.update([], {interval}), false);
    });
}

for (const blocked of [{locked: true}, {enabled: false}, {available: false}, {suppressedIds: ['a']}]) {
    test(`suppression ${JSON.stringify(blocked)} defers reminders without a catch-up chime`, () => {
        const {time, sound, row} = reminders();
        sound.update([row], {interval: 60});
        time.seconds = 600;
        assert.equal(sound.update([row], {interval: 60, ...blocked}), false);
        assert.equal(sound.update([row], {interval: 60}), false);
        time.seconds = 659;
        assert.equal(sound.update([row], {interval: 60}), false);
        time.seconds = 660;
        assert.equal(sound.update([row], {interval: 60}), true);
    });
}

test('cadence changes take effect immediately and Once cancels pending repeats', () => {
    const {time, sound, row} = reminders();
    sound.update([row], {interval: 60});
    time.seconds = 30;
    assert.equal(sound.update([row], {interval: 300}), false);
    time.seconds = 329;
    assert.equal(sound.update([row], {interval: 300}), false);
    time.seconds = 330;
    assert.equal(sound.update([row], {interval: 300}), true);
    sound.update([row], {interval: 0});
    time.seconds = 10000;
    assert.equal(sound.update([row], {interval: 0}), false);
});

test('multiple waiting terminals share a reminder cadence', () => {
    const {time, sound, row} = reminders();
    assert.equal(sound.update([row], {interval: 60}), true);
    time.seconds = 30;
    const other = {...row, id: 'b'};
    assert.equal(sound.update([row, other], {interval: 60}), true);
    time.seconds = 60;
    assert.equal(sound.update([row, other], {interval: 60}), false);
    time.seconds = 90;
    assert.equal(sound.update([row, other], {interval: 60}), true);
    assert.equal(sound.update([row, other], {interval: 60}), false);
});
