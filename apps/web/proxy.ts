import { NextRequest, NextResponse } from "next/server";


function unauthorized(): NextResponse {
  return new NextResponse("Company login required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Furniture Workflow", charset="UTF-8"' },
  });
}

export function proxy(request: NextRequest): NextResponse {
  const expectedUser = process.env.INTRANET_AUTH_USER?.trim();
  const expectedPassword = process.env.INTRANET_AUTH_PASSWORD;
  if (expectedUser && expectedPassword) {
    const authorization = request.headers.get("authorization") || "";
    if (!authorization.startsWith("Basic ")) return unauthorized();
    try {
      const decoded = atob(authorization.slice(6));
      const separator = decoded.indexOf(":");
      const user = decoded.slice(0, separator);
      const password = decoded.slice(separator + 1);
      if (separator < 0 || user !== expectedUser || password !== expectedPassword) {
        return unauthorized();
      }
    } catch {
      return unauthorized();
    }
  }

  const headers = new Headers(request.headers);
  const serviceToken = process.env.INTERNAL_SERVICE_TOKEN?.trim();
  if (serviceToken && request.nextUrl.pathname.startsWith("/api/")) {
    headers.set("x-internal-service-token", serviceToken);
  }
  return NextResponse.next({ request: { headers } });
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
