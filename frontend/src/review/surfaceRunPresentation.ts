export function isUnavailableRunnerError(error: string | undefined): boolean {
  if (!error) return false;
  return /(?:uses\s+)?unavailable runner/i.test(error);
}

export function unavailableRunnerMessage(): string {
  return "This check cannot run in this review environment. Its definition remains reviewable.";
}
