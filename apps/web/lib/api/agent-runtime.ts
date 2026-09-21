import { ApiError, apiBaseUrl, apiRequest, isRecord, type AuthInput } from "@/lib/api/client";

/**
 * Runtime API client for the Agent Playground.
 *
 * This module speaks the `/agent-runs/**` runtime contract only. The
 * observability product API (`lib/runs.ts`, `/runs/**`) is a different
 * contract with a different response shape and is never mixed in here.
 */

function runtimeBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}

/**
 * Authoritative runtime Run as persisted by the backend. After any
 * terminal stream event the UI re-reads this record and trusts it over
 * whatever the stream accumulated.
 */
export type AgentRun = {
  id: string;
  workspace_id: string;
  agent_version_id: string;
  status: string;
  /**
   * Null for a reader who only holds `workspace_read`: the backend withholds
   * the raw prompt and the model's answer from anyone without `agent_run`.
   * The fields stay present in the contract; only their content is gated.
   */
  input_text: string | null;
  final_output: string | null;
  failure_code: string | null;
  resolved_spec_hash: string | null;
  /** One entry per resolved knowledge snapshot, not a keyed object. */
  effective_knowledge_snapshots: Array<Record<string, unknown>>;
  model_step_count: number;
  tool_call_count: number;
  total_input_tokens: number | null;
  total_output_tokens: number | null;
  total_tokens: number | null;
  total_cached_tokens: number | null;
  /** Decimal on the wire: it may arrive as a number or a string. */
  total_cost_amount: number | string | null;
  cost_currency: string | null;
  cost_is_estimate: boolean | null;
  created_by: string;
  created_at: string;
  /** A persisted run has always started; only completion is optional. */
  started_at: string;
  completed_at: string | null;
};

export function getAgentRun(input: AuthInput, runId: string): Promise<AgentRun> {
  return apiRequest<AgentRun>(
    `${runtimeBase(input.workspaceId)}/agent-runs/${encodeURIComponent(runId)}`,
    input.accessToken,
  );
}

/**
 * Creates a new non-streaming run for a published agent version.
 *
 * Replay uses this: it is a brand new run with its own id, not a resume
 * of an existing one. The backend decides everything else, including
 * whether the new run pauses for approval.
 */
export function createAgentRun(
  input: AuthInput,
  options: { agentVersionId: string; inputText: string },
): Promise<AgentRun> {
  return apiRequest<AgentRun>(
    `${runtimeBase(input.workspaceId)}/agent-versions/` +
      `${encodeURIComponent(options.agentVersionId)}/runs`,
    input.accessToken,
    { method: "POST", body: { input_text: options.inputText } },
  );
}

/**
 * Asks the backend to stop the run. The result may legitimately still be
 * CANCEL_REQUESTED — that is a request in flight, not a cancelled run.
 */
export function cancelAgentRun(input: AuthInput, runId: string): Promise<AgentRun> {
  return apiRequest<AgentRun>(
    `${runtimeBase(input.workspaceId)}/agent-runs/${encodeURIComponent(runId)}/cancel`,
    input.accessToken,
    { method: "POST", body: {} },
  );
}

/** Frozen runtime event vocabulary. No client-invented types are accepted. */
export const AGENT_EVENT_TYPES = [
  "run.started",
  "message.started",
  "context.budget",
  "message.delta",
  "message.completed",
  "retrieval.started",
  "retrieval.completed",
  "rerank.completed",
  "tool.requested",
  "tool.started",
  "tool.completed",
  "tool.failed",
  "approval.required",
  "approval.resolved",
  "run.cancel_requested",
  "usage",
  "run.completed",
  "run.failed",
  "run.cancelled",
] as const;

export type AgentEventType = (typeof AGENT_EVENT_TYPES)[number];

export type AgentEvent = {
  event_id: string;
  type: AgentEventType;
  request_id: string;
  run_id: string;
  step_id: string | null;
  timestamp: string;
  payload: Record<string, unknown>;
  sequence: number;
  agent_version_id: string;
};

/** Raised for any frame the runtime contract cannot explain. */
export function streamProtocolError(message: string): ApiError {
  return new ApiError("STREAM_PROTOCOL_ERROR", message, 0);
}

function isKnownEventType(value: string): value is AgentEventType {
  return (AGENT_EVENT_TYPES as readonly string[]).includes(value);
}

/**
 * One decoded SSE frame. `comment` frames (`: heartbeat`) carry no data
 * field and are never business events.
 */
type SseFrame = { event: string | null; data: string | null };

function decodeFrame(raw: string): SseFrame {
  let event: string | null = null;
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    // A leading colon marks a comment; heartbeats arrive exactly this way.
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }
  return { event, data: dataLines.length > 0 ? dataLines.join("\n") : null };
}

/**
 * Validates one frame against the frozen AgentEvent contract.
 * Returns null for heartbeats and other dataless frames; throws a
 * STREAM_PROTOCOL_ERROR for frames that claim to be events but are not.
 */
