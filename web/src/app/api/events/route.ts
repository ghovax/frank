export const dynamic = "force-dynamic";

const BACKEND_URL = process.env.BACKEND_URL ?? "http://localhost:8822";

// Proxy the backend's live SSE event stream (agents changed, etc.) through.
export async function GET(request: Request): Promise<Response> {
  const upstream = await fetch(`${BACKEND_URL}/events`, {
    headers: { Accept: "text/event-stream" },
    cache: "no-store",
    signal: request.signal,
  });
  if (!upstream.ok || !upstream.body) {
    return new Response("Backend error", { status: upstream.status });
  }
  return new Response(upstream.body, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
}
