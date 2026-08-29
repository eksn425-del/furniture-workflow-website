"use client";

import { useEffect } from "react";

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <section className="workspace-error" role="alert">
      <p className="folio">INTERFACE / ERROR</p>
      <span className="error-code">!</span>
      <h1>页面没有完成装订</h1>
      <p>{error.message || "界面发生未知错误，请重试。"}</p>
      <div className="error-actions">
        <button type="button" onClick={reset}>
          重新加载
        </button>
      </div>
    </section>
  );
}