export function parseAgentEventFrame(raw: string): AgentEvent | null {
  const frame = decodeFrame(raw);
  if (frame.data === null) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    throw streamProtocolError("A stream frame contained data that is not valid JSON.");
  }
  if (!isRecord(parsed)) {
    throw streamProtocolError("A stream frame contained data that is not an object.");
  }
  if (typeof parsed.type !== "string") {
    throw streamProtocolError("A stream event is missing its type.");
  }
  if (!isKnownEventType(parsed.type)) {
    throw streamProtocolError(`A stream event used an unknown type: ${parsed.type}`);
  }
  if (typeof parsed.run_id !== "string") {
    throw streamProtocolError("A stream event is missing its run id.");
  }
  if (typeof parsed.sequence !== "number") {
    throw streamProtocolError("A stream event is missing its sequence.");
  }
  if (!isRecord(parsed.payload)) {
    throw streamProtocolError("A stream event is missing its payload object.");
  }
  return {
    event_id: typeof parsed.event_id === "string" ? parsed.event_id : "",
    type: parsed.type,
    request_id: typeof parsed.request_id === "string" ? parsed.request_id : "",
    run_id: parsed.run_id,
    step_id: typeof parsed.step_id === "string" ? parsed.step_id : null,
    timestamp: typeof parsed.timestamp === "string" ? parsed.timestamp : "",
    payload: parsed.payload,
    sequence: parsed.sequence,
    agent_version_id: typeof parsed.agent_version_id === "string" ? parsed.agent_version_id : "",
  };
}

/**
 * Incremental SSE reader: network chunks and frame boundaries are
 * unrelated, so bytes are buffered until a blank line completes a frame.
 * CRLF and bare CR line endings are normalised first.
 */
export class SseFrameBuffer {
  private buffer = "";

  push(chunk: string): string[] {
    this.buffer += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const frames: string[] = [];
    let boundary = this.buffer.indexOf("\n\n");
    while (boundary !== -1) {
      frames.push(this.buffer.slice(0, boundary));
      this.buffer = this.buffer.slice(boundary + 2);
      boundary = this.buffer.indexOf("\n\n");
    }
    return frames;
  }

  /** Whatever the server left unterminated when the stream ended. */
  flush(): string[] {
    const rest = this.buffer;
    this.buffer = "";
    return rest.trim() === "" ? [] : [rest];
  }
}

/**
 * Turns a live SSE response into events.
 *
 * Shared by the endpoint that starts a run and the one that follows an
 * existing one, because the framing, the error envelope and the
 * cancellation handling are identical and a second copy of them is a
 * second place for them to drift.
 */
async function consumeEventStream(
  response: Response,
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  if (!response.ok) {
    // A structured failure before the stream starts is still the normal
    // error envelope; it must not be reported as a network problem.
    const body = (await response.json().catch(() => null)) as unknown;
    const error = isRecord(body) && isRecord(body.error) ? body.error : {};
    throw new ApiError(
      typeof error.code === "string" ? error.code : "REQUEST_FAILED",
      typeof error.message === "string" ? error.message : "The API request failed.",
      response.status,
    );
  }
  if (!response.body) {
    throw streamProtocolError("The streaming response carried no body.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const frames = new SseFrameBuffer();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      for (const frame of frames.push(decoder.decode(value, { stream: true }))) {
        const event = parseAgentEventFrame(frame);
        if (event) onEvent(event);
      }
    }
    for (const frame of frames.flush()) {
      const event = parseAgentEventFrame(frame);
      if (event) onEvent(event);
    }
  } finally {
    reader.cancel().catch(() => undefined);
  }
}

function bearer(input: AuthInput): string {
  const token = input.accessToken.trim();
  if (!token) {
    throw new ApiError("SESSION_REQUIRED", "A workspace session with an access token is required.", 401);
  }
  return `Bearer ${token}`;
}

/**
 * Opens the streaming run.
 *
 * Streaming is POST + Authorization + JSON body, which EventSource
 * cannot express, so this uses fetch + ReadableStream directly. Reaching
 * end-of-stream is not an error: the backend deliberately closes the
 * stream when a run pauses for approval.
 */
export async function streamAgentRun(
  input: AuthInput,
  options: {
    agentVersionId: string;
    inputText: string;
    signal: AbortSignal;
    onEvent: (event: AgentEvent) => void;
  },
): Promise<void> {
  const authorization = bearer(input);
  const path =
    `${runtimeBase(input.workspaceId)}/agent-versions/` +
    `${encodeURIComponent(options.agentVersionId)}/runs/stream`;

  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: "POST",
    headers: {
      Authorization: authorization,
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    credentials: "include",
    body: JSON.stringify({ input_text: options.inputText }),
    signal: options.signal,
  });

  await consumeEventStream(response, options.onEvent);
}

/**
 * Follows a run this connection did not start.
 *
 * `afterSequence` is the last sequence the caller already processed; the
 * backend replays everything after it from the durable event log before
 * joining the live stream. That is what makes a reload or a dropped
 * connection cost frames rather than the run: pass 0 to rebuild the whole
 * timeline, or the highest sequence seen to resume exactly where the
 * stream broke. Replayed events carry their original sequence numbers, so
 * a caller that keys off `sequence` never double-counts one.
 */
export async function followAgentRun(
  input: AuthInput,
  options: {
    runId: string;
    afterSequence?: number;
    signal: AbortSignal;
    onEvent: (event: AgentEvent) => void;
  },
): Promise<void> {
  const authorization = bearer(input);
  const after = options.afterSequence ?? 0;
  const path =
    `${runtimeBase(input.workspaceId)}/agent-runs/` +
    `${encodeURIComponent(options.runId)}/stream?after_sequence=${encodeURIComponent(String(after))}`;

  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: "GET",
    headers: { Authorization: authorization, Accept: "text/event-stream" },
    credentials: "include",
    signal: options.signal,
  });

  await consumeEventStream(response, options.onEvent);
}

/** Reads a string field from an event payload, rejecting other shapes. */
export function payloadString(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

export function payloadNumber(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" ? value : null;
}
