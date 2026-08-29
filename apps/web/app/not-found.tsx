import Link from "next/link";

export default function NotFound() {
  return (
    <section className="workspace-error">
      <p className="folio">ARCHIVE / NOT FOUND</p>
      <span className="error-code">404</span>
      <h1>没有找到这份档案</h1>
      <p>地址可能已变更，或该项目从未由控制面创建。</p>
      <div className="error-actions">
        <Link href="/">返回项目登记簿</Link>
      </div>
    </section>
  );
}
