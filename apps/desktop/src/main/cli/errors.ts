export type DesktopCliErrorCode =
  | "cli_not_found"
  | "cli_timeout"
  | "cli_cancelled"
  | "cli_output_limit"
  | "cli_protocol_error"
  | "cli_process_error"
  | "cli_command_error";

export class DesktopCliError extends Error {
  readonly code: DesktopCliErrorCode;

  constructor(code: DesktopCliErrorCode, safeMessage: string) {
    super(safeMessage);
    this.name = "DesktopCliError";
    this.code = code;
  }
}
