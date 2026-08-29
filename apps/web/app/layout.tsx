import type { Metadata } from "next";
import type { ReactNode } from "react";
import { SiteHeader } from "@/components/site-header";
import AppErrorBoundary from "@/components/app-error-boundary";
import "@/app/globals.css";

export const metadata: Metadata = {
  title: {
    default: "家具自动化工作流",
    template: "%s｜家具自动化工作流",
  },
  description: "家具资料输入、自动处理与结果输出。",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>
        <a className="skip-link" href="#main-content">
          跳到主要内容
        </a>
        <div className="page-shell console-shell">
          <AppErrorBoundary>
            <SiteHeader />
            <main id="main-content" className="console-main">{children}</main>
          </AppErrorBoundary>
        </div>
      </body>
    </html>
  );
}
