#!/usr/bin/env python3
"""Remove preview work from the deployed v1 agent without changing its player."""
import ast


def strip_agent(source):
    tree = ast.parse(source)
    ranges = []
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in ('_emit_preview', '_jpeg'):
            ranges.append((node.lineno-1, node.end_lineno))
            found |= node.name == '_emit_preview'
        elif isinstance(node, ast.If) and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == '_emit_preview' for call in ast.walk(node)):
            ranges.append((node.lineno-1, node.end_lineno))
    if not found:
        raise ValueError('Expected legacy preview generator; refusing unknown agent')
    lines = source.splitlines(keepends=True)
    # The call is inside a dedicated timing block in the deployed legacy loop.
    for start, end in sorted(set(ranges), reverse=True):
        del lines[start:end]
    source = ''.join(lines)
    source = ''.join(line for line in source.splitlines(keepends=True)
                     if line.strip() not in ('import base64', 'next_pv = 0.0',
                                             'pv_interval = 1.0 / max(self.pv_fps, 1)'))
    if '_emit_preview(' in source or 'render_preview(' in source:
        raise ValueError('Preview call remains')
    compile(source, 'agent.py', 'exec')
    return source


def strip_ui(source):
    source = ''.join(line for line in source.splitlines(keepends=True)
                     if not any(token in line for token in ('previews: {},', 'if (state.previews[', '`<img id="thumb-')))
    start = source.index('  } else if (m.preview) {')
    end = source.index('  } else if (m.type === "show")', start)
    source = source[:start] + source[end:]
    start = source.index('function updatePreview(node)')
    end = source.index('\n}', start) + 2
    return source[:start] + source[end:]


def status_ui(source, current_ui):
    """Backport status classification without changing the live playback API."""
    if 'heartbeatSeen: {}' not in source:
        source = source.replace('  heartbeats: {},', '  heartbeats: {},\n  heartbeatSeen: {},', 1)
        source = source.replace('    state.heartbeats = m.heartbeats || {};',
            '    state.heartbeats = m.heartbeats || {};\n    for (const node of Object.keys(state.heartbeats)) state.heartbeatSeen[node] = performance.now();', 1)
        source = source.replace('    state.heartbeats[m.hb.node] = m.hb;',
            '    state.heartbeats[m.hb.node] = m.hb;\n    state.heartbeatSeen[m.hb.node] = performance.now();', 1)
    if 'm.type === "nodes"' not in source:
        source = source.replace('  } else if (m.type === "power") {',
            '  } else if (m.type === "nodes") {\n    state.nodesUp = m.nodes_up || [];\n    renderThumbDots();\n  } else if (m.type === "power") {', 1)
    start = current_ui.index('function renderThumbDots()')
    end = current_ui.index('\n}', start) + 2
    new = current_ui[start:end]
    start = source.index('function renderThumbDots()')
    end = source.index('\n}', start) + 2
    source = source[:start] + new + source[end:]
    if 'setInterval(renderThumbDots, 1000)' not in source:
        source = source.replace('  connect();', '  connect();\n  setInterval(renderThumbDots, 1000);', 1)
    return source
