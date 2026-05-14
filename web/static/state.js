/**
 * state.js — module-level state shared across the chat UI.
 * Mutable; importers read/write fields directly via the exported `state` object.
 */
export const state = {
  running:             false,
  sessionId:           null,   // current / last session id
  lastQuestion:        '',     // most recent user question (for explore-sources)
  reconnectSessionId:  null,   // set on stream start, cleared on clean done/error
  reconnecting:        false,  // true while attemptReconnect is looping
  reconnectErrorEl:    null,   // red error card to remove on reconnect
  currentStagesEl:     null,   // .ai-stages-card of active session
};
