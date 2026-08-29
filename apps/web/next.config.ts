import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  // Next.js 16 dev 默认拦截跨主机名的开发资源请求（如 127.0.0.1 vs localhost），
  // 会导致页面无法 hydrate、按钮永久停在忙碌状态。这里放行本机两种主机名。
  allowedDevOrigins: ["localhost", "127.0.0.1"],
  async headers() {
    return [{
      source: "/:path*",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "Referrer-Policy", value: "same-origin" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
      ],
    }];
  },
};

export default nextConfig;
