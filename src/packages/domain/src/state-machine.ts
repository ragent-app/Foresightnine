import type { ResearchStatus } from "@foresightnine/contracts";

export const TRANSITIONS: Readonly<Record<ResearchStatus, readonly ResearchStatus[]>> = {
  DRAFT: ["CHALLENGING"],
  CHALLENGING: ["EVIDENCE_REQUIRED", "CONFIRMED"],
  EVIDENCE_REQUIRED: ["CHALLENGING"],
  CONFIRMED: ["WORLDLINE_GENERATING"],
  WORLDLINE_GENERATING: ["WORLDLINE_READY"],
  WORLDLINE_READY: ["DATA_PREPARING"],
  DATA_PREPARING: ["CLIENT_COMPUTING"],
  CLIENT_COMPUTING: ["PORTFOLIO_READY"],
  PORTFOLIO_READY: ["EXPORTED"],
  EXPORTED: []
};

/** Explicitly models durable happy-path state. Failures retain their prior state and may retry. */
export function canTransition(from: ResearchStatus, to: ResearchStatus): boolean {
  return (TRANSITIONS[from] ?? []).includes(to);
}

export function transition(from: ResearchStatus, to: ResearchStatus): ResearchStatus {
  if (!canTransition(from, to)) {
    throw new Error(`Invalid research state transition: ${from} -> ${to}`);
  }
  return to;
}

export interface ConfirmWithReservationsCommand {
  readonly command: "CONFIRM_WITH_RESERVATIONS";
  readonly unresolved_issues_acknowledged: true;
  readonly reservation_note: string;
}

/** Early confirmation is a distinct audited command from either pre-confirmation review state. */
export function confirmWithReservations(
  from: ResearchStatus,
  command: ConfirmWithReservationsCommand
): ResearchStatus {
  if (
    (from !== "CHALLENGING" && from !== "EVIDENCE_REQUIRED") ||
    command.command !== "CONFIRM_WITH_RESERVATIONS" ||
    command.unresolved_issues_acknowledged !== true ||
    command.reservation_note.trim().length === 0
  ) {
    throw new Error("Early confirmation requires an explicit, acknowledged reservation command");
  }
  return "CONFIRMED";
}
