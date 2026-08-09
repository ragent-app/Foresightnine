import type {
  ConfirmChallengeRequest,
  ErrorResponse,
  ProbabilityAssessment,
  SseEvent
} from "./generated.js";

const positiveError = {
  schema_version: "v1",
  error_code: "bad_request",
  message: "Bad request",
  request_id: "request:test",
  retryable: false,
  details: []
} satisfies ErrorResponse;

const positiveConfirmation = {
  command: "CONFIRM_WITH_RESERVATIONS",
  unresolved_issues_acknowledged: true,
  reservation_note: "The public base rate remains unresolved."
} satisfies ConfirmChallengeRequest;

const negativeUnknownField: ErrorResponse = {
  schema_version: "v1",
  error_code: "bad_request",
  message: "Bad request",
  request_id: "request:test",
  retryable: false,
  details: [],
  // @ts-expect-error generated contracts reject unknown core fields
  unexpected: true
};

type ProbabilityHasScalarsAndIntervals = ProbabilityAssessment extends {
  premise_probability: number;
  premise_probability_range: { minimum: number; maximum: number };
  conditional_probability: number;
  conditional_probability_range: { minimum: number; maximum: number };
  joint_probability: number;
  joint_probability_range: { minimum: number; maximum: number };
} ? true : false;

const probabilityShapeGate: ProbabilityHasScalarsAndIntervals = true;
declare const event: SseEvent;
const exhaustiveEventName: SseEvent["event"] = event.event;

void positiveError;
void positiveConfirmation;
void negativeUnknownField;
void probabilityShapeGate;
void exhaustiveEventName;
