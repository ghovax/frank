export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8822";

// Proxy A2A JSON-RPC calls (per-agent endpoints under /a2a/agents/...) to the
// backend, streaming SSE responses (message/stream) straight through.
export async function POST(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> }
): Promise<Response> {
  const { path } = await params;
  const body = await request.text();

  const upstream = await fetch(`${BACKEND_URL}/a2a/${path.join("/")}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body,
    cache: "no-store",
    signal: request.signal,
  });

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "Backend error");
    return new Response(text, { status: upstream.status });
  }

  const contentType = upstream.headers.get("content-type") ?? "";
  if (contentType.includes("text/event-stream")) {
    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      },
    });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "Content-Type": contentType || "application/json" },
  });
}
