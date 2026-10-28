/**
 * Where Wake says what went wrong in recording. Warnings go to console.warn, prefixed
 * "wake:"; reassign warn or debug to send them elsewhere. Recording never throws into the
 * agent, so this is where its failures show, along with the recorder's health().
 */

export const log = {
  warn(message: string, error?: unknown): void {
    if (error === undefined) console.warn(`wake: ${message}`);
    else console.warn(`wake: ${message}:`, error);
  },
  debug(_message: string, _error?: unknown): void {},
};
