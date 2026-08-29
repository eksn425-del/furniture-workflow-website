import { statusLabel, statusTone } from "@/lib/workflow";

export function StatusChip({ status }: { status?: string | null }) {
  return (
    <span className="status-chip" data-tone={statusTone(status)}>
      <span className="status-dot" aria-hidden="true" />
      {statusLabel(status)}
    </span>
  );
}
