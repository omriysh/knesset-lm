/**
 * sse.js — Async iterator over Server-Sent Events from a fetch Response.
 * Yields { event, data } objects, where `event` is the most recent `event:`
 * line and `data` is the parsed JSON from a `data:` line.
 */
export async function* sseLines(res) {
  const reader  = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '', curEvent = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split('\n');
    buf = lines.pop();
    for (const line of lines) {
      if (line.startsWith('event: ')) {
        curEvent = line.slice(7).trim();
      } else if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          yield { event: curEvent, data };
        } catch (exc) {
          console.error('[sse] JSON parse failed:', exc, 'line=', line.slice(0, 200));
        }
      }
    }
  }
}
