import http from "node:http";
import type { AddressInfo } from "node:net";

export type RecordedRequest = {
  method: string;
  url: string;
  headers: http.IncomingHttpHeaders;
  body: Buffer;
};

export type MockUpstream = {
  url: string;
  port: number;
  requests: RecordedRequest[];
  close: () => Promise<void>;
};

/**
 * A tiny local upstream used in place of real providers (CLAUDE.md §8: no live
 * provider calls in CI). It records each request verbatim and responds with a
 * provider-shaped JSON or SSE body that includes a `usage` block.
 *
 * Route → provider shape:
 *   /v1/messages         -> Anthropic
 *   /v1/chat/completions -> OpenAI
 * Streaming is chosen when the request body has {"stream": true}.
 */
export async function startMockUpstream(): Promise<MockUpstream> {
  const requests: RecordedRequest[] = [];

  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks);
      requests.push({
        method: req.method ?? "",
        url: req.url ?? "",
        headers: req.headers,
        body,
      });

      let parsed: Record<string, unknown> = {};
      try {
        parsed = JSON.parse(body.toString("utf8"));
      } catch {
        /* ignore */
      }
      const isAnthropic = (req.url ?? "").includes("/v1/messages");
      const stream = parsed.stream === true;

      if (stream) {
        respondStream(res, isAnthropic);
      } else {
        respondJSON(res, isAnthropic);
      }
    });
  });

  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = (server.address() as AddressInfo).port;

  return {
    url: `http://127.0.0.1:${port}`,
    port,
    requests,
    close: () => new Promise((resolve) => server.close(() => resolve())),
  };
}

function respondJSON(res: http.ServerResponse, isAnthropic: boolean): void {
  const body = isAnthropic
    ? {
        id: "msg_mock",
        type: "message",
        role: "assistant",
        model: "claude-test",
        content: [{ type: "text", text: "hello from anthropic" }],
        usage: { input_tokens: 42, output_tokens: 7 },
      }
    : {
        id: "chatcmpl_mock",
        object: "chat.completion",
        model: "gpt-test",
        choices: [{ index: 0, message: { role: "assistant", content: "hello from openai" } }],
        usage: { prompt_tokens: 42, completion_tokens: 7, total_tokens: 49 },
      };
  const payload = Buffer.from(JSON.stringify(body));
  res.writeHead(200, { "content-type": "application/json", "content-length": payload.length });
  res.end(payload);
}

function respondStream(res: http.ServerResponse, isAnthropic: boolean): void {
  res.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive",
  });

  const events = isAnthropic
    ? [
        `event: message_start\ndata: ${JSON.stringify({
          type: "message_start",
          message: { id: "msg_mock", usage: { input_tokens: 42, output_tokens: 0 } },
        })}\n\n`,
        `event: content_block_delta\ndata: ${JSON.stringify({
          type: "content_block_delta",
          delta: { type: "text_delta", text: "hello" },
        })}\n\n`,
        `event: message_delta\ndata: ${JSON.stringify({
          type: "message_delta",
          usage: { output_tokens: 7 },
        })}\n\n`,
      ]
    : [
        `data: ${JSON.stringify({
          choices: [{ index: 0, delta: { content: "hello" } }],
        })}\n\n`,
        `data: ${JSON.stringify({
          choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
          usage: { prompt_tokens: 42, completion_tokens: 7, total_tokens: 49 },
        })}\n\n`,
        `data: [DONE]\n\n`,
      ];

  // Emit events across separate ticks so the test can observe progressive streaming.
  let i = 0;
  const tick = (): void => {
    if (i < events.length) {
      res.write(events[i++]);
      setTimeout(tick, 10);
    } else {
      res.end();
    }
  };
  tick();
}
