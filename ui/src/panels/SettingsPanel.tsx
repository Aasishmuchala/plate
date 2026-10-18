// Settings (T102): read-only facts about the running daemon from `GET /session` and `GET /health`: its version,
// the packets folders it watches, where projects live, dev mode, the router mode, the pixels worker, the nightly
// jobs and which vendor key env vars are set (names only, never values, and only when `/health` reports them).
// Nothing here is editable: the daemon's config file and environment are the truth. Help opens the artist manual in
// a new tab; the docs route needs the bearer token, so the tab is opened at once and filled with the fetched text.

import { type MouseEvent, type ReactElement, useState } from "react";
import { docUrl, errorMessage, fetchDocText, getHealthInfo, getSession, type HealthInfo, type SessionInfo } from "./api";
import { S } from "./theme";
import { useLoad } from "./useLoad";

export const HELP_DOC = "artist-manual.md";

interface Facts {
  session: SessionInfo | null;
  health: HealthInfo | null;
  /** Why one of the two reads failed, when it did. */
  problems: string[];
}

async function loadFacts(): Promise<Facts> {
  const [session, health] = await Promise.allSettled([getSession(), getHealthInfo()]);
  const problems: string[] = [];
  if (session.status === "rejected") problems.push(`session: ${errorMessage(session.reason)}`);
  if (health.status === "rejected") problems.push(`health: ${errorMessage(health.reason)}`);
  return {
    session: session.status === "fulfilled" ? session.value : null,
    health: health.status === "fulfilled" ? health.value : null,
    problems,
  };
}

const NOT_REPORTED = "not reported";

/** A health field as one line: a string as is, a flag as on/off, an object as its plain `key value` pairs. */
export function describe(value: unknown): string {
  if (value === undefined || value === null || value === "") return NOT_REPORTED;
  if (typeof value === "boolean") return value ? "on" : "off";
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.length === 0 ? "none" : value.map(describe).join(", ");
  if (typeof value === "object") {
    const parts = Object.entries(value as Record<string, unknown>)
      .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v))
      .map(([k, v]) => `${k} ${v === null ? "none" : typeof v === "boolean" ? (v ? "on" : "off") : String(v)}`);
    return parts.length > 0 ? parts.join(" · ") : NOT_REPORTED;
  }
  return NOT_REPORTED;
}

function Row({ label, testId, children }: { label: string; testId: string; children: ReactElement | string }): ReactElement {
  return (
    <>
      <dt style={S.muted}>{label}</dt>
      <dd style={{ margin: 0 }} data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

function PathList({ paths }: { paths: string[] }): ReactElement {
  if (paths.length === 0) return <span style={S.muted}>none</span>;
  return (
    <ul style={{ ...S.list, gap: 2 }}>
      {paths.map((p) => (
        <li key={p} style={S.mono}>
          {p}
        </li>
      ))}
    </ul>
  );
}

export function SettingsPanel(): ReactElement {
  const { data, loading, error, reload } = useLoad("settings", loadFacts);
  const [helpError, setHelpError] = useState<string | null>(null);
  const session = data?.session ?? null;
  const health = data?.health ?? null;

  const openHelp = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    setHelpError(null);
    // Open now, inside the click, so the browser does not block it; fill it once the token-carrying fetch lands.
    const tab = window.open("", "_blank");
    fetchDocText(HELP_DOC).then(
      (text) => {
        if (!tab) {
          setHelpError("the browser blocked the new tab: allow pop-ups for this page");
          return;
        }
        tab.document.title = "PLATE artist manual";
        const pre = tab.document.createElement("pre");
        pre.style.whiteSpace = "pre-wrap";
        pre.style.font = "13px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
        pre.style.maxWidth = "100ch";
        pre.style.margin = "24px auto";
        pre.textContent = text; // raw Markdown as text: never parsed as HTML
        tab.document.body.replaceChildren(pre);
      },
      (err: unknown) => {
        tab?.close();
        setHelpError(`the manual did not load: ${errorMessage(err)}`);
      },
    );
  };

  return (
    <section style={S.section} aria-label="Settings" data-testid="settings-panel">
      <div style={{ ...S.row, justifyContent: "space-between" }}>
        <h3 style={S.h}>Settings</h3>
        <span style={S.row}>
          <a
            href={docUrl(HELP_DOC)}
            target="_blank"
            rel="noopener noreferrer"
            onClick={openHelp}
            style={{ ...S.btn, textDecoration: "none" }}
            data-testid="settings-help"
          >
            Help
          </a>
          <button type="button" style={S.btn} onClick={reload} disabled={loading}>
            Refresh
          </button>
        </span>
      </div>
      <p style={{ ...S.muted, margin: 0 }}>Read-only: these come from the daemon's config file and environment.</p>
      {loading ? (
        <p style={S.muted} role="status">
          Loading settings...
        </p>
      ) : null}
      {error ? (
        <p style={S.error} role="alert">
          {error}
        </p>
      ) : null}
      {helpError ? (
        <p style={S.error} role="alert">
          {helpError}
        </p>
      ) : null}
      {data && data.problems.length > 0 ? (
        <p style={S.error} role="alert">
          {data.problems.join("; ")}
        </p>
      ) : null}
      {data ? (
        <dl style={{ display: "grid", gridTemplateColumns: "max-content 1fr", gap: "4px 10px", margin: 0 }}>
          <Row label="Version" testId="settings-version">
            {health?.daemon_version ?? session?.daemon_version ?? NOT_REPORTED}
          </Row>
          <Row label="Packets folders" testId="settings-packets-dirs">
            <PathList paths={session?.packets_dirs ?? []} />
          </Row>
          <Row label="Projects folder" testId="settings-projects-dir">
            <span style={S.mono}>{session?.projects_dir ?? NOT_REPORTED}</span>
          </Row>
          <Row label="Dev mode" testId="settings-dev">
            {session?.dev === undefined ? NOT_REPORTED : session.dev ? "on (PLATE_DEV=1)" : "off"}
          </Row>
          <Row label="Router mode" testId="settings-router-mode">
            {health?.router_mode ?? session?.router_mode ?? NOT_REPORTED}
          </Row>
          <Row label="Pixels worker" testId="settings-pixels">
            {describe(health?.pixels)}
          </Row>
          <Row label="Nightly jobs" testId="settings-nightly">
            {describe(health?.nightly)}
          </Row>
          <Row label="Engine" testId="settings-stub">
            {health?.stub_engine === undefined ? NOT_REPORTED : health.stub_engine ? "stub (the fake engine echoes the plate)" : "vendor adapters"}
          </Row>
          <Row label="Key env vars set" testId="settings-keys">
            {health?.keys_set === null || health?.keys_set === undefined ? (
              NOT_REPORTED
            ) : health.keys_set.length === 0 ? (
              "none"
            ) : (
              <span style={S.row}>
                {health.keys_set.map((k) => (
                  <span key={k} style={{ ...S.chip, ...S.mono }}>
                    {k}
                  </span>
                ))}
              </span>
            )}
          </Row>
        </dl>
      ) : null}
    </section>
  );
}
