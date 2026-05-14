/**
 * transforms.js — pure transforms applied to backend SSE payloads.
 */

// Status messages displayed when the backend emits a `progress` subgraph event.
export const PROGRESS_MSGS = {
  planning_started:          'מתכנן שלבי חקר...',
  executing:                 'מבצע שלבי חקר...',
  synthesizing:              'מסכם ממצאים...',
  replanning:                'מתכנן מחדש...',
  critic_pre_revise:         'מתקן תוכנית...',
  validator_revise:          'מאמת תוכנית...',
  critic_post_started:       'בודק תוצאות...',
  critic_post_replan_capped: 'מסכם למרות תוצאות חלקיות...',
};

// Categorize a subgraph phase name (e.g. "executor:s2:t1", "synthesizer:expand").
export function classifyPhase(name) {
  const n = name || '';
  if (n.startsWith('executor:'))   return { type: 'executor', stepKey: n.split(':')[1] || n, name: n };
  if (n === 'synthesizer:expand')  return { type: 'synthesizer_expand', name: n };
  if (n.startsWith('synthesizer')) return { type: 'synthesizer', name: n };
  return { type: 'other', name: n };
}

// Extract the human-readable step task from an executor prompt (the line `    task: ...`).
export function extractTaskLabel(prompt) {
  const text = (prompt || {}).user || '';
  const m = text.match(/^\s+task:\s+(.+)$/m);
  return m ? m[1].trim() : null;
}

// Hebrew label for a subgraph phase, used as the card heading.
export function subgraphPhaseLabel(phase) {
  const labels = {
    'planner':        'מתכנן שלבי חקר',
    'planner_replan': 'מתכנן מחדש',
    'critic_pre':     'ביקורת תוכנית',
    'validator':      'אימות תוכנית',
    'critic_post':    'ביקורת תוצאות',
    'synthesizer':    'מסכם ממצאים',
  };
  if (phase && phase.startsWith('executor:')) {
    const stepId = phase.split(':')[1] || '';
    return `ביצוע ${stepId}`;
  }
  return labels[phase] || phase || 'שלב';
}

// Normalize a step_completed payload into a uniform tool-results array.
export function mapToolResults(payload) {
  const toolCallResults = payload.tool_call_results || [];
  const toolCalls       = payload.tool_calls        || [];
  const toolName        = payload.tool_name         || '';
  const fullResult      = payload.full              || '';

  if (toolCallResults.length > 0) {
    return toolCallResults.map(tc => ({
      name:       tc.name,
      args:       tc.args || {},
      result:     tc.full || tc.summary || '',
      result_ref: tc.result_ref || null,
    }));
  }
  if (toolCalls.length === 1) {
    return [{ name: toolCalls[0].name || toolName || 'כלי', args: toolCalls[0].args || {}, result: fullResult }];
  }
  if (toolCalls.length > 1) {
    const results = toolCalls.map(tc => ({ name: tc.name, args: tc.args || {}, result: '' }));
    if (fullResult) results.push({ name: 'תוצאה מלאה', args: {}, result: fullResult });
    return results;
  }
  if (fullResult) {
    return [{ name: toolName || 'תוצאה מלאה', args: {}, result: fullResult }];
  }
  return [];
}
