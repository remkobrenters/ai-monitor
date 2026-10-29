"""Metadata-only subagent counts, scoped to one parent terminal session."""

import math

LIFECYCLE = {'SubagentStart', 'SubagentStop'}
MAX_TRACKED = 512


def count(row):
    return row.get('_subagentUnknown', 0) + sum(
        bool(agent['active']) for agent in row.get('_subagents', {}).values())


def reset(row, sent):
    row.update(_subagents={}, _subagentUnknown=0, _subagentResetAt=sent,
               _subagentSnapshotAt=sent, waitingForSubagents=False)


def update(row, event, sent):
    """Deduplicate exact IDs; child events have their own ordering, not the parent's."""
    if sent <= max(row.get('_subagentResetAt', 0), row.get('_subagentSnapshotAt', 0)):
        return
    agents = row.setdefault('_subagents', {})
    identifier = event.get('agentId')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
        return
    previous = agents.get(identifier)
    if previous and sent <= previous['at']:
        return
    active = event.get('event') == 'SubagentStart'
    # A Stop snapshot can include agents whose start hooks were missed. Consume
    # those unknown agents once as their exact stop identities become available.
    if row.get('_subagentUnknown', 0) and (
            (previous and previous.get('snapshotActive')) or (not active and not previous)):
        row['_subagentUnknown'] -= 1
    if identifier not in agents and len(agents) >= MAX_TRACKED:
        inactive = [key for key, value in agents.items() if not value['active']]
        if not inactive:
            return
        del agents[min(inactive, key=lambda key: agents[key]['at'])]
    agents[identifier] = {'active': active, 'at': sent}
    if not count(row):
        row['waitingForSubagents'] = False


def snapshot(row, event, sent):
    """Reconcile Claude's optional in-flight subagent count, excluding shell tasks."""
    value = event.get('backgroundSubagents')
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TRACKED:
        return
    agents = row.get('_subagents', {})
    if any(agent['at'] > sent for agent in agents.values()):
        return
    if not math.isfinite(sent):
        return
    row['_subagents'] = {key: {'active': False, 'at': agent['at'],
        'snapshotActive': bool(value and (agent['active'] or agent.get('snapshotActive')))}
        for key, agent in agents.items()}
    row['_subagentUnknown'] = value
    row['_subagentSnapshotAt'] = sent
