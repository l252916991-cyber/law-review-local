import assert from 'node:assert/strict';
import { test } from 'node:test';
import { consumeStream, parseEvent } from '../src/lib/stream.ts';

const frame = (type: string, data: unknown) => `event:${type}\r\ndata:${JSON.stringify(data)}\r\n\r\n`;
function body(text: string) {
  const bytes = new TextEncoder().encode(text);
  return new ReadableStream<Uint8Array>({ start(controller) {
    // One byte per chunk exercises CRLF and multi-byte UTF-8 boundaries.
    for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
    controller.close();
  } });
}
test('split UTF-8 and CRLF frames preserve provisional text and terminal order', async () => {
  const events: string[] = [];
  await consumeStream(body(': keepalive\r\n\r\n' + frame('status', { phase: 'retrieval' }) + frame('token', { text: '证据', provisional: true }) + frame('citations', { citations: [] }) + frame('final', { answer: '证据' }) + frame('done', { status: 'ok' })), e => events.push(e.type));
  assert.deepEqual(events, ['status', 'token', 'citations', 'final', 'done']);
});
test('truncated stream cannot be accepted as a completed answer', async () => {
  await assert.rejects(consumeStream(body(frame('status', { phase: 'retrieval' }) + frame('token', { text: '草稿', provisional: true })), () => {}), /连接中断/);
});
test('retraction clears the provisional path and ends as failure', async () => {
  const events: string[] = [];
  await consumeStream(body(frame('status', { phase: 'retrieval' }) + frame('retract', { message: '拒绝' }) + frame('done', { status: 'failed' })), e => events.push(e.type));
  assert.deepEqual(events, ['status', 'retract', 'done']);
  await assert.rejects(consumeStream(body(frame('status', { phase: 'retrieval' }) + frame('done', { status: 'ok' })), () => {}), /答案未完成/);
});
test('unknown events are ignored; malformed known events fail closed', () => {
  assert.equal(parseEvent(frame('future', {})), null);
  assert.throws(() => parseEvent(frame('token', { text: 'x' })), /格式无效/);
});
test('token before status is rejected', async () => {
  await assert.rejects(consumeStream(body(frame('token', { text: 'x', provisional: true })), () => {}), /初始状态/);
});
