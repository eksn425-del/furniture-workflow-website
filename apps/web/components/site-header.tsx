import Link from "next/link";

const NAV_GROUPS = [
  { label: "网站", items: [{ href: "/sites", label: "网站总览", mark: "01" }, { href: "/jobs/new", label: "扫描新网站", mark: "+" }] },
  { label: "任务", items: [{ href: "/jobs", label: "工作流任务", mark: "02" }, { href: "/review", label: "人工处理", mark: "03" }] },
  { label: "交付", items: [{ href: "/delivery", label: "可下载模型", mark: "04" }] },
  { label: "系统", items: [{ href: "/system", label: "运行状态", mark: "05" }] },
];

export function SiteHeader() {
  return (
    <>
      <aside className="console-sidebar" aria-label="主导航">
        <Link className="console-brand" href="/" aria-label="Furniture Workflow Dashboard">
          <span className="console-brand-mark" aria-hidden="true"><i /></span>
          <span><strong>Furniture<br />Workflow</strong><small>PRODUCTION CONSOLE</small></span>
        </Link>
        <div className="console-tenant"><span className="console-tenant-dot" /><div><strong>Local workspace</strong><small>Development · v0.16.0</small></div><span className="console-tenant-menu">···</span></div>
        <nav className="console-nav">{NAV_GROUPS.map((group) => <div className="console-nav-group" key={group.label}><span className="console-nav-label">{group.label}</span>{group.items.map((item) => <Link href={item.href} className="console-nav-item" key={item.href}><span className="console-nav-mark">{item.mark}</span><span>{item.label}</span></Link>)}</div>)}</nav>
        <div className="console-sidebar-foot"><div className="console-runtime-state"><i /><span><strong>Native Runtime</strong><small>Website-owned · Provider OFF</small></span></div><Link href="/system" className="console-nav-item console-nav-item--small"><span className="console-nav-mark">?</span><span>帮助与诊断</span></Link></div>
      </aside>
      <header className="console-topbar">
        <div className="console-topbar-context"><span className="console-topbar-line" /><span>家具 AI 生产工作台</span><b>/</b><strong>Local environment</strong></div>
        <div className="console-topbar-actions"><span className="console-topbar-status"><i /> API 在线</span><span className="console-topbar-separator" /><span className="console-avatar">HZ</span><span className="console-user">Operator</span></div>
      </header>
    </>
  );
}
