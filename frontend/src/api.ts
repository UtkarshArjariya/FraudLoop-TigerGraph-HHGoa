import type { Role, TimelineEvent } from './types';
export let activeRole: Role = (localStorage.getItem('fraudloop-role') as Role) || 'viewer';
export function setApiRole(role: Role) { activeRole = role; localStorage.setItem('fraudloop-role', role); }
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
 const response = await fetch(`/api${path}`, { ...init, headers: {'Content-Type': 'application/json', 'X-Role': activeRole, ...init.headers} });
 if (!response.ok) {
  const body = await response.json().catch(() => ({}));
  const detail = body.error?.message || body.detail || `Request failed (${response.status})`;
  throw new Error(typeof detail === 'string' ? detail : 'Check the supplied IDs and required fields.');
 }
 return response.json() as Promise<T>;
}
export async function streamCase(caseId: string, mode: 'live' | 'replay', signal: AbortSignal, receive: (event: TimelineEvent) => void) {
 const response = await fetch(`/api/cases/${encodeURIComponent(caseId)}/investigate?mode=${mode}`, {method:'POST', headers:{'X-Role': activeRole}, signal});
 if (!response.ok || !response.body) {
  const body = await response.json().catch(() => ({}));
  throw new Error(body.error?.message || body.detail || `Stream unavailable (${response.status})`);
 }
 const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
 const dispatch = (block: string) => {
  const lines = block.split('\n'); const data = lines.filter(line => line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
  if (!data) return;
  const value = JSON.parse(data);
  const eventType = lines.find(line=>line.startsWith('event:'))?.slice(6).trim() || value.event_type || 'message';
  receive(value.payload ? {...value, event_type: value.event_type || eventType} : {event_type:eventType,payload:value});
 };
 while (true) {
  const {value, done} = await reader.read(); buffer += decoder.decode(value, {stream: !done}).replace(/\r\n/g, '\n');
  let index: number; while ((index = buffer.indexOf('\n\n')) >= 0) { dispatch(buffer.slice(0, index)); buffer = buffer.slice(index + 2); }
  if (done) { if (buffer.trim()) dispatch(buffer); break; }
 }
}
