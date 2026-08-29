import { SiteDetailPage } from "@/components/production-console";

export default async function SiteDetailRoute({ params }: { params: Promise<{ siteKey: string }> }) {
  const { siteKey } = await params;
  return <SiteDetailPage siteKey={decodeURIComponent(siteKey)} />;
}
