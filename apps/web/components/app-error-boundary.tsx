"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 全局客户端错误边界：包裹整个页面外壳（Header + Main）。
 * 任何子组件渲染阶段抛错都会被这里捕获，显示可读错误页而非黑屏；
 * 同时避免 Next dev overlay 全屏盖住页面。
 */
export default class AppErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[AppErrorBoundary]", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <section className="workspace-error" role="alert">
          <p className="folio">INTERFACE / ERROR</p>
          <span className="error-code">!</span>
          <h1>界面发生异常</h1>
          <p>{this.state.error.message || "未知错误，请重试。"}</p>
          <div className="error-actions">
            <button
              type="button"
              onClick={() => this.setState({ error: null })}
            >
              尝试恢复
            </button>
          </div>
        </section>
      );
    }
    return this.props.children;
  }
}