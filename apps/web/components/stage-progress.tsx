import type { StageSummary } from "@/lib/types";
import { findStageData, findStageDefinition, STAGE_DEFINITIONS } from "@/lib/workflow";
import { StatusChip } from "@/components/status-chip";

export function StageProgress({ stages, currentStage }: { stages: StageSummary[]; currentStage?: string | null }) {
  const current = findStageDefinition(currentStage);
  return (
    <ol className="simple-stage-list" aria-label="处理进度">
      {STAGE_DEFINITIONS.map((definition) => {
        const stage = findStageData(stages, definition);
        return (
          <li data-current={current?.key === definition.key || undefined} key={definition.key}>
            <span>{definition.order}</span>
            <strong>{definition.label}</strong>
            <StatusChip status={stage?.status} />
          </li>
        );
      })}
    </ol>
  );
}
