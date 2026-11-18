#!/usr/bin/env python3
"""Evidence rules for an explicitly named, complete-world communicator.

Gradient purpose, large membership, and membership equal to DP are insufficient
to establish GLOBAL identity. This module has no dependency on feature 1.
"""
import re

WORLD_NAMES = frozenset(('hccl_world_group', 'world', 'global'))


def is_global_name(value):
    return str(value or '').strip().casefold() in WORLD_NAMES


def identify_global_group(detail, world_size):
    """Return (confirmed, reason), checking explicit semantics and exact ranks."""
    named = next(((field, str(detail[field])) for field in ('pg_name', 'group_name', 'type')
                  if field in detail and is_global_name(detail[field])), None)
    if named is None:
        return False, 'no explicit world/global communicator name; full membership alone is insufficient'
    return validate_global_members(detail.get('global_ranks', []), world_size,
                                   f'metadata {named[0]}={named[1]}')


def validate_global_members(members, world_size, identity_evidence):
    """Explicit GLOBAL overrides must satisfy the same completeness rule."""
    try:
        world_size = int(world_size)
        members = [int(rank) for rank in members]
    except (TypeError, ValueError):
        return False, 'GLOBAL world_size/membership is missing, conflicting or invalid'
    if world_size <= 0 or len(members) != world_size or len(set(members)) != world_size:
        return False, f'{identity_evidence}; communicator does not contain every world rank exactly once'
    if set(members) != set(range(world_size)):
        return False, f'{identity_evidence}; communicator membership differs from complete world rank range'
    return True, f'{identity_evidence}; exact complete-world members verified against world_size={world_size}'


def purpose_of_event(event):
    """Preserve gradient evidence; expose only specific recorded CPU contexts.

    CPU overlap is reported as context evidence, not as an invented causal
    association. Unknown purposes remain Other and retain their full context.
    """
    if event.get('metric_category') == 'Grad AR':
        return 'Grad AR', 'specific gradient norm/sync CPU context already recorded on this AllReduce event'
    names = event.get('cpu_context', [])
    barriers = [name for name in names if re.search(r'(?:\.py\(\d+\):|:)barrier$', name, re.I)]
    if barriers:
        return 'Barrier', 'temporally overlapping recorded CPU barrier: ' + '; '.join(barriers)
    timers = [name for name in names if re.search(r'(?:^|[/\\])timers\.py\(', name, re.I)]
    if timers:
        return 'Timer', 'temporally overlapping recorded timer CPU function: ' + '; '.join(timers)
    return '未确认', 'no specific gradient/timer/barrier purpose established; raw CPU contexts retained'
