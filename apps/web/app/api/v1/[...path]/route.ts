import { NextRequest } from "next/server";

type RouteContext = { params: Promise<{ path: string[] }> };

const REQUEST_HEADERS_TO_REMOVE = [
  "authorization",
  "connection",
  "content-length",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

const RESPONSE_HEADERS_TO_REMOVE = [
  "connection",
  "content-length",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

async function forward(request: NextRequest, context: RouteContext): Promise<Response> {
  const upstream = process.env.API_INTERNAL_URL?.replace(/\/+$/, "") || "http://127.0.0.1:8000";
  const serviceToken = process.env.INTERNAL_SERVICE_TOKEN?.trim();

  const { path } = await context.params;
  const target = new URL(`${upstream}/api/v1/${path.map(encodeURIComponent).join("/")}`);
  target.search = request.nextUrl.search;

  const headers = new Headers(request.headers);
  for (const name of REQUEST_HEADERS_TO_REMOVE) headers.delete(name);
  if (serviceToken) headers.set("x-internal-service-token", serviceToken);
  else headers.delete("x-internal-service-token");

  try {
    const response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
    });
    const responseHeaders = new Headers(response.headers);
    for (const name of RESPONSE_HEADERS_TO_REMOVE) responseHeaders.delete(name);
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: responseHeaders,
    });
  } catch {
    return Response.json({ detail: "Internal API is temporarily unavailable" }, { status: 502 });
  }
}

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

export { forward as DELETE, forward as GET, forward as PATCH, forward as POST, forward as PUT };
