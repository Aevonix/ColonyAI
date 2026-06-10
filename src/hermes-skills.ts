/**
 * Hermes skills reporter — the inbound half of the Hermes↔Colony
 * skills bridge (v0.18.0).
 *
 * Hermes loads instructional skills from
 * ``~/.hermes/skills/<category>/<name>/SKILL.md`` (markdown with YAML
 * frontmatter: name, description, version, author,
 * metadata.hermes.tags, ...). This module scans that tree at plugin
 * startup and every 24h, parses each SKILL.md's frontmatter with a
 * dependency-free yaml-lite parser, and POSTs the index to Colony as a
 * ``skills``-domain observation batch
 * (``POST /v1/host/observations``) so Colony knows what the agent can
 * do.
 *
 * Push-only by design: Colony's autonomy loop never requests
 * ``agent_sync`` jobs for the ``skills`` domain — this service is the
 * sole writer.
 */

import { promises as fs } from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import type { ColonyPluginContext } from "./plugin.js";

type Logger = {
  info?(m: string): void;
  warn?(m: string): void;
  error?(m: string): void;
};

export interface HermesSkillEntry {
  name: string;
  description: string;
  tags: string[];
  /** Absolute path of the SKILL.md the entry was parsed from. */
  path: string;
}

export interface HermesSkillFrontmatter {
  name?: string;
  description?: string;
  tags: string[];
}

const SKILL_FILE = "SKILL.md";
const MAX_SCAN_DEPTH = 4; // skills root → category → name → SKILL.md (+1 slack)
export const HERMES_SKILLS_REPORT_INTERVAL_MS = 24 * 60 * 60 * 1000;

function stripQuotes(value: string): string {
  const v = value.trim();
  if (
    (v.startsWith('"') && v.endsWith('"') && v.length >= 2) ||
    (v.startsWith("'") && v.endsWith("'") && v.length >= 2)
  ) {
    return v.slice(1, -1);
  }
  return v;
}

function parseInlineList(value: string): string[] {
  // [a, "b", c]
  const inner = value.trim().replace(/^\[/, "").replace(/\]$/, "");
  return inner
    .split(",")
    .map((s) => stripQuotes(s))
    .filter((s) => s.length > 0);
}

/**
 * Minimal frontmatter parse — no YAML dependency. Extracts ``name``,
 * ``description`` (top-level scalars) and ``tags`` (the first ``tags:``
 * key found at any nesting level, which covers both top-level ``tags:``
 * and ``metadata.hermes.tags``; inline ``[a, b]`` and block ``- item``
 * forms are both supported). Returns ``null`` when the file has no
 * leading ``---`` frontmatter block.
 */
export function parseSkillFrontmatter(content: string): HermesSkillFrontmatter | null {
  const normalized = content.replace(/^﻿/, "");
  if (!/^---\s*\r?\n/.test(normalized)) return null;
  const endMatch = normalized.slice(3).match(/\r?\n---\s*(\r?\n|$)/);
  if (!endMatch || endMatch.index === undefined) return null;
  const block = normalized.slice(3, 3 + endMatch.index);

  const out: HermesSkillFrontmatter = { tags: [] };
  const lines = block.split(/\r?\n/);
  let tagsFound = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    if (/^\s*#/.test(line) || line.trim() === "") continue;

    const topLevel = line.match(/^(name|description):\s*(.*)$/);
    if (topLevel) {
      const value = stripQuotes(topLevel[2]!);
      if (topLevel[1] === "name" && !out.name) out.name = value;
      if (topLevel[1] === "description" && !out.description) out.description = value;
      continue;
    }

    const tagsKey = line.match(/^(\s*)tags:\s*(.*)$/);
    if (tagsKey && !tagsFound) {
      const rest = tagsKey[2]!.trim();
      if (rest.startsWith("[")) {
        out.tags = parseInlineList(rest);
        tagsFound = true;
        continue;
      }
      // Block list: collect subsequent more-indented "- item" lines.
      const keyIndent = tagsKey[1]!.length;
      const items: string[] = [];
      for (let j = i + 1; j < lines.length; j++) {
        const candidate = lines[j]!;
        if (candidate.trim() === "" || /^\s*#/.test(candidate)) continue;
        const item = candidate.match(/^(\s*)-\s*(.+)$/);
        if (!item || item[1]!.length <= keyIndent) break;
        items.push(stripQuotes(item[2]!));
      }
      if (items.length > 0) {
        out.tags = items;
        tagsFound = true;
      }
    }
  }
  return out;
}

/**
 * Walk ``rootDir`` (depth-limited) collecting every ``SKILL.md`` with
 * parseable frontmatter. Missing root or unreadable entries are
 * skipped silently — an agent without Hermes skills is a normal state.
 */
export async function scanHermesSkills(
  rootDir: string,
  maxDepth: number = MAX_SCAN_DEPTH,
): Promise<HermesSkillEntry[]> {
  const entries: HermesSkillEntry[] = [];

  async function walk(dir: string, depth: number): Promise<void> {
    if (depth > maxDepth) return;
    let dirents;
    try {
      dirents = await fs.readdir(dir, { withFileTypes: true });
    } catch {
      return; // missing/unreadable directory — nothing to report
    }
    for (const dirent of dirents) {
      if (dirent.name.startsWith(".") || dirent.name === "node_modules") continue;
      const full = path.join(dir, dirent.name);
      if (dirent.isDirectory()) {
        await walk(full, depth + 1);
      } else if (dirent.isFile() && dirent.name === SKILL_FILE) {
        try {
          const content = await fs.readFile(full, "utf8");
          const fm = parseSkillFrontmatter(content);
          if (!fm) continue;
          entries.push({
            name: fm.name ?? path.basename(path.dirname(full)),
            description: fm.description ?? "",
            tags: fm.tags,
            path: full,
          });
        } catch {
          // unreadable SKILL.md — skip
        }
      }
    }
  }

  await walk(rootDir, 0);
  entries.sort((a, b) => a.name.localeCompare(b.name));
  return entries;
}

export function resolveHermesSkillsDir(ctx: ColonyPluginContext): string {
  return ctx.config.hermesSkillsDir ?? path.join(os.homedir(), ".hermes", "skills");
}

/**
 * Scan + report once. Returns the number of skills reported (0 when
 * the directory is empty/missing — Colony is not called in that case).
 */
export async function reportHermesSkills(
  ctx: ColonyPluginContext,
  logger?: Logger,
): Promise<number> {
  const dir = resolveHermesSkillsDir(ctx);
  const skills = await scanHermesSkills(dir);
  if (skills.length === 0) {
    logger?.info?.(`[colony.hermes-skills] no SKILL.md found under ${dir} — nothing to report`);
    return 0;
  }
  const res = await ctx.client.reportObservations({
    domain: "skills",
    reported_by: "hermes-plugin",
    observations: skills.map((s) => ({
      entity_id: s.name,
      payload: {
        description: s.description,
        tags: s.tags,
        path: s.path,
        source: "hermes",
      },
    })),
  });
  logger?.info?.(
    `[colony.hermes-skills] reported ${res.written ?? skills.length} skill(s) from ${dir}`,
  );
  return res.written ?? skills.length;
}

/**
 * Lifecycle service: report the Hermes skill index at startup and
 * every 24h thereafter. All failures are logged and swallowed — the
 * skill index is advisory, never load-bearing for the host.
 */
export function hermesSkillsLifecycleService(
  ctx: ColonyPluginContext,
  logger?: Logger,
) {
  let timer: ReturnType<typeof setInterval> | null = null;

  const runOnce = () =>
    reportHermesSkills(ctx, logger).catch((err) => {
      logger?.warn?.(`[colony.hermes-skills] report failed: ${String(err)}`);
    });

  return {
    id: "colony-hermes-skills",
    async start() {
      // Fire-and-forget initial scan; do not block plugin startup.
      void runOnce();
      timer = setInterval(() => {
        void runOnce();
      }, HERMES_SKILLS_REPORT_INTERVAL_MS);
      timer.unref?.();
    },
    async stop() {
      if (timer) clearInterval(timer);
      timer = null;
    },
  };
}
